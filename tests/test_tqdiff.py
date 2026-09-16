"""tqdiff 解析函数测试（纯数据转换，不依赖网络）。"""

from __future__ import annotations

import unittest

from tqdiff import auth as diff_auth


class ParseInstrumentRecordTests(unittest.TestCase):
    def test_full_record(self):
        entry = {
            "class": "FUTURE", "instrument_id": "SHFE.rb2610", "exchange_id": "SHFE",
            "ins_name": "螺纹钢2610", "price_tick": 1.0, "volume_multiple": 10,
            "expired": False, "expire_datetime": 1_800_000_000.0,
        }
        record = diff_auth.parse_instrument_record("SHFE.rb2610", entry)
        self.assertEqual(record["symbol"], "SHFE.rb2610")
        self.assertEqual(record["exchange"], "SHFE")
        self.assertEqual(record["instrument_id"], "rb2610")
        self.assertEqual(record["name"], "螺纹钢2610")
        self.assertEqual(record["price_tick"], 1.0)
        self.assertIsNotNone(record["expire_rest_days"])

    def test_prefixed_instrument_id_dedup(self):
        entry = {"class": "FUTURE", "instrument_id": "CFFEX.IC2608",
                 "exchange_id": "CFFEX", "ins_name": "中证2608"}
        record = diff_auth.parse_instrument_record("CFFEX.IC2608", entry)
        self.assertEqual(record["instrument_id"], "IC2608")
        self.assertEqual(record["symbol"], "CFFEX.IC2608")

    def test_empty_entry(self):
        self.assertIsNone(diff_auth.parse_instrument_record("X.Y", {}))
        self.assertIsNone(diff_auth.parse_instrument_record("X.Y", None))


class ParseKlineRowTests(unittest.TestCase):
    def test_dict_row(self):
        # 实测格式：nfmd 前置返回字典行
        row = {"datetime": 1_788_531_000_000_000_000, "open": 3109, "high": 3111,
               "low": 3108, "close": 3108, "volume": 442, "open_oi": 576030, "close_oi": 576055}
        bar = diff_auth.parse_kline_row(row)
        self.assertEqual(bar["datetime"], 1_788_531_000_000)
        self.assertEqual(bar["open"], 3109.0)
        self.assertEqual(bar["close"], 3108.0)
        self.assertEqual(bar["open_interest"], 576055.0)

    def test_list_row(self):
        row = [1_756_700_000_000_000_000, 3122.0, 3125.0, 3121.0, 3124.0, 17142, 1171737, 1171600]
        bar = diff_auth.parse_kline_row(row)
        self.assertEqual(bar["datetime"], 1_756_700_000_000)
        self.assertEqual(bar["close"], 3124.0)
        self.assertEqual(bar["open_interest"], 1171600.0)

    def test_nan_close_rejected(self):
        nan = float("nan")
        self.assertIsNone(diff_auth.parse_kline_row([1, 1, 1, 1, nan, 1, 0, 0]))

    def test_short_row_rejected(self):
        self.assertIsNone(diff_auth.parse_kline_row([1, 2, 3]))


class ParseKlineRowEdgeCaseTests(unittest.TestCase):
    """K 线解析边界用例（对应待办 P0：空响应/字段缺失/非法值不产生脏数据）。"""

    def test_none_row_rejected(self):
        self.assertIsNone(diff_auth.parse_kline_row(None))

    def test_empty_list_rejected(self):
        self.assertIsNone(diff_auth.parse_kline_row([]))

    def test_dict_row_missing_optional_fields(self):
        # 缺 volume/open_oi/close_oi：volume 兜底 0，close_oi 允许为 None
        row = {"datetime": 1_788_531_000_000_000_000, "open": 1, "high": 2,
               "low": 0.5, "close": 1.5}
        bar = diff_auth.parse_kline_row(row)
        self.assertIsNotNone(bar)
        self.assertEqual(bar["volume"], 0.0)
        self.assertIsNone(bar["open_interest"])

    def test_dict_row_non_numeric_rejected(self):
        # 字段缺失到连 OHLC 都不完整（None）→ 拒绝该行，不抛异常
        row = {"datetime": 1_788_531_000_000_000_000, "open": None, "high": 2,
               "low": 0.5, "close": 1.5}
        self.assertIsNone(diff_auth.parse_kline_row(row))

    def test_dict_row_bad_datetime_rejected(self):
        row = {"datetime": "not-a-number", "open": 1, "high": 2, "low": 0.5, "close": 1.5}
        self.assertIsNone(diff_auth.parse_kline_row(row))

    def test_normal_history_series_order_preserved(self):
        # 正常历史数据：多行解析后按输入顺序输出，OHLC/成交量/持仓全字段有效
        base_ns = 1_788_531_000_000_000_000
        rows = []
        for i in range(5):
            rows.append([base_ns + i * 300_000_000_000, 3000 + i, 3010 + i,
                         2990 + i, 3005 + i, 100 + i, 500000, 500100 + i])
        bars = [diff_auth.parse_kline_row(r) for r in rows]
        self.assertTrue(all(b is not None for b in bars))
        datetimes = [b["datetime"] for b in bars]
        self.assertEqual(datetimes, sorted(datetimes))          # 时间升序
        self.assertEqual([b["close"] for b in bars], [3005 + i for i in range(5)])
        self.assertEqual(bars[0]["volume"], 100.0)
        self.assertEqual(bars[-1]["open_interest"], 500104.0)


class KlineDiffMergeTests(unittest.TestCase):
    """盘中增量回归：正在形成的 K 线只推"变化的字段"，必须合并而非整体替换。

    真机实测 bug（2026-09-11）：替换导致形成中的 K 线丢失 open/low 等字段、
    解析失败从图上消失，直到重启重新拉全量才恢复（表现为"K 线不自己更新"）。
    """

    DUR = 60_000_000_000

    def _client_with_chart(self):
        from tqdiff.client import DiffClient
        import asyncio
        client = DiffClient("acc", "pwd")   # 只用其数据结构，不联网
        client._charts[("SHFE.rb2610", self.DUR)] = {
            "rows": {}, "last_id": -1, "ready": asyncio.Event(), "view_width": 10,
        }
        return client

    def test_full_row_then_partial_update_merges(self):
        client = self._client_with_chart()
        full = {"datetime": 1_789_138_200_000_000_000, "open": 3042, "high": 3043,
                "low": 3042, "close": 3043, "volume": 254, "open_oi": 409615, "close_oi": 409574}
        client._apply_kline_diff("SHFE.rb2610", self.DUR, {"data": {"5": full}, "last_id": 5})
        # 盘中 tick：服务器只推变化的字段
        client._apply_kline_diff("SHFE.rb2610", self.DUR,
                                 {"data": {"5": {"close": 3050, "high": 3051, "volume": 300}},
                                  "last_id": 5})
        row = client._charts[("SHFE.rb2610", self.DUR)]["rows"][5]
        bar = diff_auth.parse_kline_row(row)
        self.assertIsNotNone(bar)                # 合并后字段完整，仍可解析
        self.assertEqual(bar["close"], 3050.0)   # 新值生效
        self.assertEqual(bar["open"], 3042.0)    # 旧字段没有被冲掉
        self.assertEqual(bar["volume"], 300.0)

    def test_apply_diff_routes_by_symbol(self):
        client = self._client_with_chart()
        client._apply_diff({"klines": {"SHFE.rb2610": {"60000000000": {
            "data": {"7": {"datetime": 1, "open": 1, "high": 1, "low": 1, "close": 1}},
            "last_id": 7}}}})
        chart = client._charts[("SHFE.rb2610", self.DUR)]
        self.assertEqual(chart["last_id"], 7)
        self.assertIn(7, chart["rows"])

    def test_unknown_chart_diff_ignored(self):
        client = self._client_with_chart()
        client._apply_diff({"klines": {"UNKNOWN.zz": {"60000000000": {
            "data": {"1": {}}, "last_id": 1}}}})
        # 未注册的图表数据被安全忽略，不影响已有图表
        self.assertEqual(client._charts[("SHFE.rb2610", self.DUR)]["last_id"], -1)


if __name__ == "__main__":
    unittest.main()

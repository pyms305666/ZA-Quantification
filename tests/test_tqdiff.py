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


if __name__ == "__main__":
    unittest.main()

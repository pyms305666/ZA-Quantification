"""tqdiff 解析函数测试（纯数据转换，不依赖网络）。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import urllib3.exceptions

from tqdiff import auth as diff_auth
from tqdiff.client import DiffClient, SymbolNotFoundError, TqClientError


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


class CatalogCompletenessTests(unittest.TestCase):
    def _partial_client(self) -> DiffClient:
        client = DiffClient("acc", "pwd")
        client._file_loaded.set()
        client._symbol_file = {"SHFE.rb2610": {"symbol": "SHFE.rb2610"}}
        return client

    def test_partial_catalog_miss_is_recoverable(self):
        client = self._partial_client()
        with self.assertRaises(TqClientError) as raised:
            asyncio.run(client._get_instrument("SHFE.au2612"))
        self.assertNotIsInstance(raised.exception, SymbolNotFoundError)
        self.assertFalse(client.catalog_complete)

    def test_complete_catalog_miss_is_strictly_rejected(self):
        client = self._partial_client()
        client._catalog_complete.set()
        with self.assertRaises(SymbolNotFoundError):
            asyncio.run(client._get_instrument("SHFE.au2612"))
        self.assertTrue(client.catalog_complete)


class SubscriptionLatencyTests(unittest.TestCase):
    def test_subscribe_returns_without_waiting_for_first_quote(self):
        client = DiffClient("acc", "pwd")
        client._subscribed.add("SHFE.au2612")

        async def subscribe():
            return await asyncio.wait_for(client._subscribe("SHFE.au2612"), timeout=0.1)

        self.assertEqual(asyncio.run(subscribe()), "SHFE.au2612")

    def test_resend_subscribe_releases_data_lock_before_sending(self):
        client = DiffClient("acc", "pwd")
        client._subscribed.add("SHFE.au2612")

        async def send(pack):
            self.assertFalse(client._data_lock.locked())

        client._send = send
        asyncio.run(client._resend_subscribe())

    def test_queue_subscription_records_state_without_waiting_for_loop(self):
        client = DiffClient("acc", "pwd")

        client.queue_subscription("SHFE.au2612")

        self.assertEqual(client._subscribed, {"SHFE.au2612"})


class _FakeRaw:
    """模拟 requests 响应的 raw 字节流（本模块只用 stream(amt=, decode_content=)）。

    break_after_bytes 模拟连接中途被重置：发出部分字节后抛 urllib3 ProtocolError
    （该异常不是 OSError 子类，见 download_symbol_file 的捕获说明）。
    """

    def __init__(self, chunks, break_after_bytes=None):
        self._chunks = chunks
        self._break_after = break_after_bytes

    def stream(self, amt=0, decode_content=True):
        sent = 0
        for chunk in self._chunks:
            if self._break_after is not None and sent + len(chunk) > self._break_after:
                keep = self._break_after - sent
                if keep > 0:
                    yield chunk[:keep]
                raise urllib3.exceptions.ProtocolError("Connection broken: IncompleteRead")
            sent += len(chunk)
            yield chunk


class _FakeResponse:
    def __init__(self, status_code, headers, chunks, break_after_bytes=None):
        self.status_code = status_code
        self.headers = headers
        self.raw = _FakeRaw(chunks, break_after_bytes=break_after_bytes)


class _FakeCatalogServer:
    """可编程的 latest.json 假服务器：按脚本顺序响应，并记录每次请求头。"""

    def __init__(self, plain: bytes, etag: str = "W/\"test-etag\""):
        self.plain = plain
        self.etag = etag
        self.gz = gzip.compress(plain)
        self.total = len(plain)
        self.requests: list[dict] = []
        self.script: list = []          # 每项为 callable(request_headers) -> _FakeResponse
        self.on_exhausted = None        # 脚本耗尽时的兜底（默认报错）

    def get(self, url, headers=None, timeout=None, stream=True):
        self.requests.append(dict(headers or {}))
        index = len(self.requests) - 1
        if index >= len(self.script):
            if self.on_exhausted is not None:
                return self.on_exhausted(self.requests[-1])
            raise AssertionError(f"假服务器脚本耗尽（第 {index + 1} 次请求无响应脚本）")
        return self.script[index](self.requests[-1])

    # ---- 常用响应脚本 ----
    def script_gzip_200(self):
        self.script.append(lambda headers: _FakeResponse(
            200, {"content-length": str(len(self.gz)),
                  "content-encoding": "gzip", "etag": self.etag}, [self.gz]))

    def script_gzip_status(self, status):
        self.script.append(lambda headers: _FakeResponse(status, {}, []))

    def script_identity_200(self):
        self.script.append(lambda headers: _FakeResponse(
            200, {"content-length": str(self.total), "etag": self.etag},
            [self.plain]))

    def script_resume_206(self, offset):
        tail = self.plain[offset:]
        self.script.append(lambda headers: _FakeResponse(
            206, {"content-range": f"bytes {offset}-{self.total - 1}/{self.total}",
                  "etag": self.etag}, [tail]))

    def script_gzip_break_midstream(self, after_bytes):
        self.script.append(lambda headers: _FakeResponse(
            200, {"content-length": str(len(self.gz)),
                  "content-encoding": "gzip", "etag": self.etag},
            [self.gz], break_after_bytes=after_bytes))

    def script_identity_break_midstream(self, after_bytes):
        self.script.append(lambda headers: _FakeResponse(
            200, {"content-length": str(self.total), "etag": self.etag},
            [self.plain], break_after_bytes=after_bytes))


class DownloadSymbolFileTests(unittest.TestCase):
    """download_symbol_file 双路径（gzip 快路径 + identity Range 续传）。"""

    PLAIN = json.dumps({
        "SHFE.rb2610": {"class": "FUTURE", "instrument_id": "SHFE.rb2610",
                        "exchange_id": "SHFE", "ins_name": "螺纹钢2610",
                        "price_tick": 1.0, "volume_multiple": 10, "expired": False},
        "DCE.m2609": {"class": "FUTURE", "instrument_id": "DCE.m2609",
                      "exchange_id": "DCE", "ins_name": "豆粕2609",
                      "price_tick": 1.0, "volume_multiple": 10, "expired": False},
    }).encode("utf-8")

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="za-test-cache-")
        self._old_cache = os.environ.get("TQ_GATEWAY_CACHE")
        os.environ["TQ_GATEWAY_CACHE"] = self._tmp
        self.server = _FakeCatalogServer(self.PLAIN)
        patcher = mock.patch("tqdiff.auth.requests.get", self.server.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("TQ_GATEWAY_CACHE", None)
        else:
            os.environ["TQ_GATEWAY_CACHE"] = self._old_cache

    def _part(self) -> Path:
        return diff_auth._part_path()

    def _etag_file(self) -> Path:
        return Path(str(self._part()) + ".etag")

    def _seed_part(self, data: bytes, etag: str = "W/\"test-etag\""):
        part = self._part()
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(data)
        self._etag_file().write_text(etag, encoding="utf-8")

    def test_gzip_fast_path_downloads_and_parses(self):
        self.server.script_gzip_200()
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        self.assertEqual(len(self.server.requests), 1)
        # 快路径不产生续传状态文件
        self.assertFalse(self._part().exists())
        self.assertFalse(self._etag_file().exists())

    def test_no_part_falls_back_to_resume_after_gzip_failure(self):
        self.server.script_gzip_status(500)
        self.server.script_identity_200()
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        self.assertEqual(len(self.server.requests), 2)
        # 第一次走 gzip 快路径，第二次续传路径请求 identity
        self.assertEqual(self.server.requests[0].get("Accept-Encoding"), "gzip")
        self.assertEqual(self.server.requests[1].get("Accept-Encoding"), "identity")
        self.assertNotIn("Range", self.server.requests[1])

    def test_resume_appends_tail_from_206(self):
        offset = len(self.PLAIN) // 2
        self._seed_part(self.PLAIN[:offset])
        self.server.script_resume_206(offset)
        progress_log = []
        symbols = diff_auth.download_symbol_file(
            "tok", progress=lambda done, total: progress_log.append((done, total)))
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        request = self.server.requests[0]
        self.assertEqual(request.get("Range"), f"bytes={offset}-")
        self.assertEqual(request.get("If-Range"), "W/\"test-etag\"")
        self.assertEqual(request.get("Accept-Encoding"), "identity")
        # 首个进度点是断点处，末点是全量
        self.assertEqual(progress_log[0], (offset, len(self.PLAIN)))
        self.assertEqual(progress_log[-1], (len(self.PLAIN), len(self.PLAIN)))
        # 成功后清理续传状态
        self.assertFalse(self._part().exists())
        self.assertFalse(self._etag_file().exists())

    def test_resume_restarts_from_200_on_etag_mismatch(self):
        self._seed_part(self.PLAIN[:100], etag="W/\"stale\"")
        self.server.etag = "W/\"new-etag\""
        self.server.script_identity_200()
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        request = self.server.requests[0]
        self.assertEqual(request.get("Range"), "bytes=100-")
        self.assertEqual(request.get("If-Range"), "W/\"stale\"")
        # 200 全量应整体重写 .part（若错误追加，JSON 拼接必然解析失败）
        self.assertFalse(self._part().exists())

    def test_resume_416_resets_and_restarts(self):
        self._seed_part(self.PLAIN[:100])
        # 远端全量 100 与 .part 大小不符 → 416 重置后全量重收
        self.server.script.append(lambda headers: _FakeResponse(
            416, {"content-range": f"bytes */{self.server.total + 50}"}, []))
        self.server.script_identity_200()
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("Range", self.server.requests[0])
        self.assertNotIn("Range", self.server.requests[1])

    def test_resume_416_with_complete_part_parses_without_more_download(self):
        self._seed_part(self.PLAIN)   # .part 与远端全量等大：上次"下完没解析"中断
        self.server.script.append(lambda headers: _FakeResponse(
            416, {"content-range": f"bytes */{self.server.total}"}, []))
        self.server.on_exhausted = lambda headers: (_ for _ in ()).throw(
            AssertionError("文件已完整时不应再发起下载"))
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self._part().exists())

    def test_parse_failure_clears_part_state(self):
        garbage = b"not-json{{{"
        self._seed_part(garbage)
        self.server.script.append(lambda headers: _FakeResponse(
            416, {"content-range": f"bytes */{len(garbage)}"}, []))
        with self.assertRaises(diff_auth.DiffAuthError):
            diff_auth.download_symbol_file("tok")
        # 损坏文件无法靠续传修复 → 状态清理，下轮从 0 重收
        self.assertFalse(self._part().exists())
        self.assertFalse(self._etag_file().exists())

    def test_midstream_reset_falls_back_to_identity(self):
        # gzip 全量传到一半连接被重置（ProtocolError 非 OSError）→ 必须降级续传而不是直接失败
        self.server.script_gzip_break_midstream(len(self.server.gz) // 2)
        self.server.script_identity_200()
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        self.assertEqual(len(self.server.requests), 2)

    def test_identity_break_keeps_state_then_resumes(self):
        # 首次 identity 传输中断：.part 保留已收字节，ETag 已先于收流落盘 → 下次 206 续传
        offset = len(self.PLAIN) // 3
        self.server.script_gzip_status(500)   # 无 .part 时先走 gzip 快路径，让其快速失败
        self.server.script_identity_break_midstream(offset)
        with self.assertRaises(diff_auth.DiffAuthError):
            diff_auth.download_symbol_file("tok")
        self.assertTrue(self._part().exists())
        self.assertEqual(self._part().stat().st_size, offset)
        self.assertEqual(self._etag_file().read_text(encoding="utf-8").strip(),
                         "W/\"test-etag\"")
        # 第二次调用：从断点 206 续传到完成（请求序：gzip500 → identity中断 → 206续传）
        self.server.script_resume_206(offset)
        symbols = diff_auth.download_symbol_file("tok")
        self.assertEqual(set(symbols), {"SHFE.rb2610", "DCE.m2609"})
        self.assertEqual(len(self.server.requests), 3)
        request = self.server.requests[2]
        self.assertEqual(request.get("Range"), f"bytes={offset}-")
        self.assertEqual(request.get("If-Range"), "W/\"test-etag\"")
        self.assertFalse(self._part().exists())

    def test_index_from_file_matches_gzip_path(self):
        from tqdiff import symbol_index
        plain_path = Path(self._tmp) / "plain.json"
        gz_path = Path(self._tmp) / "plain.json.gz"
        plain_path.write_bytes(self.PLAIN)
        gz_path.write_bytes(gzip.compress(self.PLAIN))
        from_file = symbol_index.index_from_file(plain_path)
        from_gzip = symbol_index.index_from_gzip_file(gz_path)
        self.assertEqual(from_file.keys(), from_gzip.keys())
        self.assertEqual(from_file["SHFE.rb2610"], from_gzip["SHFE.rb2610"])


if __name__ == "__main__":
    unittest.main()

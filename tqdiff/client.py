"""DIFF 协议行情客户端（C 路线核心）。

架构：
- 独立线程跑 asyncio 事件循环：认证/合约文件（阻塞 HTTP，executor 执行）→
  WebSocket 长连接（接收协程）→ 全部命令以协程并发执行。
- 与旧 TqClient 的命令接口完全一致（subscribe / unsubscribe / subscribed /
  get_instrument / get_instruments_info / query_instruments / query_options /
  get_kline），上层 tq/instruments、tq/subscriber、api/ 零改动。
- 关键差异：没有任何"长命令"独占事件循环——合约信息全部来自静态文件（内存读取），
  K 线通过 set_chart 异步到达，命令只做"发一条 ws 消息 + 等本地事件"，
  因此不存在队列堵塞 / 活锁的土壤。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Optional
from types import SimpleNamespace

from market.processor import to_market_quote
from . import auth as diff_auth

FUTURE_SYMBOL_RE = re.compile(r"^[A-Z]+\.[a-z]{1,3}\d{3,4}$")
EXCHANGE_INSTRUMENT_CASE = {
    "SHFE": "lower", "DCE": "lower", "INE": "lower", "GFEX": "lower",
    "CZCE": "upper", "CFFEX": "upper",
}


class TqClientError(RuntimeError):
    """连接或命令执行失败（与旧 TqClient 对外一致的错误类型）。"""


def _candidates(symbol: str) -> list[str]:
    """同一合约的大小写候选（静态文件键为各交易所规范写法）。"""
    value = (symbol or "").strip()
    if not value or "." not in value:
        return [value] if value else []
    exchange, instrument = value.split(".", 1)
    case = EXCHANGE_INSTRUMENT_CASE.get(exchange.upper())
    out = [value]
    if case == "lower":
        out += [f"{exchange}.{instrument.lower()}"]
    elif case == "upper":
        out += [f"{exchange}.{instrument.upper()}"]
    out += [f"{exchange}.{instrument.lower()}", f"{exchange}.{instrument.upper()}"]
    seen: set[str] = set()
    return [item for item in out if not (item in seen or seen.add(item))]


class DiffClient:
    READY_DELAY_SECONDS = 0.0  # DIFF 路线无预热期（无 free-api 登录排队问题）

    def __init__(self, account: str, password: str, wait_deadline: float = 0.5) -> None:
        self._account = account
        self._password = password
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        self._error: Optional[str] = None
        self._last_status = ""
        self._on_quote_change: Optional[Callable[[Any], None]] = None
        self._on_status: Optional[Callable[[str], None]] = None
        # 事件循环与连接
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._token: Optional[str] = None
        self._file_task: Optional[asyncio.Task] = None
        self._file_loaded: asyncio.Event = asyncio.Event()
        # 数据状态（仅事件循环协程内读写；快照读取加 _data_lock）
        self._data_lock = threading.Lock()
        self._symbol_file: dict[str, Any] = {}
        self._quotes: dict[str, dict] = {}
        self._quote_events: dict[str, asyncio.Event] = {}
        self._subscribed: set[str] = set()
        self._charts: dict[tuple[str, int], dict] = {}  # (symbol, dur_ns) -> 缓冲

    # ------------------------------------------------------------ 状态与回调

    def set_callbacks(
        self,
        on_quote_change: Optional[Callable[[Any], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_quote_change = on_quote_change
        self._on_status = on_status

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def ready(self) -> bool:
        return self.connected

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def account(self) -> str:
        return self._account

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            self._connected = value

    def _set_error(self, value: Optional[str]) -> None:
        with self._lock:
            self._error = value

    def _status(self, value: str) -> None:
        if value != self._last_status and self._on_status is not None:
            self._last_status = value
            self._on_status(value)

    # ------------------------------------------------------------ 线程安全接口

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="diff-loop", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def run_command(self, command: str, *args: Any, timeout: float = 8.0) -> Any:
        """提交命令协程并等待结果；接口与旧 TqClient 完全一致。"""
        loop = self._loop
        if loop is None or not loop.is_running() or not self.connected:
            raise TqClientError("DIFF 行情连接初始化中，请稍候重试")
        coro = self._execute(command, args)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TqClientError:
            raise
        except TimeoutError:
            future.cancel()
            raise TqClientError(f"命令超时（{timeout:g}s）：{command}") from None
        except Exception as error:
            raise TqClientError(str(error)) from error

    # ------------------------------------------------------------ 主循环

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            while not self._stop.is_set():
                try:
                    self._loop.run_until_complete(self._session())
                    self._status("连接已断开")
                except Exception as error:
                    self._set_error(str(error))
                    self._status(f"连接异常，3 秒后重连：{error}")
                self._set_connected(False)
                if self._stop.wait(3.0):
                    break
        finally:
            self._set_connected(False)
            self._loop.close()

    async def _session(self) -> None:
        """一次完整会话：认证 → WebSocket → （后台）合约文件。

        行情连接不依赖合约文件：K 线/订阅先可用，目录后台补齐——
        网络再慢也不会拖住连接建立。
        """
        import websockets

        self._status("天勤登录中")
        token_info = await self._loop.run_in_executor(
            None, lambda: diff_auth.login(self._account, self._password))
        self._token = token_info["access_token"]

        self._status("获取行情网关地址")
        md_url = await self._loop.run_in_executor(
            None, lambda: diff_auth.get_md_url(self._token))

        self._send_lock = asyncio.Lock()
        self._status("连接行情服务器")
        async with websockets.connect(md_url, open_timeout=20, ping_interval=15) as ws:
            self._ws = ws
            self._set_error(None)
            self._set_connected(True)
            self._status("行情服务器已连接")
            self._ensure_symbol_file_task()
            await self._send({"aid": "peek_message"})
            async for raw in ws:
                if self._stop.is_set():
                    return
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                for diff in message.get("data") or []:
                    if not isinstance(diff, dict):
                        continue
                    try:
                        self._apply_diff(diff)
                    except Exception as error:
                        self._status(f"数据包处理异常（已忽略）：{error}")
                await self._send({"aid": "peek_message"})

    def _ensure_symbol_file_task(self) -> None:
        """启动合约目录后台加载（缓存优先，失败 10 秒后重试）。"""
        if self._file_task is not None and not self._file_task.done():
            return
        self._file_task = self._loop.create_task(self._load_symbol_file())

    async def _load_symbol_file(self) -> None:
        while not self._stop.is_set():
            cached = await self._loop.run_in_executor(
                None, diff_auth.load_cached_symbol_file)
            if cached is not None:
                with self._data_lock:
                    self._symbol_file = cached
                self._file_loaded.set()
                self._status(f"合约目录就绪（缓存，{len(cached)} 个合约）")
                return
            def _download():
                return diff_auth.download_symbol_file(
                    self._token,
                    progress=lambda done, total: self._status(
                        f"下载合约目录 {done // 1024 // 1024}MB"
                        + (f"/{total // 1024 // 1024}MB" if total else "")),
                )
            try:
                symbols = await self._loop.run_in_executor(None, _download)
                with self._data_lock:
                    self._symbol_file = symbols
                self._file_loaded.set()
                self._status(f"合约目录就绪（{len(symbols)} 个合约）")
                return
            except Exception as error:
                self._status(f"合约目录下载失败，10 秒后重试：{error}")
                await asyncio.sleep(10.0)

    async def _wait_file(self, timeout: float) -> bool:
        """等待合约目录就绪（K 线/订阅不依赖它；目录查询依赖）。"""
        if self._file_loaded.is_set():
            return True
        self._ensure_symbol_file_task()
        try:
            await asyncio.wait_for(asyncio.shield(self._file_loaded), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _send(self, pack: dict) -> None:
        assert self._send_lock is not None
        async with self._send_lock:
            if self._ws is not None:
                await self._ws.send(json.dumps(pack, ensure_ascii=False))

    # ------------------------------------------------------------ 数据合并

    def _apply_diff(self, diff: dict) -> None:
        quotes_diff = diff.get("quotes")
        if isinstance(quotes_diff, dict):
            for symbol, fields in quotes_diff.items():
                if not isinstance(fields, dict):
                    continue
                with self._data_lock:
                    merged = self._quotes.setdefault(symbol, {})
                    merged.update(fields)
                    snapshot = dict(merged)
                self._dispatch_quote(symbol, snapshot)

        klines_diff = diff.get("klines")
        if isinstance(klines_diff, dict):
            for symbol, durations in klines_diff.items():
                if not isinstance(durations, dict):
                    continue
                for dur_str, chart_data in durations.items():
                    if not isinstance(chart_data, dict):
                        continue
                    self._apply_kline_diff(symbol, int(dur_str), chart_data)

    def _apply_kline_diff(self, symbol: str, dur_ns: int, chart_data: dict) -> None:
        if not isinstance(chart_data, dict):
            return
        key = (symbol, dur_ns)
        with self._data_lock:
            buffer = self._charts.get(key)
            if buffer is None:
                return  # 未请求的图表数据（理论上不会出现）
            data = chart_data.get("data")
            if isinstance(data, dict):
                for index, row in data.items():
                    if index == "@":
                        continue
                    try:
                        buffer["rows"][int(index)] = row
                    except (TypeError, ValueError):
                        continue
                anchor = data.get("@")
            else:
                anchor = None
            last_id = chart_data.get("last_id", anchor)
            if last_id is not None:
                try:
                    buffer["last_id"] = int(last_id)
                except (TypeError, ValueError):
                    pass
            buffer["ready"].set()

    def _dispatch_quote(self, symbol: str, snapshot: dict) -> None:
        event = self._quote_events.get(symbol)
        if event is not None:
            event.set()
        if self._on_quote_change is None:
            return
        if snapshot.get("last_price") in (None, ""):
            return
        dt = snapshot.get("datetime")
        if isinstance(dt, (int, float)) and dt > 10**14:  # 纳秒 -> 毫秒
            snapshot["datetime"] = int(dt) // 1_000_000
        view = SimpleNamespace(**snapshot)
        market_quote = to_market_quote(symbol, view)
        if market_quote is not None:
            self._on_quote_change(market_quote)

    # ------------------------------------------------------------ 命令实现（协程，事件循环内并发）

    async def _execute(self, command: str, args: tuple) -> Any:
        if command == "subscribe":
            return await self._subscribe(args[0])
        if command == "unsubscribe":
            return await self._unsubscribe(args[0])
        if command == "subscribed":
            with self._data_lock:
                return sorted(self._subscribed)
        if command == "get_instrument":
            return await self._get_instrument(args[0])
        if command == "get_instruments_info":
            return await self._get_instruments_info(list(args[0]))
        if command == "query_instruments":
            return await self._query_instruments()
        if command == "query_options":
            raise TqClientError("C 直连路线暂未实现期权查询")
        if command == "start_catalog_worker":
            return True  # C 路线无后台目录任务（目录来自静态文件）
        if command == "get_kline":
            return await self._get_kline(args[0], int(args[1]), int(args[2]))
        raise TqClientError(f"未知命令：{command}")

    def _file_entry(self, symbol: str) -> Optional[dict]:
        with self._data_lock:
            file_data = self._symbol_file
        for candidate in _candidates(symbol):
            entry = file_data.get(candidate)
            if isinstance(entry, dict) and entry:
                return entry
        return None

    async def _resend_subscribe(self) -> None:
        with self._data_lock:
            ins_list = ",".join(sorted(self._subscribed))
        pack = {"aid": "subscribe_quote", "ins_list": ins_list}
        if ins_list:
            await self._send(pack)

    async def _subscribe(self, symbol: str) -> str:
        have_file = await self._wait_file(5.0)
        if have_file:
            entry = self._file_entry(symbol)
            if entry is None:
                raise TqClientError(f"合约不存在或查询失败：{symbol}")
            if entry.get("expired"):
                raise TqClientError(f"合约已过期或不存在：{symbol}")
        record = diff_auth.parse_instrument_record(symbol, entry)
        if record is None:
            raise TqClientError(f"合约不存在或查询失败：{symbol}")
        canonical = record["symbol"]
        event = self._quote_events.get(canonical)
        if event is None:
            event = asyncio.Event()
            self._quote_events[canonical] = event
        if canonical not in self._subscribed:
            with self._data_lock:
                self._subscribed.add(canonical)
            await self._resend_subscribe()
        try:
            await asyncio.wait_for(event.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            pass  # 非交易时段可能无首笔行情：订阅本身已成功
        # 休市时段服务器不再推 diffs：主动重放当前快照，保证页面订阅后立刻有行情
        with self._data_lock:
            snapshot = dict(self._quotes.get(canonical) or {})
        if snapshot:
            self._dispatch_quote(canonical, snapshot)
        return canonical

    async def _unsubscribe(self, symbol: str) -> None:
        with self._data_lock:
            self._subscribed.discard(symbol)
            self._quotes.pop(symbol, None)
            self._quote_events.pop(symbol, None)
        await self._resend_subscribe()
        return None

    async def _get_instrument(self, symbol: str) -> dict:
        if not await self._wait_file(60.0):
            raise TqClientError("合约目录后台下载中，请稍候重试")
        entry = self._file_entry(symbol)
        if entry is None:
            raise TqClientError(f"合约不存在或查询失败：{symbol}")
        record = diff_auth.parse_instrument_record(symbol, entry)
        if record is None:
            raise TqClientError(f"合约不存在或查询失败：{symbol}")
        return record

    async def _get_instruments_info(self, symbols: list[str]) -> dict:
        if not await self._wait_file(60.0):
            raise TqClientError("合约目录后台下载中，请稍候重试")
        output: dict[str, dict] = {}
        for symbol in symbols:
            entry = self._file_entry(symbol)
            record = diff_auth.parse_instrument_record(symbol, entry) if entry else None
            if record is not None:
                output[symbol] = record
        return output

    async def _query_instruments(self) -> list[str]:
        if not await self._wait_file(60.0):
            raise TqClientError("合约目录后台下载中，请稍候重试")
        with self._data_lock:
            file_data = self._symbol_file
        symbols = []
        for key, entry in file_data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("class") != "FUTURE" or entry.get("expired"):
                continue
            if key.upper().startswith("KQD."):
                continue  # 外盘主连，国内评估器不需要
            if not FUTURE_SYMBOL_RE.match(key):
                continue
            symbols.append(key)
        return sorted(symbols)

    async def _get_kline(self, symbol: str, period: int, count: int) -> list[dict]:
        # K 线不依赖合约目录文件：目录还在后台下载时也应可用
        canonical = symbol
        dur_ns = period * 1_000_000_000
        fetch_length = max(count, 400)
        key = (canonical, dur_ns)
        with self._data_lock:
            buffer = self._charts.get(key)
            if buffer is None:
                buffer = {
                    "rows": {},
                    "last_id": -1,
                    "ready": asyncio.Event(),
                    "view_width": fetch_length,
                }
                self._charts[key] = buffer
            else:
                buffer["view_width"] = max(buffer["view_width"], fetch_length)
            rows = dict(buffer["rows"])
            last_id = buffer["last_id"]
        if last_id < 0:
            # 注意：合约服务就绪（insserve_ready）之前发送的 set_chart 会被前置丢弃，
            # 因此轮询重发直到服务器返回 last_id（实测 2~6 秒内就绪）。
            chart_id = f"ZAQ_{abs(hash(key)) % 10**10}"
            pack = {
                "aid": "set_chart",
                "chart_id": chart_id,
                "ins_list": canonical,
                "duration": dur_ns,
                "view_width": fetch_length,
            }
            deadline = time.monotonic() + 20.0
            resend_at = 0.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= resend_at:
                    await self._send(pack)
                    resend_at = now + 2.0
                await asyncio.sleep(0.2)
                with self._data_lock:
                    last_id = self._charts[key]["last_id"]
                if last_id >= 0:
                    break
            if last_id < 0:
                raise TqClientError(f"K线数据初始化失败：{symbol} {period}s")
        with self._data_lock:
            buffer = self._charts[key]
            rows = dict(buffer["rows"])
            last_id = buffer["last_id"]
        need_from = max(0, last_id - fetch_length + 1)
        missing = [i for i in range(need_from, last_id + 1) if i not in rows]
        if missing:
            # 等待补齐（增量更新正在路上）
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and missing:
                await asyncio.sleep(0.1)
                with self._data_lock:
                    rows = dict(self._charts[key]["rows"])
                    last_id = self._charts[key]["last_id"]
                need_from = max(0, last_id - fetch_length + 1)
                missing = [i for i in range(need_from, last_id + 1) if i not in rows]
            if missing:
                raise TqClientError(f"K线数据不完整：{symbol} {period}s（缺 {len(missing)} 根）")
        bars: list[dict] = []
        for index in range(need_from, last_id + 1):
            bar = diff_auth.parse_kline_row(rows.get(index))
            if bar is not None:
                bars.append(bar)
        return bars[-count:]

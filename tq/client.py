"""TqSdk 连接层：唯一持有 TqApi 的客户端。

线程模型（关键）：
- TqApi 不是线程安全的，所有 TqSdk 调用（get_quote / query_symbol_info /
  wait_update / is_changing）都只在事件循环线程内发生。
- 其他线程通过命令队列提交请求并等待 Future 结果。
- 行情变化通过 ``on_quote_change`` 回调（在事件循环线程内同步调用）向外推送，
  由上层转发给缓存 / WebSocket，绝不让 HTTP/WS 层直接触碰 TqSdk 对象。

tqsdk 3.10 约定（已按源码核实）：
- ``wait_update(deadline=...)`` 的 deadline 是 **绝对时间**（time.time() 秒）。
- ``query_quotes`` / ``query_options`` 返回 SymbolList，``query_symbol_info``
  返回 TqSymbolDataFrame；两者都带 ``_task``，用 ``wait_update(_task=...)``
  等待查询结果落地。
"""

from __future__ import annotations

import asyncio
import math
import queue
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Optional

from market.processor import to_market_quote

FUTURE_SYMBOL_RE = re.compile(r"^[A-Z]+\.[a-z]{1,3}\d{3,4}$")

CATALOG_BATCH_SIZE = 50          # 目录批量查询每批合约数
CATALOG_BATCH_TIMEOUT = 25.0     # 单批查询超时（秒）
CATALOG_IDLE_INTERVAL = 2.0      # 目录无待查合约时的轮询间隔（秒）


class TqClientError(RuntimeError):
    """连接或命令执行失败（对外统一错误类型）。"""


class _Cmd:
    """队列命令对象：future 之外带 abandoned 标记。

    等待方超时后把命令标记为 abandoned，事件循环执行前跳过——
    避免"执行了也白执行"的命令把队列越堆越长（活锁根源之一）。
    """

    __slots__ = ("command", "args", "future", "abandoned")

    def __init__(self, command: str, args: tuple, future: Future) -> None:
        self.command = command
        self.args = args
        self.future = future
        self.abandoned = False


def _finite(value: Any) -> Optional[float]:
    """数值清洗：None / NaN / Inf 统一转 None（避免 JSON 序列化出 NaN）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class TqClient:
    READY_DELAY_SECONDS = 8.0  # 连接建立后的预热期：期间命令快速失败，避免排队超时

    def __init__(
        self,
        account: str,
        password: str,
        wait_deadline: float = 0.5,
    ) -> None:
        self._account = account
        self._password = password
        self._wait_deadline = max(0.05, min(5.0, wait_deadline))
        self._commands: "queue.Queue[_Cmd]" = queue.Queue()
        self._quotes: dict[str, Any] = {}
        self._kline_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        self._ready_since = 0.0
        self._error: Optional[str] = None
        self._last_status = ""
        self._reconnect_requested = False
        self._command_timeout_count = 0
        self._on_quote_change: Optional[Callable[[Any], None]] = None
        self._on_status: Optional[Callable[[str], None]] = None
        # 目录后台任务（B 路线）：协程跑在 TqApi 自己的事件循环上，逐批查询不阻塞主循环
        self._catalog_task: Optional[Any] = None
        self._catalog_needs_futures: Optional[Callable[[], bool]] = None
        self._catalog_on_futures: Optional[Callable[[list[str]], None]] = None
        self._catalog_next_batch: Optional[Callable[[], Optional[list[str]]]] = None
        self._catalog_on_result: Optional[Callable[[list[str], Optional[list[dict]]], None]] = None

    def set_callbacks(
        self,
        on_quote_change: Optional[Callable[[Any], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_quote_change = on_quote_change
        self._on_status = on_status

    def set_catalog_hooks(
        self,
        needs_futures: Callable[[], bool],
        on_futures: Callable[[list[str]], None],
        next_batch: Callable[[], Optional[list[str]]],
        on_result: Callable[[list[str], Optional[list[dict]]], None],
    ) -> None:
        """注册目录后台任务的钩子（由 InstrumentManager 提供，事件循环线程内回调）。

        - needs_futures：是否还没有期货代码列表（协程据此先查列表）；
        - on_futures：期货代码列表查询完成回写；
        - next_batch：返回下一批待查合约（≤50 个），无待查时返回 None；
        - on_result：批次查询完成（records 为 None 表示该批失败）。
        """
        self._catalog_needs_futures = needs_futures
        self._catalog_on_futures = on_futures
        self._catalog_next_batch = next_batch
        self._catalog_on_result = on_result

    # ------------------------------------------------------------------ 状态

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def ready(self) -> bool:
        """连接已建立且过了预热期（free-api 登录后需要数秒才响应请求）。"""
        with self._lock:
            return self._connected and time.monotonic() - self._ready_since >= self.READY_DELAY_SECONDS

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def account(self) -> str:
        return self._account

    @property
    def credentials_configured(self) -> bool:
        with self._lock:
            return bool(self._account and self._password)

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
        self._thread = threading.Thread(target=self._run, name="tqsdk-loop", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        self._stop.set()
        try:
            self._commands.put(_Cmd("close", (), Future()))
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def submit(self, command: str, *args: Any) -> _Cmd:
        """提交命令到事件循环线程；结果通过返回对象的 future 获取。"""
        cmd = _Cmd(command, args, Future())
        self._commands.put(cmd)
        return cmd

    def run_command(self, command: str, *args: Any, timeout: float = 8.0) -> Any:
        """提交并阻塞等待结果；失败抛出 :class:`TqClientError`。

        连接未就绪（未连接 / 预热期内）时快速失败，绝不排队等待超时。
        等待超时后命令被标记为 abandoned，事件循环将跳过执行（不再白占队列）。
        """
        if not self.ready:
            raise TqClientError("天勤连接初始化中，请稍候重试")
        cmd = self.submit(command, *args)
        try:
            return cmd.future.result(timeout=timeout)
        except TqClientError:
            raise
        except TimeoutError:
            cmd.abandoned = True
            self._note_command_timeout()
            raise TqClientError(f"命令超时（{timeout:g}s）：{command}") from None
        except Exception as error:
            raise TqClientError(str(error)) from error

    def _note_command_timeout(self) -> None:
        """连续命令超时说明连接已被服务器端挂起（如订阅了异常合约），触发强制重连。"""
        with self._lock:
            self._command_timeout_count += 1
            if self._command_timeout_count >= 3:
                self._command_timeout_count = 0
                self._reconnect_requested = True
                self._status("检测到连接异常（命令持续超时），准备重连")

    def set_credentials(self, account: str, password: str) -> None:
        """运行中设置天勤凭据（登录界面保存后调用），并触发重新登录。

        若事件循环线程尚未启动，凭据会在 start() 后的首次连接中使用。
        """
        with self._lock:
            self._account = account.strip()
            self._password = password
            self._error = None
        if self._thread is not None and self._thread.is_alive():
            self._reconnect_requested = True
            self._status("收到账户信息，正在重新登录")

    # ------------------------------------------------------------------ 主循环

    def _run(self) -> None:
        # 首次连接失败（如未配置凭据、网络未就绪）不退出线程：进入重试循环，
        # 等待 set_credentials/环境恢复后自动连上。
        api = None
        while api is None and not self._stop.is_set():
            try:
                api = self._connect()
            except Exception as error:
                self._set_error(str(error))
                self._status(f"天勤登录失败，5 秒后重试：{error}")
                self._fail_pending_commands(TqClientError(f"天勤未连接：{error}"))
                if self._stop.wait(5.0):
                    return
        self._set_error(None)
        self._set_connected(True)
        with self._lock:
            self._ready_since = time.monotonic()
            self._command_timeout_count = 0
        self._status("天勤已连接")
        self._ensure_catalog_worker(api)
        try:
            while not self._stop.is_set():
                if self._reconnect_requested:
                    self._reconnect_requested = False
                    self._status("连接异常，正在重建行情会话")
                    self._quotes.clear()
                    self._kline_cache.clear()
                    try:
                        api.close()
                    except Exception:
                        pass
                    try:
                        api = self._connect()
                    except Exception as error:
                        self._set_error(str(error))
                        self._status(f"重连失败：{error}，5 秒后重试")
                        time.sleep(5.0)
                        continue
                    self._set_error(None)
                    self._set_connected(True)
                    with self._lock:
                        self._ready_since = time.monotonic()
                        self._command_timeout_count = 0
                    self._status("天勤已重连")
                    self._ensure_catalog_worker(api)
                try:
                    api.wait_update(deadline=time.time() + self._wait_deadline)
                except Exception as error:
                    if self._stop.is_set():
                        break
                    # tqsdk 内置断线重连，标记状态后继续。
                    self._set_error(str(error))
                    self._status(f"连接异常，等待重连：{error}")
                    continue
                self._process_commands(api)
                self._dispatch_changes(api)
        finally:
            try:
                api.close()
            except Exception:
                pass
            self._set_connected(False)
            self._fail_pending_commands(TqClientError("天勤连接已关闭"))

    def _connect(self) -> Any:
        from tqsdk import TqApi, TqAuth  # 延迟导入：未安装 tqsdk 时网关仍可启动

        if not self._account or not self._password:
            raise TqClientError("未配置天勤账号/密码（config.json 或环境变量 TQ_ACCOUNT/TQ_PASSWORD）")
        self._status("天勤登录中")
        return TqApi(auth=TqAuth(self._account, self._password))

    def _process_commands(self, api: Any) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            if cmd.abandoned:
                continue  # 等待方已超时放弃：跳过执行，避免白占事件循环
            try:
                if cmd.command == "close":
                    cmd.future.set_result(None)
                    self._stop.set()
                    return
                cmd.future.set_result(self._execute(api, cmd.command, cmd.args))
            except Exception as error:
                cmd.future.set_exception(error)

    def _execute(self, api: Any, command: str, args: tuple) -> Any:
        if command == "subscribe":
            return self._subscribe(api, args[0])
        if command == "unsubscribe":
            self._quotes.pop(args[0], None)
            return None
        if command == "subscribed":
            return sorted(self._quotes)
        if command == "get_instrument":
            return self._get_instrument(api, args[0])
        if command == "get_instruments_info":
            return self._get_instruments_info(api, args[0])
        if command == "start_catalog_worker":
            self._ensure_catalog_worker(api)
            return True
        if command == "query_instruments":
            return self._query_instruments(api)
        if command == "query_options":
            return self._query_options(api, args[0])
        if command == "get_kline":
            return self._get_kline(api, args[0], int(args[1]), int(args[2]))
        raise TqClientError(f"未知命令：{command}")

    def _subscribe(self, api: Any, symbol: str) -> str:
        if symbol in self._quotes:
            return symbol
        quote = api.get_quote(symbol)
        if getattr(quote, "expired", False):
            raise TqClientError(f"合约已过期或不存在：{symbol}")
        self._quotes[symbol] = quote
        return symbol

    def _get_instrument(self, api: Any, symbol: str) -> dict:
        """合约信息查询：按交易所大小写规范生成候选变体，失败逐个重试。

        - 天勤服务器对合约代码大小写敏感（如 CFFEX.IF2609 / CZCE.SR609 必须大写）。
        - 不用 api.query_symbol_info 的同步版本：其内部 wait_update 无超时，
          服务器响应稍慢会无限阻塞事件循环线程；改为直接构造查询对象 +
          带 deadline 的 _wait_task。
        """
        from .instruments import build_symbol_variants
        from tqsdk.objs_not_entity import TqSymbolDataFrame  # 延迟导入

        exchange, instrument = symbol.split(".", 1)
        last_error: Optional[Exception] = None
        for candidate in build_symbol_variants(exchange, instrument):
            try:
                frame = TqSymbolDataFrame(api, [candidate], None)
                self._wait_task(api, frame, 8.0, f"合约信息查询超时：{candidate}")
                return _instrument_record(frame, candidate)
            except TqClientError:
                raise
            except Exception as error:  # graphql 查询失败会断连，等待重连后重试
                last_error = error
                try:
                    api.wait_update(deadline=time.time() + 1.0)
                except Exception:
                    pass
        raise TqClientError(f"合约不存在或查询失败：{symbol}"
                            + (f"（{last_error}）" if last_error is not None else ""))

    def _get_instruments_info(self, api: Any, symbols: list[str]) -> dict:
        """批量合约信息查询（每批 50 个，一次网络往返），失败批次降级单查。

        目录场景绝不能逐合约单查（933 个合约会把事件循环线程堵死）。
        """
        from tqsdk.objs_not_entity import TqSymbolDataFrame  # 延迟导入

        output: dict[str, dict] = {}
        for start in range(0, len(symbols), 50):
            batch = symbols[start:start + 50]
            try:
                frame = TqSymbolDataFrame(api, batch, None)
                self._wait_task(api, frame, 20.0, "合约信息批量查询超时")
                for index, symbol in enumerate(batch):
                    try:
                        output[symbol] = _instrument_record(frame, symbol, index=index)
                    except TqClientError:
                        continue
            except Exception:
                # 整批失败：少量降级单查（带大小写容错），其余跳过。
                for symbol in batch[:5]:
                    try:
                        output[symbol] = self._get_instrument(api, symbol)
                    except TqClientError:
                        continue
        return output

    def _query_instruments(self, api: Any) -> list[str]:
        query = getattr(api, "query_quotes", None)
        if query is None:
            raise TqClientError("TqSdk 无 query_quotes 接口，请检查 tqsdk 版本")
        try:
            symbols = query(ins_class="FUTURE", expired=False)
        except TypeError:
            symbols = None  # 旧版本签名：无参数，返回全部行情代码
        if symbols is None:
            symbols = query()
            self._wait_task(api, symbols, 15.0, "合约列表查询超时")
            symbols = [item for item in symbols if FUTURE_SYMBOL_RE.match(item)]
        else:
            self._wait_task(api, symbols, 15.0, "期货合约列表查询超时")
        # 只保留国内六大交易所；KQD 是外盘主连（如 KQD.m@CME.6J），国内评估器不需要。
        return sorted(symbol for symbol in symbols
                      if not symbol.upper().startswith("KQD."))

    def _query_options(self, api: Any, underlying: str) -> list[str]:
        symbols = api.query_options(underlying)
        self._wait_task(api, symbols, 15.0, f"期权查询超时：{underlying}")
        return sorted(symbols)

    def _get_kline(self, api: Any, symbol: str, period: int, count: int) -> list[dict]:
        """拉取 K 线（带 5 秒缓存，避免前端轮询打爆事件循环线程）。

        - 内部统一取 400 根：tqsdk 按 (合约, 周期, 长度) 复用 serial，长度一致才
          不会为同一周期创建多个永不释放的数据通道。
        - get_kline_serial 必须传 deadline：其内部会 wait_update 等待初始化，
          不传时服务器不响应会无限阻塞事件循环线程。
        - 初始化失败会留下"坏 serial"并被 tqsdk 永久复用（该周期永远失败），
          此时清理内部注册并按不同长度重建，最多重试 3 次。
        """
        key = (symbol, period)
        now = time.time()
        cached = self._kline_cache.get(key)
        if cached is not None and now - cached[0] < 5.0:
            return cached[1][-count:]
        last_error: Optional[Exception] = None
        for attempt, fetch_length in enumerate((400, 401, 402)):
            try:
                serial = api.get_kline_serial(symbol, period, fetch_length,
                                              deadline=time.time() + 15.0)
            except Exception as error:
                last_error = error
                continue
            deadline = time.time() + 20.0
            while time.time() < deadline and not self._stop.is_set():
                try:
                    ready = api.is_serial_ready(serial)
                except Exception:
                    ready = False
                if ready and len(serial):
                    break
                api.wait_update(deadline=time.time() + 1.0)
            if len(serial):
                break
            # 清理 tqsdk 内部对该请求的注册，避免坏 serial 被永久复用。
            self._drop_stale_kline_registration(api, symbol, period, fetch_length)
            last_error = TqClientError(f"K线数据初始化失败：{symbol} {period}s（尝试 {attempt + 1}）")
        else:
            raise TqClientError(f"K线数据获取失败：{symbol} {period}s"
                                + (f"（{last_error}）" if last_error is not None else ""))
        bars: list[dict] = []
        for _, row in serial.iterrows():
            close = row["close"]
            if close is None:
                continue  # 无价格不成 K 线（部分免费行情字段可能缺失）
            bar: dict = {
                "datetime": int(float(row["datetime"]) / 1e6),  # 纳秒 -> 毫秒
                "open": _finite(row["open"]),
                "high": _finite(row["high"]),
                "low": _finite(row["low"]),
                "close": float(close),
                "volume": float(row["volume"]) if _finite(row["volume"]) is not None else 0.0,
                "open_interest": _finite(row.get("close_oi", None)),
            }
            if bar["open"] is None or bar["high"] is None or bar["low"] is None:
                continue
            bars.append(bar)
        self._kline_cache[key] = (time.time(), bars)
        return bars[-count:]

    @staticmethod
    def _drop_stale_kline_registration(api: Any, symbol: str, period: int, fetch_length: int) -> None:
        """删除 tqsdk 内部对失败 K 线请求的注册（_requests/_serials），强制下次重建。

        访问内部 dict 是 tqsdk 未公开机制，仅用于清理卡死的 serial；失败时静默忽略。
        """
        try:
            request = (tuple(symbol), period, fetch_length, None)
            serial = api._requests["klines"].get(request)  # type: ignore[attr-defined]
            if serial is not None:
                api._serials.pop(id(serial["df"]), None)  # type: ignore[attr-defined]
                api._requests["klines"].pop(request, None)  # type: ignore[attr-defined]
        except Exception:
            pass

    # ------------------------------------------------------- 目录后台协程（B 路线）

    def _ensure_catalog_worker(self, api: Any) -> None:
        """在 TqApi 事件循环上挂目录后台协程（每条连接只挂一次）。

        官方模式：api.create_task 创建的协程由主循环 wait_update 驱动，
        每批查询用 await 等待，等待期间 K 线/订阅/行情推送照常处理，
        彻底避免目录查询独占事件循环。
        """
        if self._catalog_task is not None and not self._catalog_task.done():
            return
        if self._catalog_next_batch is None or self._catalog_on_result is None:
            return
        self._catalog_task = api.create_task(self._catalog_worker(api))

    async def _catalog_worker(self, api: Any) -> None:
        """目录后台协程：先查期货代码列表，再逐批查询合约信息。

        绝不在协程内调用阻塞版 wait_update（会卡死事件循环），
        统一用 asyncio.wait_for + shield 等待 tqsdk 内部任务。
        """
        from tqsdk.objs_not_entity import TqSymbolDataFrame  # 延迟导入：未安装 tqsdk 时网关仍可启动

        while not self._stop.is_set():
            if self._catalog_needs_futures is not None and self._catalog_needs_futures():
                try:
                    symbols = await self._await_futures_query(api)
                    self._catalog_on_futures(symbols)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(5.0)
                    continue
            batch = self._catalog_next_batch()
            if not batch:
                try:
                    await asyncio.sleep(CATALOG_IDLE_INTERVAL)
                except asyncio.CancelledError:
                    raise
                continue
            try:
                frame = TqSymbolDataFrame(api, batch, None)
                await self._await_frame(api, frame, CATALOG_BATCH_TIMEOUT)
                records = []
                for index, symbol in enumerate(batch):
                    try:
                        records.append(_instrument_record(frame, symbol, index=index))
                    except TqClientError:
                        records.append(None)
                self._catalog_on_result(batch, records)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._catalog_on_result(batch, None)  # 整批失败：交给上层冷却
            await asyncio.sleep(0.05)

    async def _await_futures_query(self, api: Any) -> list[str]:
        """协程版期货代码列表查询（等价于 _query_instruments 的阻塞版）。"""
        query = getattr(api, "query_quotes", None)
        if query is None:
            raise TqClientError("TqSdk 无 query_quotes 接口，请检查 tqsdk 版本")
        try:
            symbols = query(ins_class="FUTURE", expired=False)
        except TypeError:
            symbols = None  # 旧版本签名：无参数，返回全部行情代码
        if symbols is None:
            symbols = query()
            await self._await_query_task(symbols, 15.0)
            raw = list(symbols)
            return sorted(item for item in raw if FUTURE_SYMBOL_RE.match(item))
        await self._await_query_task(symbols, 15.0)
        # 只保留国内六大交易所；KQD 是外盘主连（如 KQD.m@CME.6J），国内评估器不需要。
        return sorted(symbol for symbol in symbols
                      if not symbol.upper().startswith("KQD."))

    async def _await_query_task(self, query_object: Any, timeout: float) -> None:
        """等待 query 系列接口的内部任务完成（shield 防止超时取消 tqsdk 任务）。"""
        task = getattr(query_object, "_task", None)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TypeError:
            # _task 不是可 await 对象（异常版本）：交给上层按超时失败处理
            raise TqClientError("query 任务对象不可等待") from None
        if hasattr(task, "done") and not task.done():
            raise TqClientError("期货代码列表查询超时")

    @staticmethod
    async def _await_frame(api: Any, frame: Any, timeout: float) -> None:
        """等待 TqSymbolDataFrame 内部任务完成（shield 防止超时取消 tqsdk 任务）。"""
        task = getattr(frame, "_task", None)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TypeError:
            # _task 不是可 await 对象（异常版本）：退化为更新通知轮询
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            async with api.register_update_notify() as channel:
                async for _ in channel:
                    if task.done() or loop.time() > deadline:
                        break
        if hasattr(task, "done") and not task.done():
            raise TqClientError("目录批量查询超时")
        if hasattr(task, "exception") and task.done():
            error = task.exception()
            if error is not None:
                raise TqClientError(f"目录批量查询失败：{error}")

    def _wait_task(self, api: Any, query_object: Any, timeout: float, message: str) -> None:
        """等待 query 系列接口的结果落地（内部任务完成）。"""
        task = getattr(query_object, "_task", None)
        if task is None:
            return
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set() and not task.done():
            api.wait_update(deadline=deadline)
        if not task.done():
            raise TqClientError(message)

    def _dispatch_changes(self, api: Any) -> None:
        for symbol, quote in list(self._quotes.items()):
            try:
                if not api.is_changing(quote):
                    continue
            except Exception:
                continue
            market_quote = to_market_quote(symbol, quote)
            if market_quote is not None and self._on_quote_change is not None:
                self._on_quote_change(market_quote)

    def _fail_pending_commands(self, error: Exception) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            if not cmd.future.done():
                cmd.future.set_exception(error)


def _row_value(row: Any, key: str) -> Any:
    """DataFrame 行的取值：nan / None 统一转成 None。"""
    value = row.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value if value == value else None  # 非数值原样返回（如名称）
    return None if not math.isfinite(number) else number


def _instrument_record(frame: Any, symbol: str, index: int = 0) -> dict:
    """把 query_symbol_info 的 DataFrame 行转成纯 dict（在事件循环线程内完成）。

    天勤返回的 instrument_id 有时已带交易所前缀（如 ``CFFEX.IC2608``、
    ``KQD.m@NYMEX.RB``），拼接 symbol 前去重，避免 ``CFFEX.CFFEX.IC2608``。
    """
    if len(frame) <= index:
        raise TqClientError(f"合约不存在：{symbol}")
    row = frame.iloc[index]
    instrument_id = str(_row_value(row, "instrument_id") or "")
    exchange = str(_row_value(row, "exchange_id") or "").upper()
    if not instrument_id or not exchange:
        raise TqClientError(f"合约不存在：{symbol}")
    if instrument_id.upper().startswith(exchange + "."):
        instrument_id = instrument_id[len(exchange) + 1:]
    price_tick = _row_value(row, "price_tick")
    volume_multiple = _row_value(row, "volume_multiple")
    expire_rest_days = _row_value(row, "expire_rest_days")
    return {
        "symbol": f"{exchange}.{instrument_id}",
        "exchange": exchange,
        "instrument_id": instrument_id,
        "name": str(_row_value(row, "instrument_name") or ""),
        "kind": str(_row_value(row, "ins_class") or "FUTURE").upper(),
        "expired": bool(_row_value(row, "expired") or False),
        "price_tick": float(price_tick) if price_tick is not None else None,
        "volume_multiple": int(volume_multiple) if volume_multiple is not None else None,
        "expire_rest_days": int(expire_rest_days) if expire_rest_days is not None else None,
    }

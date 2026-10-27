"""DIFF 协议行情客户端（C 路线核心）。

架构：
- 独立线程跑 asyncio 事件循环：认证/合约文件（阻塞 HTTP，executor 执行）→
  WebSocket 长连接（接收协程）→ 全部命令以协程并发执行。
- 与旧 TqClient 的命令接口完全一致（subscribe / unsubscribe / subscribed /
  get_instrument / get_instruments_info / query_instruments / query_options /
  get_kline），上层 tq/instruments、tq/subscriber、api/ 零改动。
- 合约按用户输入向天勤 GraphQL 查询，不在启动时下载完整静态目录；
  K 线通过 set_chart 异步到达，命令只做"发一条 ws 消息 + 等本地事件"，
  因此不存在队列堵塞 / 活锁的土壤。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Callable, Optional
from types import SimpleNamespace

from market.processor import to_market_quote
from . import auth as diff_auth
from .search import FUTURE_SYMBOL_RE, SymbolSearch, build_symbol_query

EXCHANGE_INSTRUMENT_CASE = {
    "SHFE": "lower", "DCE": "lower", "INE": "lower", "GFEX": "lower",
    "CZCE": "upper", "CFFEX": "upper",
}

logger = logging.getLogger("gateway.tqdiff")
# K 线结构化诊断开关（P0-HISTORY_EMPTY 定位）：启动前设置 TQ_GATEWAY_DEBUG=1 开启
KLINE_DEBUG = os.getenv("TQ_GATEWAY_DEBUG", "") == "1"


class TqClientError(RuntimeError):
    """连接或命令执行失败（与旧 TqClient 对外一致的错误类型）。"""


class SymbolNotFoundError(TqClientError):
    """合约目录已就绪，但查不到该合约。

    与"目录未就绪"（普通 TqClientError）区分开：订阅层据此决定
    严格拒绝（查无此合约）还是降级放行（目录还没下载完）。
    """


def _candidates(symbol: str) -> list[str]:
    """生成同一合约在各交易所规范写法下的候选代码列表（按优先级排序、去重）。

    静态合约文件里的键使用各交易所的规范大小写（如 SHFE/DCE 用小写月份代码、
    CZCE/CFFEX 用大写），用户输入可能大小写随意，因此按交易所规则生成候选，
    依次尝试查表直到命中。

    Args:
        symbol: 用户输入的合约代码，形如 "SHFE.rb2610"；可不含交易所前缀之外的修饰。

    Returns:
        候选代码列表（至少包含原始输入本身）；空输入返回空列表。
        例：("czce.SR609") → ["czce.SR609", "CZCE.SR609"]（CZCE 规范为大写）。
    """
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
    supports_symbol_search = True

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
        self._on_connected: Optional[Callable[[], None]] = None
        self._catalog_progress: Optional[str] = None   # 合约目录下载进度（如 "12MB/256MB"）
        # 事件循环与连接
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._token: Optional[str] = None
        self._file_task: Optional[asyncio.Task] = None
        # threading.Event：跨线程/跨事件循环都安全（asyncio.Event 会绑定创建它的 loop）
        self._file_loaded = threading.Event()
        # 可搜索与完整可校验是不同状态：内置表/增量解析只能前者。
        self._catalog_complete = threading.Event()
        # 数据状态（仅事件循环协程内读写；快照读取加 _data_lock）
        self._data_lock = threading.Lock()
        self._symbol_file: dict[str, Any] = {}
        self._quotes: dict[str, dict] = {}
        self._quote_events: dict[str, asyncio.Event] = {}
        self._subscribed: set[str] = set()
        self._charts: dict[tuple[str, int], dict] = {}  # (symbol, dur_ns) -> 缓冲
        self._symbol_queries: dict[str, dict] = {}
        self._symbol_search = SymbolSearch(self._query_symbols)

    # ------------------------------------------------------------ 状态与回调

    def set_callbacks(
        self,
        on_quote_change: Optional[Callable[[Any], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_connected: Optional[Callable[[], None]] = None,
    ) -> None:
        """注册三个来自行情线程的回调（均须线程安全、绝不能抛异常阻塞行情）。

        Args:
            on_quote_change: 每收到一笔合并后的报价回调，参数为 market.MarketQuote 实例
                （由 to_market_quote 转换），驱动缓存与前端 WS 广播。
            on_status: 人类可读的状态文本变化回调（"连接中"/"已连接"/异常信息等），
                同文本只回调一次，用于界面状态栏展示。
            on_connected: 每次 WebSocket 建立成功（含断线重连）后回调，供订阅管理器
                重放挂起的订阅（缺陷 A/F 配套）。
        """
        self._on_quote_change = on_quote_change
        self._on_status = on_status
        self._on_connected = on_connected

    @property
    def connected(self) -> bool:
        """WebSocket 是否处于已连接状态（线程安全读）。"""
        with self._lock:
            return self._connected

    @property
    def ready(self) -> bool:
        """兼容旧 TqClient 的别名：DIFF 路线连接即就绪，无额外预热期。"""
        return self.connected

    @property
    def error(self) -> Optional[str]:
        """最近一次连接/会话的错误文本；正常连接中为 None（线程安全读）。"""
        with self._lock:
            return self._error

    @property
    def account(self) -> str:
        """当前生效的天勤账号（登录界面回显用，已去除首尾空白）。"""
        return self._account

    @property
    def credentials_configured(self) -> bool:
        """账号与密码是否都已配置（决定登录页是否需要弹出）。"""
        with self._lock:
            return bool(self._account and self._password)

    @property
    def catalog_ready(self) -> bool:
        """连接成功即可按需搜索，无需等待整份目录。"""
        return self.connected

    @property
    def catalog_loading(self) -> bool:
        """表示实际进行中的合约查询，空闲时不会显示“完整目录下载中”。"""
        return bool(self._symbol_queries)

    @property
    def known_futures_count(self) -> int:
        """本次会话查询过的合约数；状态轮询只读内存，不触发合约查询。"""
        with self._data_lock:
            return len(self._symbol_file)

    @property
    def catalog_complete(self) -> bool:
        """完整目录是否已加载，可据此严格判定本地未命中。"""
        return self._catalog_complete.is_set()

    @property
    def catalog_progress(self) -> Optional[str]:
        """合约目录下载进度文本（如 "12MB/256MB"）；未下载/就绪时可为 None。"""
        with self._lock:
            return self._catalog_progress

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            self._connected = value

    def _set_error(self, value: Optional[str]) -> None:
        with self._lock:
            self._error = value

    def _status(self, value: str) -> None:
        if value != self._last_status and self._on_status is not None:
            self._last_status = value
            try:
                self._on_status(value)
            except Exception:
                pass   # 状态回调绝不允许拖垮行情线程

    def _set_catalog_progress(self, value: str) -> None:
        """记录合约目录下载进度（线程安全）。"""
        with self._lock:
            self._catalog_progress = value

    # ------------------------------------------------------------ 线程安全接口

    def start(self) -> None:
        """启动行情线程（幂等：线程已存活时直接返回）。

        线程内跑独立 asyncio 事件循环并进入"会话-断开-重连"主循环，
        直到 close() 被调用。daemon=True：主进程退出无需显式收尾。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="diff-loop", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        """停止行情线程并释放事件循环。

        置位停止信号 → 唤醒阻塞中的事件循环 → 等待线程退出（至多 timeout 秒）。
        线程未在超时内退出也不强杀（daemon 线程随进程消亡）。
        """
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def run_command(self, command: str, *args: Any, timeout: float = 8.0) -> Any:
        """提交一条命令协程到行情事件循环并同步等待结果（跨线程唯一通用入口）。

        接口与旧 TqClient 完全一致，上层（tq/instruments、tq/subscriber、api/）
        无需感知底层是 TqSdk 还是 DIFF 直连。

        Args:
            command: 命令名，见 _execute 支持的分发表。
            *args: 命令参数（与 _execute 分发处的解包顺序一一对应）。
            timeout: 同步等待上限（秒）；超时会取消远端协程。

        Returns:
            命令协程的返回值（类型随命令而异）。

        Raises:
            TqClientError: 连接未就绪、命令超时或命令执行失败（统一错误类型）。
        """
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

    def queue_subscription(self, symbol: str) -> None:
        """登记订阅并异步重放，不等待行情循环处理完成。

        首次下载和解析完整合约目录会持续占用 Python 运行时。目录尚未完整时，
        订阅不能因此阻塞 HTTP 请求；状态先写入重连可重放的集合，行情循环恢复
        调度后再发送 subscribe_quote。
        """
        with self._data_lock:
            self._subscribed.add(symbol)
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._resend_subscribe(), loop)

    def discard_subscription(self, symbol: str) -> None:
        """断线期间取消本地订阅意图，重连时只重放仍然需要的合约。"""
        with self._data_lock:
            self._subscribed.discard(symbol)
            self._quotes.pop(symbol, None)
            for key in [key for key in self._charts if key[0] == symbol]:
                self._charts.pop(key, None)

    def set_credentials(self, account: str, password: str) -> None:
        """运行中设置天勤凭据（登录界面保存后调用）。

        会话重连循环（_run_loop）每次尝试都会读取最新凭据，无需额外触发；
        若当前已连接且账号发生变化，主动断开当前 WebSocket 以强制用新凭据重新登录。
        """
        self._account = account.strip()
        self._password = password
        self._error = None
        loop, ws = self._loop, self._ws
        if loop is not None and loop.is_running() and ws is not None:
            self._status("收到账户信息，正在重新登录")
            asyncio.run_coroutine_threadsafe(self._force_disconnect(), loop)

    async def _force_disconnect(self) -> None:
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 主循环

    def _run_loop(self) -> None:
        """行情线程主函数：创建事件循环并驱动"会话-断开-重连"循环。

        _session() 正常返回（服务器主动断开）→ 提示后 3 秒重连；
        _session() 抛异常（登录失败/网络断开等）→ 记录错误后 3 秒重连。
        凭据每次重连都重新读取，因此 set_credentials 后无需额外触发。
        """
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
            # 缺陷 F（桌面/手机同病）：服务器断开即丢弃本端全部订阅与图表状态，
            # 重连后若不重放，会出现"连接恢复但行情推送冻结"（K 线拉取正常、
            # 缓存报价停在断网前旧值）。这里在每次连接建立后按订阅表/图表缓冲重放。
            await self._resend_subscribe()
            await self._resend_charts()
            # 回调会提交同步 run_command；放到工作线程，避免行情循环等待自己而超时。
            self._loop.run_in_executor(None, self._notify_connected)
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
        """兼容旧启动入口：标记按需查询可用，不读取旧目录、不下载完整文件。"""
        self._file_loaded.set()
        self._catalog_complete.clear()
        self._set_catalog_progress(None)

    async def _query_symbols(self, filters: dict) -> list[dict]:
        """通过既有 DIFF 连接发出一次 GraphQL 查询，收到答复后释放服务端查询。

        每次请求有独立 ID 和事件；行情和其他品种查询可以同时进行。
        超时/服务端错误向上抛，只有成功返回空列表才表示合约不存在。
        """
        if self._ws is None:
            raise TqClientError("天勤合约服务未连接，请稍候重试")
        query_id = "ZA_search_" + uuid.uuid4().hex
        pending = {"ready": asyncio.Event(), "data": {}}
        self._symbol_queries[query_id] = pending
        try:
            query = build_symbol_query(filters)
            await asyncio.wait_for(self._send({"aid": "ins_query", "query_id": query_id,
                                              "query": query, "variables": filters}), timeout=1.0)
            await asyncio.wait_for(pending["ready"].wait(), timeout=4.0)
            response = pending["data"]
            if response.get("error"):
                # 保留可诊断信息供日志排查；查询条件只有品种/代码，不包含账号或令牌。
                # 界面保留可读提示，不把 GraphQL 语句和内部字段堆给用户。
                detail = str(response["error"]).replace("\r", " ").replace("\n", " ")[:600]
                logger.warning("[contract-search] query_id=%s filters=%s upstream_error=%s",
                               query_id, filters, detail)
                raise TqClientError("天勤合约查询失败，请稍后重试")
            rows = (response.get("result") or {}).get("multi_symbol_info")
            if not isinstance(rows, list):
                raise TqClientError("天勤合约服务返回格式异常，请重试")
            return [row for row in rows if isinstance(row, dict)]
        except asyncio.TimeoutError:
            raise TqClientError("天勤合约查询超时，请重试或输入完整品种代码") from None
        finally:
            self._symbol_queries.pop(query_id, None)
            # 空 query 是官方 SDK 使用的释放协议；搜索不会留下持续查询/行情订阅。
            if self._ws is not None:
                try:
                    await asyncio.wait_for(self._send({"aid": "ins_query", "query_id": query_id,
                                                       "query": "", "variables": {}}), timeout=0.5)
                except Exception:
                    pass

    async def _send(self, pack: dict) -> None:
        assert self._send_lock is not None
        async with self._send_lock:
            if self._ws is not None:
                await self._ws.send(json.dumps(pack, ensure_ascii=False))

    # ------------------------------------------------------------ 数据合并

    def _apply_diff(self, diff: dict) -> None:
        """把一条 DIFF 数据包合并进本地状态（报价字典 + K 线图表缓冲）。

        DIFF 协议的推送是"增量补丁"：quotes 按 symbol 给变化的字段，
        klines 按 symbol→duration→图表数据三层组织。合并语义（update 而非
        整体替换）是正确性的关键——增量只带变化字段，替换会丢数据。

        Args:
            diff: 服务器推送的单条数据包，形如
                {"quotes": {symbol: {field: value}}, "klines": {...}}，两键均可缺省。
        """
        # ins_query 返回的是独立的 symbols 数据，不得当作行情订阅或写入价格缓存。
        for query_id, patch in (diff.get("symbols") or {}).items():
            pending = self._symbol_queries.get(query_id)
            if pending is not None and isinstance(patch, dict):
                pending["data"].update(patch)
                if "result" in patch or patch.get("error"):
                    pending["ready"].set()

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
        """把一根/一批 K 线增量合并进对应图表缓冲，并唤醒等待者。

        处理三类内容：
        - data：行号→行数据（"@"键为锚点，跳过）；已有行做字段合并（增量语义），
          新行直接写入；
        - last_id：服务器最新行号（本地行号水位线），用于判定补齐进度；
        - ready：首次确认图表后置位，唤醒 set_chart 后的等待协程。

        Args:
            symbol: 合约代码（klines 增量第一层键就是合约代码，非 chart_id）。
            dur_ns: K 线周期（纳秒），与 symbol 共同定位图表缓冲。
            chart_data: 该合约该周期的增量数据包。
        """
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
                        row_id = int(index)
                    except (TypeError, ValueError):
                        continue
                    existing = buffer["rows"].get(row_id)
                    if isinstance(existing, dict) and isinstance(row, dict):
                        # DIFF 增量只送"变化的字段"（与 quotes 的 update 同语义）：必须合并。
                        # 整体替换会把正在形成的 K 线冲掉部分字段（盘中增量常只有 close/volume），
                        # 该根 K 线因缺 open/low 解析失败从图上消失，直到重启重新拉全量才恢复。
                        existing.update(row)
                    else:
                        buffer["rows"][row_id] = row
                anchor = data.get("@")
            else:
                anchor = None
            last_id = chart_data.get("last_id", anchor)
            if last_id is not None:
                try:
                    buffer["last_id"] = int(last_id)
                except (TypeError, ValueError):
                    pass
            # 淘汰过老的行（最多保留 max(buffer["view_width"] * 2, 1000) 根，防止无限累加）
            if len(buffer["rows"]) > 1500:
                cutoff = buffer["last_id"] - max(buffer.get("view_width", 400) + 100, 800)
                for old_id in [k for k in buffer["rows"] if k < cutoff]:
                    buffer["rows"].pop(old_id, None)
            buffer["ready"].set()

    def _dispatch_quote(self, symbol: str, snapshot: dict) -> None:
        """把合并后的报价快照推向两个出口：本地等待事件 + 上层回调。

        - 若有协程正在等该合约的首笔报价（_quote_events），置位唤醒它；
        - 无最新价（休市初始化包）则跳过回调；datetime 为纳秒时转毫秒；
        - 经 to_market_quote 转成 market.MarketQuote 后触发 on_quote_change，
          由外层写缓存并广播给前端。

        Args:
            symbol: 合约代码。
            snapshot: 合并后的报价快照（data_lock 内拷贝出的独立副本）。
        """
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
        """命令分发表：把 run_command 的命令名路由到对应协程实现。

        支持的命令（与旧 TqClient 对齐）：
            subscribe/unsubscribe/subscribed —— 行情订阅管理；
            get_instrument/get_instruments_info/query_instruments —— 合约目录查询；
            get_kline —— K 线查询；query_options —— C 路线未实现，明确报错。

        Args:
            command: 命令名。
            args: 参数元组（按各命令实现的解包顺序）。

        Returns:
            各命令协程的返回值。

        Raises:
            TqClientError: 未知命令。
        """
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
        if command == "search_instruments":
            records = await self._symbol_search.search(*args)
            with self._data_lock:
                self._symbol_file.update({row["symbol"]: row for row in records})
            return records
        if command == "query_options":
            raise TqClientError("C 直连路线暂未实现期权查询")
        if command == "start_catalog_worker":
            return True  # 按需模式无后台全量目录任务
        if command == "get_kline":
            return await self._get_kline(args[0], int(args[1]), int(args[2]))
        raise TqClientError(f"未知命令：{command}")

    def _file_entry(self, symbol: str) -> Optional[dict]:
        """按大小写候选在目录里查找合约的精简 record。

        Args:
            symbol: 任意大小写的合约代码。

        Returns:
            命中的精简 record（含 symbol/exchange/name/price_tick 等）；未命中返回 None。
        """
        with self._data_lock:
            file_data = self._symbol_file
        for candidate in _candidates(symbol):
            entry = file_data.get(candidate)
            if isinstance(entry, dict) and entry:
                return entry
        return None

    async def _resend_subscribe(self) -> None:
        """把当前订阅表整体重发给服务器（subscribe_quote）。

        服务器端订阅是全量语义：每次重发完整 ins_list（逗号分隔）即可完成
        增/删订阅与断线重放，无需本地记录与服务器侧的差量。空表也发送以取消全部订阅。
        """
        with self._data_lock:
            ins_list = ",".join(sorted(self._subscribed))
        pack = {"aid": "subscribe_quote", "ins_list": ins_list}
        # 空 ins_list 表示取消全部行情订阅，必须发送，否则服务端会保留最后一只。
        await self._send(pack)

    async def _resend_charts(self) -> None:
        """重连后重放所有 set_chart：服务器断开即丢弃图表状态，不重放则 K 线停止刷新。

        缺陷 F 配套：仅 subscribe_quote 重放不够——set_chart 丢失后服务器不再推 K 线
        增量，页面看起来"连着"但图不动。重发用与首次相同的 chart_id，服务器据此
        恢复推送；last_id 清零让它从最新可见位置重新发全量。
        """
        with self._data_lock:
            charts = [(key, buf) for key, buf in self._charts.items()]
        for (symbol, dur_ns), buffer in charts:
            chart_id = f"ZAQ_{abs(hash((symbol, dur_ns))) % 10**10}"
            await self._send({
                "aid": "set_chart", "chart_id": chart_id,
                "ins_list": symbol, "duration": dur_ns,
                "view_width": buffer.get("view_width", 400),
            })

    def _notify_connected(self) -> None:
        """连接建立（含重连）后回调：供订阅管理器重放挂起的订阅（缺陷 A/F）。"""
        if self._on_connected is not None:
            try:
                self._on_connected()
            except Exception:
                pass   # 回调绝不允许拖垮行情线程


    async def _subscribe(self, symbol: str) -> str:
        """订阅一只合约的行情推送，返回规范化后的合约代码。

        订阅完全不依赖合约目录文件（只依赖规范代码）：
        - 目录已就绪且命中 → 顺便校验过期（过期即拒绝），取规范代码；
        - 目录未就绪或未命中 → 用 _candidates 规范化后直接订阅，
          行情服务器会校验合约是否存在——避免"目录下载中"阻塞订阅、拖慢首笔价格。
        订阅成功后主动重放本地已有快照：休市时段服务器不再推 diffs，
        这样页面订阅后立刻有行情可见。

        Args:
            symbol: 用户输入的合约代码（大小写随意）。

        Returns:
            规范化后的合约代码（如 "SHFE.rb2610"）。

        Raises:
            TqClientError: 目录判定合约已过期，或输入无法规范化。
        """
        # 订阅行情完全不依赖合约目录文件（只依赖规范代码）。
        # 目录是否就绪不影响订阅：就绪则顺便校验过期/名称，未就绪直接用 _candidates 规范化订阅，
        # 行情服务器会校验合约是否存在并推送行情——避免"目录下载中"阻塞订阅、拖慢价格出现。
        have_file = self._file_loaded.is_set()
        record = self._file_entry(symbol) if have_file else None
        if have_file and record is not None:
            if record.get("expired"):
                raise TqClientError(f"合约已过期或不存在：{symbol}")
        if record is not None:
            # _symbol_file 存的是精简 record，直接取规范代码
            canonical = record["symbol"]
        else:
            # 目录未就绪或目录里没有该条目：用 _candidates 规范化代码直接订阅
            candidates = _candidates(symbol)
            if not candidates or not candidates[0]:
                raise TqClientError(f"合约不存在或查询失败：{symbol}")
            canonical = candidates[0]
        if canonical not in self._subscribed:
            with self._data_lock:
                self._subscribed.add(canonical)
            await self._resend_subscribe()
        # 休市时段服务器不再推 diffs：主动重放当前快照，保证页面订阅后立刻有行情
        with self._data_lock:
            snapshot = dict(self._quotes.get(canonical) or {})
        if snapshot:
            self._dispatch_quote(canonical, snapshot)
        return canonical

    async def _unsubscribe(self, symbol: str) -> None:
        """退订一只合约：从订阅表移除并清掉其报价快照与等待事件，重发订阅表。"""
        with self._data_lock:
            self._subscribed.discard(symbol)
            self._quotes.pop(symbol, None)
            self._quote_events.pop(symbol, None)
            removed_charts = [ck for ck in self._charts if ck[0] == symbol]
            for k in removed_charts:
                self._charts.pop(k, None)
        # 删除本地缓冲不会停止服务端图表推送，须显式清空每个 chart 的 ins_list。
        for key in removed_charts:
            await self._send({"aid": "set_chart", "chart_id": f"ZAQ_{abs(hash(key)) % 10**10}",
                              "ins_list": "", "duration": key[1]})
        await self._resend_subscribe()
        return None

    async def _get_instrument(self, symbol: str) -> dict:
        """按代码向天勤确认真实挂牌信息；本地缺记录不再误判成不存在。"""
        record = await self._symbol_search.lookup(symbol)
        if record is None:
            raise SymbolNotFoundError(f"合约不存在或已到期：{symbol}")
        with self._data_lock:
            self._symbol_file[record["symbol"]] = dict(record)
        self._file_loaded.set()
        return record

    async def _get_instruments_info(self, symbols: list[str]) -> dict:
        """兼容批量元数据接口，只查询调用方明确要求的代码。"""
        if not symbols:
            return {}
        if len(symbols) > 300:
            raise TqClientError("单次合约查询最多 300 个代码")
        records = await self._symbol_search.lookup_many(symbols)
        with self._data_lock:
            self._symbol_file.update({row["symbol"]: row for row in records})
        return {row["symbol"]: row for row in records}

    async def _query_instruments(self) -> list[str]:
        """旧接口仅返回已查询的期货记录；不会触发完整目录下载。"""
        with self._data_lock:
            return sorted(key for key, record in self._symbol_file.items()
                          if record.get("kind") == "FUTURE" and not record.get("expired")
                          and FUTURE_SYMBOL_RE.fullmatch(key))

    async def _get_kline(self, symbol: str, period: int, count: int) -> list[dict]:
        """拉取一只合约最近 count 根 K 线（不依赖合约目录，目录未就绪也可用）。

        流程：
        1. 定位/创建该 (symbol, duration) 的图表缓冲；无水位线（last_id<0）说明
           服务器尚未确认图表——注意合约服务就绪前发的 set_chart 会被前置丢弃，
           因此每 2 秒重发一次 set_chart，直到返回 last_id（实测 2~6 秒内就绪）；
        2. 检查 [last_id-fetch_length+1, last_id] 区间内的缺行，等增量补齐
           （最多 10 秒），仍缺则报"数据不完整"；
        3. 逐行经 parse_kline_row 解析成标准 K 线 dict；服务器确认了图表却无任何
           有效行时，明确抛"历史不可用"，绝不允许上层用快照/模拟数据顶替历史。

        Args:
            symbol: 合约代码。
            period: K 线周期（秒），如 60/300/86400。
            count: 需要的根数；实际拉取量至少 400 根（为增量合并留缓冲）。

        Returns:
            升序的标准 K 线列表（至多 count 根），每根含
            datetime(ms)/open/high/low/close/volume/open_interest。

        Raises:
            TqClientError: 图表初始化超时、数据不完整或历史不可用。
        """
        # K 线不依赖合约目录文件：目录还在后台下载时也应可用。
        # 诊断（P0-HISTORY_EMPTY 定位）：TQ_GATEWAY_DEBUG=1 时输出请求ID/等待/行数/异常的结构化日志。
        req_id = self._kline_seq = getattr(self, "_kline_seq", 0) + 1
        t0 = time.monotonic()
        if KLINE_DEBUG:
            logger.info("[kline #%d] start symbol=%s period=%ds count=%d",
                        req_id, symbol, period, count)
        canonical = symbol
        dur_ns = period * 1_000_000_000
        fetch_length = max(count, 400)
        key = (canonical, dur_ns)
        chart_id = f"ZAQ_{abs(hash(key)) % 10**10}"
        pack = {
            "aid": "set_chart",
            "chart_id": chart_id,
            "ins_list": canonical,
            "duration": dur_ns,
            "view_width": fetch_length,
        }
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
                needs_chart_pack = True
            else:
                needs_chart_pack = (fetch_length > buffer.get("view_width", 0))
                if needs_chart_pack:
                    buffer["view_width"] = fetch_length
            rows = dict(buffer["rows"])
            last_id = buffer["last_id"]
        if last_id < 0 or needs_chart_pack:
            # 注意：合约服务就绪（insserve_ready）之前发送的 set_chart 会被前置丢弃，
            # 初始化或需要扩容 view_width 时重发并等待
            if KLINE_DEBUG:
                logger.info("[kline #%d] set_chart pack=%s (init=%s expand=%s)",
                            req_id, pack, last_id < 0, needs_chart_pack)
            deadline = time.monotonic() + 20.0
            resend_at = 0.0
            while time.monotonic() < deadline:
                with self._data_lock:
                    if self._charts.get(key) is not buffer:
                        raise TqClientError("合约查看已结束")
                now = time.monotonic()
                if now >= resend_at:
                    await self._send(pack)
                    resend_at = now + 2.0
                await asyncio.sleep(0.2)
                with self._data_lock:
                    current = self._charts.get(key)
                    if current is not buffer:
                        raise TqClientError("合约查看已结束")
                    last_id = current["last_id"]
                if last_id >= 0:
                    break
            if last_id < 0:
                if KLINE_DEBUG:
                    logger.warning("[kline #%d] init timeout %.1fs（服务器始终未确认 chart）",
                                   req_id, time.monotonic() - t0)
                raise TqClientError(f"K线数据初始化失败：{symbol} {period}s")
            if KLINE_DEBUG:
                logger.info("[kline #%d] chart ready %.2fs last_id=%d",
                            req_id, time.monotonic() - t0, last_id)
        with self._data_lock:
            current = self._charts.get(key)
            if current is not buffer:
                raise TqClientError("合约查看已结束")
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
                    current = self._charts.get(key)
                    if current is not buffer:
                        raise TqClientError("合约查看已结束")
                    rows = dict(current["rows"])
                    last_id = current["last_id"]
                need_from = max(0, last_id - fetch_length + 1)
                missing = [i for i in range(need_from, last_id + 1) if i not in rows]
            if missing:
                if KLINE_DEBUG:
                    need_n = last_id - need_from + 1
                    logger.warning("[kline #%d] incomplete %s %ds need=%d got=%d missing=%d elapsed=%.1fs",
                                   req_id, symbol, period, need_n, need_n - len(missing),
                                   len(missing), time.monotonic() - t0)
                raise TqClientError(f"K线数据不完整：{symbol} {period}s（缺 {len(missing)} 根）")
        bars: list[dict] = []
        for index in range(need_from, last_id + 1):
            bar = diff_auth.parse_kline_row(rows.get(index))
            if bar is not None:
                bars.append(bar)
        if not bars:
            # 服务器确认了 chart 却没有任何有效 K 线行：明确报“历史不可用”，
            # 绝不允许上层用实时快照/模拟数据顶替历史 K 线（P0 约定）。
            if KLINE_DEBUG:
                logger.warning("[kline #%d] history unavailable %s %ds rows=%d elapsed=%.1fs",
                               req_id, symbol, period, len(rows), time.monotonic() - t0)
            raise TqClientError(
                f"历史K线数据不可用：{symbol} {period}s（服务器未返回有效历史数据）")
        if KLINE_DEBUG:
            logger.info("[kline #%d] ok %s %ds bars=%d/%d elapsed=%.2fs",
                        req_id, symbol, period, len(bars), last_id - need_from + 1,
                        time.monotonic() - t0)
        return bars[-count:]

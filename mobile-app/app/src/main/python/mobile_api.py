"""Android 端移动接口层：starlette + uvicorn（纯 Python，避免 pydantic 原生依赖）。

与桌面版 api/http.py 暴露相同的路由与 WebSocket 协议，复用同一套 Services。
Android 上由 Chaquopy 调用 run_mobile_server() 启动（后台线程）。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from config import load_config, save_credentials, clear_credentials
from market.evaluator import evaluate
from tq.client import TqClientError
from tq.instruments import normalize_symbol
from services import build_services


def run_blocking(func, *args, **kwargs):
    """线程池执行阻塞调用（兼容不支持 asyncio.to_thread 的 Chaquopy Python）。"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: func(*args, **kwargs))

KLINE_PERIODS = {60: "1分钟", 300: "5分钟", 900: "15分钟",
                 1800: "30分钟", 3600: "60分钟", 86400: "日线"}
DECISION_PERIODS = (86400, 3600, 900, 300)
ROUTE_NAME = "C 直连版"


def _json(data, status: int = 200) -> JSONResponse:
    """JSON 响应的统一封装（比桌面版少一层 pydantic，纯 dict 输出）。"""
    return JSONResponse(data, status_code=status)


class MobileHub:
    """运行时容器 + WebSocket 连接管理（与桌面版 Services/ConnectionManager 等价）。"""

    def __init__(self, config, static_dir: Optional[Path] = None) -> None:
        self.config = config
        self.static_dir = static_dir
        self.route = ROUTE_NAME
        # 缺陷 C 修复：把 MobileHub 自己的行情回调注入 build_services。
        # 此前 build_services 挂的是 Services.on_quote_change（依赖 services.loop，
        # 手机端从未设置），而 MobileHub.on_quote_change→_enqueue→_broadcast_loop
        # 整条广播链从未注册——WS 客户端一条 quote 都收不到，UI 只能靠 REST 轮询。
        self.services = build_services(config, on_quote_change=self.on_quote_change)
        self.client = self.services.client
        self.instruments = self.services.instruments
        self.subscriptions = self.services.subscriptions
        self.cache = self.services.cache
        self.connections: list[WebSocket] = []
        self.queue: Optional[asyncio.Queue] = None     # uvicorn loop 内创建；行情线程经 call_soon_threadsafe 投递
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- 行情线程回调（接管 Services.on_quote_change：统计埋点 + 缓存 + 广播投递） ----
    def on_quote_change(self, quote) -> None:
        # 延迟统计埋点必须与广播同一条链，否则 status 的 quote_recv_total 永远为 0
        self.services.last_quote_unix = time.time()
        self.services.quote_recv_total += 1
        self.services.cache.set(quote)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._enqueue, quote)

    # ---- 异步包装：阻塞调用放线程池 ----
    async def subscribe_async(self, symbols: list[str]) -> dict:
        return await run_blocking(self.subscriptions.subscribe, symbols)

    async def unsubscribe_async(self, symbols: list[str]) -> dict:
        return await run_blocking(self.subscriptions.unsubscribe, symbols)

    async def decision_async(self, symbol: str, quote: dict, klines: dict) -> dict:
        instrument = await run_blocking(self.instruments.get, symbol)
        if instrument is None:
            return {"pending": False, "data_ok": False,
                    "direction": "观望", "score_long": 0, "score_short": 0,
                    "rationale": ["合约目录未就绪，暂不评估"]}
        return await run_blocking(
            self._evaluate_blocking, instrument, quote, klines)

    def _evaluate_blocking(self, instrument, quote, klines) -> dict:
        from market.evaluator import evaluate
        return evaluate(instrument, quote, klines, self.services.config.risk)

    async def kline_async(self, symbol: str, period: int, count: int) -> list[dict]:
        normalized = await run_blocking(normalize_symbol, self.client, symbol)
        if normalized is None:
            raise TqClientError(f"合约代码无法解析：{symbol}")
        return await run_blocking(
            self._kline_blocking, normalized, period, max(30, min(1000, count)))

    def _kline_blocking(self, normalized: str, period: int, count: int) -> list[dict]:
        return self.client.run_command("get_kline", normalized, period, count, timeout=30.0)

    # ---- WebSocket 广播 ----
    def on_startup(self) -> None:
        """uvicorn lifespan 启动钩子：记录事件循环、启动行情线程与广播泵。"""
        self.loop = asyncio.get_running_loop()
        # asyncio.Queue 必须在 uvicorn loop 内创建，行情线程只经
        # call_soon_threadsafe(_enqueue) 投递——不跨线程直接操作就没有 loop 绑定问题，
        # 且是即时唤醒（旧实现同步队列 + 0.2s 轮询，每笔行情最多平白叠 200ms 延迟，
        # 直接顶满 200ms P95 目标的上限）。
        self.queue = asyncio.Queue()
        self.client.start()
        self.loop.create_task(self._broadcast_loop())

    def _enqueue(self, quote) -> None:
        # 由 call_soon_threadsafe 调入 uvicorn loop 线程执行：put_nowait 即时唤醒广播协程
        if self.queue is not None:
            self.queue.put_nowait(quote)

    async def _broadcast_loop(self) -> None:
        """WS 广播泵（uvicorn loop 常驻任务）：逐条推队列里的行情快照，
        推送失败的连接从列表摘除。"""
        while True:
            quote = await self.queue.get()
            payload = {"type": "quote", "symbol": quote.symbol, "data": quote.to_dict(),
                       "ts": time.time()}   # 服务端发送时刻，前端算端到端延迟（P0-200ms 埋点）
            dead = []
            for ws in list(self.connections):
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in self.connections:
                    self.connections.remove(ws)


def create_mobile_app(hub: MobileHub) -> Starlette:
    """构建移动版接口应用（与桌面 REST/WS 协议一致）。"""

    def mask_account(account: str) -> str:
        return account[:3] + "****" + account[-2:] if len(account) > 6 else "****"

    async def auth_status(request):
        """登录状态：只返回是否已配置与掩码账号，绝不返回密码。"""
        configured = hub.client.credentials_configured
        account = hub.client.account
        return _json({"configured": configured,
                      "account": mask_account(account) if configured and account else ""})

    async def auth_set(request):
        """保存天勤凭据（App 私有目录，git 忽略）并触发重新登录。"""
        try:
            body = await request.json()
        except Exception:
            return _json({"detail": "请求体必须是 JSON"}, 422)
        account = str(body.get("account", "")).strip()
        password = str(body.get("password", ""))
        if not account or not password:
            return _json({"detail": "账号与密码不能为空"}, 422)
        save_credentials(account, password)
        hub.client.set_credentials(account, password)
        return _json({"ok": True, "account": mask_account(account), "route": hub.route})

    async def auth_delete(request):
        """退出登录：清除本地凭据并断开行情连接。"""
        clear_credentials()
        hub.client.set_credentials("", "")
        return _json({"ok": True})

    async def status(request):
        """网关状态总览：连接/目录三态/延迟埋点/WS 客户端数（前端 15 秒轮询）。

        注意 futures_count 只在目录就绪后计算——futures() 首查会建立缓存，
        此前 v1.1.3 缺陷：未就绪时的首查把内置表钉死（已由完整性感知缓存修复）。
        """
        catalog = None
        catalog_ready = getattr(hub.client, "catalog_ready", True)
        catalog_complete = getattr(hub.client, "catalog_complete", catalog_ready)
        if hub.client.connected and catalog_ready:
            try:
                catalog = len(hub.instruments.futures())
            except Exception:
                pass
        return _json({"connected": hub.client.connected,
                      "account": (hub.client.account[:3] + "****" + hub.client.account[-2:]
                                  if len(hub.client.account) > 6 else ""),
                      "error": hub.client.error,
                      "subscribed": hub.subscriptions.subscribed(),
                      "quote_count": len(hub.cache),
                      "futures_count": catalog,
                      "catalog_ready": catalog_ready,
                      "catalog_complete": catalog_complete,
                      "catalog_loading": hub.client.connected and not catalog_complete,
                      "catalog_progress": getattr(hub.client, "catalog_progress", None),
                      # ---- 延迟统计埋点（P0-200ms）：供 tools/latency_probe.py 采样 ----
                      "last_quote_unix": hub.services.last_quote_unix,
                      "quote_recv_total": hub.services.quote_recv_total,
                      "ws_clients": len(hub.connections),
                      "route": hub.route})

    async def instruments(request):
        if not hub.client.connected:
            return _json({"detail": "天勤未连接"}, 503)
        exchange = request.query_params.get("exchange", "").upper()
        keyword = request.query_params.get("keyword", "")
        try:
            # 缺陷 D：不再硬编码 limit=200（此前 578 条期货只能看到前 200）。
            # 全量返回，前端按滚动位置增量渲染（renderSearch 分批 append）。
            items = await run_blocking(
                hub.instruments.list, exchange=exchange, keyword=keyword, limit=0)
        except TqClientError as error:
            return _json({"detail": str(error)}, 503)
        return _json({"total": len(items), "exchange": exchange,
                      "keyword": keyword, "items": items})

    async def quote(request):
        symbol = request.path_params["symbol"]
        if not hub.client.connected:
            return _json({"detail": "天勤未连接"}, 503)
        cached = hub.cache.get(symbol)
        if cached is not None:
            return _json({"symbol": symbol, "data": cached.to_dict(), "pending": False})
        result = await hub.subscribe_async([symbol])
        if result["failed"]:
            return _json({"detail": result["failed"][0]["reason"]}, 422)
        return _json({"symbol": symbol, "data": None, "pending": True,
                      "message": "已订阅，等待首笔行情"})

    async def kline(request):
        if not hub.client.connected:
            return _json({"detail": "天勤未连接"}, 503)
        symbol = request.path_params["symbol"]
        try:
            period = int(request.query_params.get("period", "300"))
            count = int(request.query_params.get("count", "200"))
        except ValueError:
            return _json({"detail": "period/count 参数不合法"}, 422)
        if period not in KLINE_PERIODS:
            return _json({"detail": f"不支持的周期：{period}，可选 {sorted(KLINE_PERIODS)}"}, 422)
        try:
            bars = await hub.kline_async(symbol, period, count)
        except TqClientError as error:
            return _json({"detail": str(error)}, 503)
        return _json({"symbol": symbol, "period": period,
                      "unit": KLINE_PERIODS.get(period, ""),
                      "count": len(bars), "bars": bars})

    async def decision(request):
        symbol = request.path_params["symbol"]
        if not hub.client.connected:
            return _json({"detail": "天勤未连接"}, 503)
        cached = hub.cache.get(symbol)
        if cached is None:
            return _json({"symbol": symbol, "pending": True,
                          "message": "尚未收到该合约行情，请稍候"})
        klines: dict[int, list] = {}
        for period in DECISION_PERIODS:
            klines[period] = await hub.kline_async(symbol, period, 200)
        result = await hub.decision_async(symbol, cached.to_dict(), klines)
        return _json({"symbol": symbol, **result})

    async def subscriptions_get(request):
        return _json({"symbols": hub.subscriptions.subscribed()})

    async def subscriptions_post(request):
        if not hub.client.connected:
            return _json({"detail": "天勤未连接"}, 503)
        body = await request.json()
        symbols = body.get("symbols") or []
        if not symbols:
            return _json({"detail": "symbols 不能为空"}, 422)
        return _json(await hub.subscribe_async([str(s) for s in symbols]))

    async def subscriptions_delete(request):
        symbol = request.path_params["symbol"]
        return _json({"symbol": symbol, **await hub.unsubscribe_async([symbol])})

    async def ws_market(websocket: WebSocket):
        """行情 WS 端点：subscribe/unsubscribe/ping 三种动作，断开自动摘除。"""
        await websocket.accept()
        await websocket.send_json({"type": "hello", "connected": hub.client.connected,
                                   "subscribed": hub.subscriptions.subscribed()})
        hub.connections.append(websocket)
        try:
            while True:
                message = json.loads(await websocket.receive_text())
                action = str(message.get("action", "")).lower()
                symbols = [str(s) for s in (message.get("symbols") or [])]
                if action == "subscribe":
                    result = await hub.subscribe_async(symbols)
                    await websocket.send_json({"type": "subscribed", **result})
                    for symbol in result["subscribed"]:
                        cached = hub.cache.get(symbol)
                        if cached is not None:
                            await websocket.send_json({"type": "quote_snapshot",
                                                       "symbol": symbol,
                                                       "data": cached.to_dict()})
                elif action == "unsubscribe":
                    result = await hub.unsubscribe_async(symbols)
                    await websocket.send_json({"type": "unsubscribed", **result})
                elif action == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in hub.connections:
                hub.connections.remove(websocket)

    async def index(request):
        if hub.static_dir and (hub.static_dir / "index.html").exists():
            # no-cache：允许缓存但每次必须回源校验（ETag/Last-Modified），换包后必拿新版
            # index.html，进而由 ?v= 版本参数带出新版 js/css。v1.1.3 真机发现：vivo 恢复
            # 应用数据会把旧 WebView 缓存一起还原，裸 FileResponse 的旧前端被长期钉死。
            return FileResponse(hub.static_dir / "index.html",
                                headers={"Cache-Control": "no-cache"})
        return _json({"name": "ZA量化 移动版", "route": hub.route})

    @asynccontextmanager
    async def lifespan(app):
        hub.on_startup()
        yield

    routes = [
        Route("/", index),
        Route("/api/v1/auth", auth_status, methods=["GET"]),
        Route("/api/v1/auth", auth_set, methods=["POST"]),
        Route("/api/v1/auth", auth_delete, methods=["DELETE"]),
        Route("/api/v1/status", status),
        Route("/api/v1/instruments", instruments),
        Route("/api/v1/quote/{symbol:path}", quote),
        Route("/api/v1/kline/{symbol:path}", kline),
        Route("/api/v1/decision/{symbol:path}", decision),
        Route("/api/v1/subscriptions", subscriptions_get, methods=["GET"]),
        Route("/api/v1/subscriptions", subscriptions_post, methods=["POST"]),
        Route("/api/v1/subscriptions/{symbol:path}", subscriptions_delete, methods=["DELETE"]),
        WebSocketRoute("/ws/market", ws_market),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    if hub.static_dir and hub.static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(hub.static_dir)), name="static")
    return app


def run_mobile_server(host: str = "127.0.0.1", port: int = 8000,
                      static_dir: Optional[Path] = None) -> None:
    """Android 入口：加载配置 → 装配 MobileHub → 启动 uvicorn（阻塞）。

    由 BackendService 的后台线程调用（Chaquopy 从 Java 侧导入本模块触发）。
    静态目录即 APK 内打包的 mobile static（前端同源加载，无跨域）。
    """
    import uvicorn
    config = load_config()
    hub = MobileHub(config, static_dir)
    app = create_mobile_app(hub)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)

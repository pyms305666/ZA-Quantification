"""Android 端移动接口层：starlette + uvicorn（纯 Python，避免 pydantic 原生依赖）。

与桌面版 api/http.py 暴露相同的路由与 WebSocket 协议，复用同一套 Services。
Android 上由 Chaquopy 调用 run_mobile_server() 启动（后台线程）。
"""

from __future__ import annotations

import asyncio
import json
import queue
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from config import load_config
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
    return JSONResponse(data, status_code=status)


class MobileHub:
    """运行时容器 + WebSocket 连接管理（与桌面版 Services/ConnectionManager 等价）。"""

    def __init__(self, config, static_dir: Optional[Path] = None) -> None:
        self.config = config
        self.static_dir = static_dir
        self.route = ROUTE_NAME
        self.services = build_services(config)
        self.client = self.services.client
        self.instruments = self.services.instruments
        self.subscriptions = self.services.subscriptions
        self.cache = self.services.cache
        self.connections: list[WebSocket] = []
        self.queue: Optional[queue.Queue] = None        # 线程安全同步队列（无 loop 绑定）
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- 行情线程回调（与桌面版 Services.on_quote_change 一致） ----
    def on_quote_change(self, quote) -> None:
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
        self.loop = asyncio.get_running_loop()
        self.queue = queue.Queue()                      # 线程安全，无 loop 绑定
        self.client.start()
        self.loop.create_task(self._broadcast_loop())

    def on_startup_hook(self) -> None:
        """供 on_startup 调用的显式钩子（预留扩展）。"""
        self.client.start()

    def _enqueue(self, quote) -> None:
        # 在 uvicorn loop 线程内调用的入队（线程安全）
        self.queue.put(quote)

    async def _broadcast_loop(self) -> None:
        while True:
            # 轮询同步队列（每 0.2s），彻底避开 asyncio.Queue 的 loop 绑定问题
            quote = None
            while quote is None:
                try:
                    quote = self.queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.2)
            payload = {"type": "quote", "symbol": quote.symbol, "data": quote.to_dict()}
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

    async def auth_status(request):
        """登录状态：手机端凭据保存在 App 私有目录，向天勤直连无需额外登录，
        返回 configured=True（mobile_api 免账户登录流程，天勤凭据存本地）"""
        return _json({"configured": True, "account": "本机账户"})

    async def status(request):
        catalog = None
        if hub.client.connected:
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
                      "route": hub.route})

    async def instruments(request):
        if not hub.client.connected:
            return _json({"detail": "天勤未连接"}, 503)
        exchange = request.query_params.get("exchange", "").upper()
        keyword = request.query_params.get("keyword", "")
        try:
            items = await run_blocking(
                hub.instruments.list, exchange=exchange, keyword=keyword)
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
            return FileResponse(hub.static_dir / "index.html")
        return _json({"name": "ZA量化 移动版", "route": hub.route})

    @asynccontextmanager
    async def lifespan(app):
        hub.on_startup()
        yield

    routes = [
        Route("/", index),
        Route("/api/v1/auth", auth_status),
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
    """Android 入口：启动后端（阻塞，放入后台线程调用）。"""
    import uvicorn
    config = load_config()
    hub = MobileHub(config, static_dir)
    app = create_mobile_app(hub)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)

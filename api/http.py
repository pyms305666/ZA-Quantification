"""HTTP REST API：负责查询，不负责高频推送。

GET    /api/v1/status                网关与天勤连接状态
GET    /api/v1/instruments            合约目录（?exchange=&keyword=&refresh=）
GET    /api/v1/instruments/{exchange} 某交易所合约
GET    /api/v1/options/{underlying}   某标的的期权代码（按需查询）
GET    /api/v1/quote/{symbol}         最新行情（未订阅时自动订阅）
GET    /api/v1/subscriptions          当前订阅
POST   /api/v1/subscriptions          动态订阅 {"symbols": [...]}
DELETE /api/v1/subscriptions/{symbol} 退订
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import Config
from market.cache import QuoteCache
from market.evaluator import evaluate
from market.model import Instrument
from tq.client import TqClient, TqClientError
from tq.instruments import InstrumentManager, normalize_symbol
from tq.subscriber import SubscriptionManager
from .websocket import ConnectionManager, create_ws_router

KLINE_PERIODS = {
    60: "1分钟", 300: "5分钟", 900: "15分钟", 1800: "30分钟", 3600: "60分钟", 86400: "日线",
}
DECISION_PERIODS = (86400, 3600, 900, 300)


class SubscribeRequest(BaseModel):
    symbols: list[str]


@dataclass
class Services:
    """网关运行时服务集合；HTTP / WS 只通过它访问行情核心。"""

    config: Config
    client: TqClient
    instruments: InstrumentManager
    subscriptions: SubscriptionManager
    cache: QuoteCache
    connections: ConnectionManager
    broadcast_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    loop: Optional[asyncio.AbstractEventLoop] = None

    def on_quote_change(self, quote: object) -> None:
        """在 tqsdk 事件循环线程内被调用：只写缓存并投递到异步队列。"""
        self.cache.set(quote)  # type: ignore[arg-type]
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.broadcast_queue.put_nowait, quote)


def build_services(config: Config) -> Services:
    connections = ConnectionManager()
    cache = QuoteCache()
    client = TqClient(config.tqsdk.account, config.tqsdk.password)
    instruments = InstrumentManager(client)
    services = Services(
        config=config,
        client=client,
        instruments=instruments,
        subscriptions=SubscriptionManager(client, instruments),
        cache=cache,
        connections=connections,
    )
    client.set_callbacks(on_quote_change=services.on_quote_change)
    return services


def create_app(config: Config) -> FastAPI:
    services = build_services(config)
    app = FastAPI(title="国内期货行情网关", version="0.2.0", lifespan=_lifespan(services))
    app.state.services = services

    def get_services(request: Request) -> Services:
        return request.app.state.services

    def require_connected(services: Services) -> None:
        if not services.client.connected:
            raise HTTPException(status_code=503, detail="天勤未连接")
        if not services.client.ready:
            raise HTTPException(status_code=503, detail="天勤连接初始化中，请稍候重试")

    def mask_account(account: str) -> str:
        return account[:3] + "****" + account[-2:] if len(account) > 6 else "****"

    router = APIRouter()

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @router.get("/")
    def index() -> object:
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {
            "name": "国内期货行情网关",
            "endpoints": [
                "/api/v1/status",
                "/api/v1/instruments",
                "/api/v1/instruments/{exchange}",
                "/api/v1/options/{underlying}",
                "/api/v1/quote/{symbol}",
                "/api/v1/kline/{symbol}",
                "/api/v1/decision/{symbol}",
                "/api/v1/subscriptions",
                "/ws/market",
            ],
        }

    @router.get("/api/v1/status")
    def status(services: Services = Depends(get_services)) -> dict:
        futures_count = None
        if services.client.connected:
            try:
                futures_count = len(services.instruments.futures())
            except TqClientError:
                pass
        return {
            "connected": services.client.connected,
            "account": mask_account(services.client.account) if services.client.account else "",
            "error": services.client.error,
            "subscribed": services.subscriptions.subscribed(),
            "quote_count": len(services.cache),
            "futures_count": futures_count,
        }

    @router.get("/api/v1/instruments")
    def instruments(exchange: str = "", keyword: str = "", refresh: bool = False,
                    services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        items = services.instruments.list(exchange=exchange.upper(), keyword=keyword, refresh=refresh)
        return {"total": len(items), "exchange": exchange.upper(), "keyword": keyword, "items": items}

    @router.get("/api/v1/instruments/{exchange}")
    def instruments_by_exchange(exchange: str, services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        items = services.instruments.list(exchange=exchange.upper())
        return {"total": len(items), "exchange": exchange.upper(), "items": items}

    @router.get("/api/v1/options/{underlying}")
    def options(underlying: str, services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        try:
            symbols = services.instruments.options(underlying)
        except TqClientError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"underlying": underlying, "total": len(symbols), "symbols": symbols}

    @router.get("/api/v1/kline/{symbol}")
    def kline(symbol: str, period: int = 300, count: int = 200,
              services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        if period not in KLINE_PERIODS:
            raise HTTPException(status_code=422, detail=f"不支持的周期：{period}，可选 {sorted(KLINE_PERIODS)}")
        normalized = normalize_symbol(services.client, symbol)
        if normalized is None:
            raise HTTPException(status_code=422, detail=f"合约代码无法解析：{symbol}")
        bars = services.client.run_command(
            "get_kline", normalized, period, max(30, min(1000, count)), timeout=30.0)
        return {"symbol": normalized, "period": period, "unit": KLINE_PERIODS[period],
                "count": len(bars), "bars": bars}

    @router.get("/api/v1/decision/{symbol}")
    def decision(symbol: str, services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        normalized = normalize_symbol(services.client, symbol)
        if normalized is None:
            raise HTTPException(status_code=422, detail=f"合约代码无法解析：{symbol}")
        instrument = services.instruments.get(normalized)
        if instrument is None:
            raise HTTPException(status_code=422, detail=f"合约不存在：{normalized}")
        cached = services.cache.get(normalized)
        if cached is None:
            return {"symbol": normalized, "pending": True, "message": "尚未收到该合约行情，请稍候"}
        klines: dict[int, list[dict]] = {}
        for period in DECISION_PERIODS:
            klines[period] = services.client.run_command(
                "get_kline", normalized, period, 200, timeout=30.0)
        return {"symbol": normalized, **evaluate(instrument, cached.to_dict(), klines, services.config.risk)}

    @router.get("/api/v1/quote/{symbol}")
    def quote(symbol: str, services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        normalized = normalize_symbol(services.client, symbol)
        if normalized is None:
            raise HTTPException(status_code=422, detail=f"合约代码无法解析：{symbol}")
        cached = services.cache.get(normalized)
        if cached is not None:
            return {"symbol": normalized, "data": cached.to_dict(), "pending": False}
        result = services.subscriptions.subscribe([normalized])
        if result["failed"]:
            raise HTTPException(status_code=422, detail=result["failed"][0]["reason"])
        return {"symbol": normalized, "data": None, "pending": True,
                "message": "已订阅，等待首笔行情"}

    @router.get("/api/v1/subscriptions")
    def subscriptions(services: Services = Depends(get_services)) -> dict:
        return {"symbols": services.subscriptions.subscribed()}

    @router.post("/api/v1/subscriptions")
    def subscribe(body: SubscribeRequest, services: Services = Depends(get_services)) -> dict:
        require_connected(services)
        if not body.symbols:
            raise HTTPException(status_code=400, detail="symbols 不能为空")
        return services.subscriptions.subscribe(body.symbols)

    @router.delete("/api/v1/subscriptions/{symbol}")
    def unsubscribe(symbol: str, services: Services = Depends(get_services)) -> dict:
        result = services.subscriptions.unsubscribe([symbol])
        return {"symbol": symbol, **result}

    app.include_router(router)
    app.include_router(create_ws_router(services))
    return app


def _lifespan(services: Services):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> None:
        services.loop = asyncio.get_running_loop()
        broadcast_task = asyncio.create_task(_broadcast_loop(services))
        services.client.start()
        try:
            yield
        finally:
            broadcast_task.cancel()
            with suppress(asyncio.CancelledError):
                await broadcast_task
            services.client.close()

    return lifespan


async def _broadcast_loop(services: Services) -> None:
    while True:
        quote = await services.broadcast_queue.get()
        await services.connections.broadcast_quote(quote)

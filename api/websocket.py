"""WebSocket 实时推送：只在行情变化（is_changing）时推送，不做定时全量广播。

协议（ws://127.0.0.1:8000/ws/market）：
  客户端 -> 服务端：
    {"action": "subscribe",   "symbols": ["SHFE.rb2610", ...]}
    {"action": "unsubscribe", "symbols": [...]}
    {"action": "ping"}
  服务端 -> 客户端：
    {"type": "hello", ...}                             连接建立
    {"type": "subscribed", "subscribed": [...], "skipped": [...], "failed": [...]}
    {"type": "quote_snapshot", "symbol": ..., "data": {...}}   新订阅合约的当前缓存
    {"type": "quote", "symbol": ..., "data": {...}}            行情变化推送
    {"type": "pong"}
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from market.model import MarketQuote

if TYPE_CHECKING:
    from .http import Services


class ConnectionManager:
    """所有 WebSocket 客户端共用一个 TqApi / 一套推送（避免每客户端一个连接）。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast_quote(self, quote: MarketQuote) -> None:
        payload = {"type": "quote", "symbol": quote.symbol, "data": quote.to_dict()}
        async with self._lock:
            targets = list(self._connections)
        for websocket in targets:
            try:
                await websocket.send_json(payload)
            except Exception:
                await self.disconnect(websocket)


def create_ws_router(services: "Services") -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/market")
    async def market(websocket: WebSocket) -> None:
        await services.connections.connect(websocket)
        await websocket.send_json({
            "type": "hello",
            "connected": services.client.connected,
            "subscribed": services.subscriptions.subscribed(),
        })
        try:
            while True:
                message = await websocket.receive_json()
                action = str(message.get("action", "")).lower()
                symbols = message.get("symbols") or []
                if action == "subscribe":
                    result = await asyncio.to_thread(services.subscriptions.subscribe, list(symbols))
                    await websocket.send_json({"type": "subscribed", **result})
                    # 已订阅(skipped)的合约也要补发快照：休市时段无行情推送，
                    # 否则页面在订阅已存在时永远收不到首笔行情
                    for symbol in dict.fromkeys(result["subscribed"] + result.get("skipped", [])):
                        cached = services.cache.get(symbol)
                        if cached is not None:
                            await websocket.send_json(
                                {"type": "quote_snapshot", "symbol": symbol, "data": cached.to_dict()})
                elif action == "unsubscribe":
                    result = await asyncio.to_thread(services.subscriptions.unsubscribe, list(symbols))
                    await websocket.send_json({"type": "unsubscribed", **result})
                elif action == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json({"type": "error", "message": f"未知 action：{action}"})
        except WebSocketDisconnect:
            pass
        finally:
            await services.connections.disconnect(websocket)

    return router

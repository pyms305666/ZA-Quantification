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
from ws_transport import TransportClosed, WebSocketHub

if TYPE_CHECKING:
    from .http import Services


class ConnectionManager(WebSocketHub):
    """所有 WebSocket 客户端共用一个 TqApi / 一套推送（避免每客户端一个连接）。"""

    async def broadcast_quote(self, quote: MarketQuote) -> None:
        """广播只入队；发送时由连接 sender 添加实际发送时刻 ts。"""
        self.broadcast({"type": "quote", "symbol": quote.symbol, "data": quote.to_dict()})


def create_ws_router(services: "Services") -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/market")
    async def market(websocket: WebSocket) -> None:
        """行情 WS 端点：处理 subscribe/unsubscribe/ping 三种动作直到断开。

        订阅动作经 asyncio.to_thread 进订阅管理器（内部可能查目录/提交命令，
        阻塞操作不占事件循环）；订阅成功后补发缓存快照，保证休市时段
        页面也能立刻见到行情。
        """
        try:
            await services.connections.connect(websocket, {
                "type": "hello",
                "connected": services.client.connected,
                "subscribed": services.subscriptions.subscribed(),
            })
            while True:
                message = await websocket.receive_json()
                action = str(message.get("action", "")).lower()
                symbols = message.get("symbols") or []
                if action == "subscribe":
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, services.subscriptions.subscribe, list(symbols))
                    await services.connections.send_control(websocket, {"type": "subscribed", **result})
                    # 已订阅(skipped)的合约也要补发快照：休市时段无行情推送，
                    # 否则页面在订阅已存在时永远收不到首笔行情
                    for symbol in dict.fromkeys(result["subscribed"] + result.get("skipped", [])):
                        cached = services.cache.get(symbol)
                        if cached is not None:
                            await services.connections.send_control(websocket,
                                {"type": "quote_snapshot", "symbol": symbol, "data": cached.to_dict()})
                elif action == "unsubscribe":
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, services.subscriptions.unsubscribe, list(symbols))
                    await services.connections.send_control(websocket, {"type": "unsubscribed", **result})
                elif action == "ping":
                    await services.connections.send_control(websocket, {"type": "pong"})
                else:
                    await services.connections.send_control(
                        websocket, {"type": "error", "message": f"未知 action：{action}"})
        except (WebSocketDisconnect, TransportClosed):
            pass
        finally:
            await services.connections.disconnect(websocket)

    return router

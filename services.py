"""运行时服务集合（Services）与装配——桌面接口层与 Android 移动接口层共用。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import Config
from market.cache import QuoteCache
from tq.client import TqClient
from tq.instruments import InstrumentManager
from tq.subscriber import SubscriptionManager
from api.websocket import ConnectionManager


@dataclass
class Services:
    """网关运行时服务集合；HTTP / WS 只通过它访问行情核心。"""

    config: Config
    client: TqClient
    instruments: InstrumentManager
    subscriptions: SubscriptionManager
    cache: QuoteCache
    connections: ConnectionManager
    broadcast_queue: Optional[asyncio.Queue] = None   # 延迟创建：asyncio.Queue 会绑定创建它的 loop
    loop: Optional[asyncio.AbstractEventLoop] = None
    auto_exit_idle_seconds: Optional[int] = None   # 无浏览器连接自动退出阈值（None=不启用）

    def on_quote_change(self, quote: object) -> None:
        """在 tqsdk 事件循环线程内被调用：只写缓存并投递到异步队列。"""
        self.cache.set(quote)  # type: ignore[arg-type]
        if self.loop is not None:
            if self.broadcast_queue is None:
                self.broadcast_queue = asyncio.Queue()
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
    def on_status(status: str) -> None:
        logging.getLogger("gateway.diff").info("行情连接状态：%s", status)

    client.set_callbacks(on_quote_change=services.on_quote_change, on_status=on_status)
    return services




async def _broadcast_loop(services: Services) -> None:
    while True:
        if services.broadcast_queue is None:
            services.broadcast_queue = asyncio.Queue()
        quote = await services.broadcast_queue.get()
        await services.connections.broadcast_quote(quote)

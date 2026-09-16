"""运行时服务集合（Services）与装配——桌面接口层与 Android 移动接口层共用。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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
    # ---- 延迟统计埋点（P0-200ms）：行情到达时刻与累计笔数，status 接口对外输出 ----
    last_quote_unix: float = 0.0    # 最近一笔行情到达时刻（Unix 秒，线程内写、接口线程读，GIL 安全）
    quote_recv_total: int = 0       # 行情到达累计笔数

    def on_quote_change(self, quote: object) -> None:
        """在 tqsdk 事件循环线程内被调用：只写缓存并投递到异步队列。"""
        self.last_quote_unix = time.time()
        self.quote_recv_total += 1
        self.cache.set(quote)  # type: ignore[arg-type]
        if self.loop is not None:
            if self.broadcast_queue is None:
                self.broadcast_queue = asyncio.Queue()
            self.loop.call_soon_threadsafe(self.broadcast_queue.put_nowait, quote)


def build_services(config: Config,
                   on_quote_change: Optional[Callable[[Any], None]] = None) -> Services:
    """装配运行时服务集合。

    on_quote_change 可注入自定义行情回调：手机版 MobileHub 用它接管"统计埋点 +
    缓存 + 广播投递"整条链（缺陷 C：此前 build_services 只挂 Services 自己的回调，
    MobileHub 的广播链路成为死代码，WS 客户端收不到任何 quote）。缺省沿用
    Services 自带实现（桌面版）。
    连接就绪回调统一挂订阅管理器：连接建立后自动重放挂起的订阅（缺陷 A/F）。
    """
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

    client.set_callbacks(on_quote_change=on_quote_change or services.on_quote_change,
                         on_status=on_status,
                         on_connected=services.subscriptions.on_connected)
    return services




async def _broadcast_loop(services: Services) -> None:
    while True:
        if services.broadcast_queue is None:
            services.broadcast_queue = asyncio.Queue()
        quote = await services.broadcast_queue.get()
        await services.connections.broadcast_quote(quote)

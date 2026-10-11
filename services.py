"""运行时服务集合（Services）与装配——桌面接口层与 Android 移动接口层共用。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from config import Config
from market.cache import QuoteCache
from tq.client import TqClient
from tq.instruments import InstrumentManager
from tq.subscriber import SubscriptionManager
from api.websocket import ConnectionManager
from ws_transport import QuoteIngress


@dataclass
class Services:
    """网关运行时服务集合；HTTP / WS 只通过它访问行情核心。"""

    config: Config
    client: TqClient
    instruments: InstrumentManager
    subscriptions: SubscriptionManager
    cache: QuoteCache
    connections: ConnectionManager
    broadcast_ingress: Optional[QuoteIngress] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    auto_exit_idle_seconds: Optional[int] = None   # 无浏览器连接自动退出阈值（None=不启用）
    # ---- 延迟统计埋点（P0-200ms）：行情到达时刻与累计笔数，status 接口对外输出 ----
    last_quote_unix: float = 0.0    # 最近一笔行情到达时刻（Unix 秒，线程内写、接口线程读，GIL 安全）
    quote_recv_total: int = 0       # 行情到达累计笔数

    def start(self) -> None:
        """先启动恢复 worker，避免首个连接通知发生在 worker 启动之前。"""
        reopen = getattr(self.connections, "reopen", None)
        if reopen is not None and getattr(self.connections, "_closing", False):
            reopen()
        if self.loop is not None:
            self.attach_ingress()
        self.subscriptions.start()
        self.client.start()

    def attach_ingress(self) -> QuoteIngress:
        if self.broadcast_ingress is None or self.broadcast_ingress.closed:
            self.broadcast_ingress = QuoteIngress(self.loop, self._broadcast_batch)
        return self.broadcast_ingress

    def _broadcast_batch(self, quotes: list) -> None:
        for quote in quotes:
            self.connections.broadcast({"type": "quote", "symbol": quote.symbol,
                                        "data": quote.to_dict()})

    async def close(self) -> None:
        """停止恢复、关闭连接与行情线程；阻塞 join 不占 Web 事件循环。"""
        self.loop = None
        if self.broadcast_ingress is not None:
            self.broadcast_ingress.close()
        self.subscriptions.request_stop()
        close_all = getattr(self.connections, "close_all", None)
        if close_all is not None:
            await close_all()
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.client.close)
        finally:
            await loop.run_in_executor(None, self.subscriptions.close)

    def on_quote_change(self, quote: object) -> None:
        """在 tqsdk 事件循环线程内被调用：只写缓存并投递到异步队列。"""
        self.last_quote_unix = time.time()
        self.quote_recv_total += 1
        self.cache.set(quote)  # type: ignore[arg-type]
        ingress = self.broadcast_ingress
        if self.loop is not None and ingress is not None:
            ingress.submit(quote)


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
    """受管入口生命周期；实际批量广播由有界 ingress 在 Web loop 调度。"""
    await services.attach_ingress().wait_closed()

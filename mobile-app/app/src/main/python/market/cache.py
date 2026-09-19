"""最新行情内存缓存：HTTP 查询直接读缓存，不触碰 TqSdk。"""

from __future__ import annotations

import threading
from typing import Optional

from .model import MarketQuote


class QuoteCache:
    """线程安全的最新行情缓存（第一层缓存；规模大了之后可替换为 Redis）。"""

    def __init__(self) -> None:
        self._data: dict[str, MarketQuote] = {}
        self._lock = threading.Lock()

    def set(self, quote: MarketQuote) -> None:
        """写入/覆盖一只合约的最新快照（行情回调线程调用，高频）。"""
        with self._lock:
            self._data[quote.symbol] = quote

    def get(self, symbol: str) -> Optional[MarketQuote]:
        """读取一只合约的最新快照；从未订阅/未收到行情返回 None。"""
        with self._lock:
            return self._data.get(symbol)

    def all(self) -> list[MarketQuote]:
        """返回全部缓存快照的列表副本（供状态统计与调试接口）。"""
        with self._lock:
            return list(self._data.values())

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

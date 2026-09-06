"""移动版 stub：不依赖 fastapi/starlette（MobileHub 自行管理 WebSocket 连接）。

桌面版 services.py 依赖此模块的 ConnectionManager，但移动版 MobileHub
不调用 services.connections——这里用最小实现满足导入即可。
"""
from __future__ import annotations
from threading import Lock


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set = set()
        self._lock = Lock()

    def add(self, ws) -> None:
        with self._lock:
            self._connections.add(ws)

    def remove(self, ws) -> None:
        with self._lock:
            self._connections.discard(ws)

    def __len__(self) -> int:
        with self._lock:
            return len(self._connections)

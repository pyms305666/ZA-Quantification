"""标准库 WebSocket FIFO 传输与有界行情入口；不参与行情缓存和评估。"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import logging
import threading
import time
from typing import Any, Callable, Optional


logger = logging.getLogger("gateway.ws")


class TransportClosed(RuntimeError):
    """控制消息未发送完成，连接已关闭或无法继续发送。"""


class OutboundConnection:
    """一个连接仅有一个 sender，控制消息和行情消息共用 FIFO。"""

    def __init__(self, websocket: Any, on_closed: Optional[Callable] = None,
                 queue_size: int = 128, send_timeout: float = 2.0) -> None:
        self.websocket = websocket
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.send_timeout = send_timeout
        self.closed = False
        self._on_closed = on_closed
        self._active_completion = None
        self._failure = TransportClosed("WebSocket 已关闭")
        self._close_task = None
        self.sender_task = asyncio.create_task(self._send_loop())

    def enqueue(self, payload: dict, control: bool = False):
        if self.closed:
            raise self._failure
        if self.queue.full():
            self._fail(1013, "WebSocket 发送队列已满")
            raise self._failure
        completion = asyncio.get_running_loop().create_future() if control else None
        self.queue.put_nowait((dict(payload), completion))
        return completion

    async def send_control(self, payload: dict) -> None:
        await self.enqueue(payload, control=True)

    def _fail(self, code: int, message: str) -> None:
        if self.closed:
            return
        self.closed = True
        self._failure = TransportClosed(message)
        if self._active_completion is not None and not self._active_completion.done():
            self._active_completion.set_exception(self._failure)
        while not self.queue.empty():
            _, completion = self.queue.get_nowait()
            if completion is not None and not completion.done():
                completion.set_exception(self._failure)
        self._close_task = asyncio.create_task(self._finish_close(code))
        if self._on_closed is not None:
            self._on_closed(self)

    async def close(self, code: int = 1001) -> None:
        self._fail(code, "WebSocket 已关闭")
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    async def _finish_close(self, code: int) -> None:
        self.sender_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.sender_task
        try:
            await asyncio.wait_for(self.websocket.close(code=code), timeout=1.0)
        except Exception:
            pass

    async def _send_loop(self) -> None:
        try:
            while True:
                payload, completion = await self.queue.get()
                self._active_completion = completion

                async def write():
                    if payload.get("type") == "quote":
                        payload["ts"] = time.time()
                    await self.websocket.send_json(payload)

                await asyncio.wait_for(write(), timeout=self.send_timeout)
                if completion is not None and not completion.done():
                    completion.set_result(None)
                self._active_completion = None
        except asyncio.TimeoutError:
            self._fail(1013, "WebSocket 发送超时")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail(1011, "WebSocket 发送失败")
        finally:
            self._active_completion = None


class WebSocketHub:
    """连接登记只在所属 Web loop 中进行，广播入队不等待任何网络发送。"""

    def __init__(self) -> None:
        self._peers = {}
        self._connecting = {}
        self._accepting = {}
        self._retiring = set()
        self._empty_since = time.monotonic()
        self._generation = 0
        self._closing = False

    def reopen(self) -> None:
        if self._retiring or self._accepting or self._connecting or self._peers:
            raise RuntimeError("WebSocket 旧生命周期尚未结束")
        self._closing = False

    def _removed(self, peer: OutboundConnection) -> None:
        self._retiring.add(peer)
        peer._close_task.add_done_callback(lambda task: self._retiring.discard(peer))
        websocket = peer.websocket
        if self._peers.get(websocket) is peer:
            self._peers.pop(websocket, None)
        if self._connecting.get(websocket) is peer:
            self._connecting.pop(websocket, None)
        if not self._peers and self._empty_since is None:
            self._empty_since = time.monotonic()

    async def connect(self, websocket: Any, hello: dict) -> None:
        generation = self._generation
        task = asyncio.current_task()
        peer = None
        self._accepting[websocket] = task
        try:
            if self._closing:
                raise TransportClosed("WebSocket 正在关闭")
            await websocket.accept()
            if self._closing or generation != self._generation:
                raise TransportClosed("WebSocket 正在关闭")
            peer = OutboundConnection(websocket, on_closed=self._removed)
            self._connecting[websocket] = peer
            await peer.send_control(hello)
            if generation != self._generation or peer.closed:
                raise TransportClosed("WebSocket 正在关闭")
            self._connecting.pop(websocket, None)
            self._peers[websocket] = peer
            self._empty_since = None
        except BaseException:
            if peer is not None:
                await peer.close()
            else:
                with suppress(Exception):
                    await asyncio.wait_for(websocket.close(code=1001), timeout=1.0)
            raise
        finally:
            if self._accepting.get(websocket) is task:
                self._accepting.pop(websocket, None)

    async def send_control(self, websocket: Any, payload: dict) -> None:
        peer = self._peers.get(websocket)
        if peer is None:
            raise TransportClosed("WebSocket 已关闭")
        await peer.send_control(payload)

    def broadcast(self, payload: dict) -> None:
        for peer in list(self._peers.values()):
            try:
                peer.enqueue(payload)
            except TransportClosed:
                pass

    async def disconnect(self, websocket: Any) -> None:
        peer = self._peers.get(websocket) or self._connecting.get(websocket)
        if peer is not None:
            await peer.close(code=1000)

    async def close_all(self, code: int = 1001) -> None:
        self._closing = True
        self._generation += 1
        current = asyncio.current_task()
        accepting = {task for task in self._accepting.values() if task is not current}
        for task in accepting:
            task.cancel()
        if accepting:
            await asyncio.gather(*accepting, return_exceptions=True)
        peers = set(self._peers.values()) | set(self._connecting.values()) | self._retiring
        await asyncio.gather(*(peer.close(code=code) for peer in peers))

    def client_count(self) -> int:
        return len(self._peers)

    def idle_seconds(self) -> Optional[float]:
        if self._peers:
            return None
        return time.monotonic() - self._empty_since


class QuoteIngress:
    """行情线程只写有界 deque，至多一个 drain 等待 Web loop 唤醒。

    过载只丢最旧的 UI 推送。调用者先完成原缓存/统计写入再 submit。
    """

    def __init__(self, loop: Any, on_batch: Callable, max_pending: int = 1024,
                 batch_size: int = 256) -> None:
        self.loop = loop
        self.on_batch = on_batch
        self.max_pending = max_pending
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._pending = deque()
        self._scheduled = False
        self._closed = False
        self._closed_event = None
        self.dropped_count = 0
        self.wakeup_count = 0
        self._last_log = float("-inf")
        self._logged_drops = 0

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def submit(self, quote: Any) -> bool:
        with self._lock:
            if self._closed:
                return False
            if len(self._pending) >= self.max_pending:
                self._pending.popleft()
                self.dropped_count += 1
            self._pending.append(quote)
            if not self._scheduled:
                self._scheduled = True
                try:
                    self.loop.call_soon_threadsafe(self._drain)
                    self.wakeup_count += 1
                except RuntimeError:
                    self._closed = True
                    self._scheduled = False
                    self._pending.clear()
                    return False
        return True

    def _drain(self) -> None:
        with self._lock:
            if self._closed:
                self._scheduled = False
                return
            batch = [self._pending.popleft()
                     for _ in range(min(len(self._pending), self.batch_size))]
            dropped = self.dropped_count
        try:
            self.on_batch(batch)
        except Exception:
            logger.exception("WebSocket 行情推送批次失败")
        finally:
            now = time.monotonic()
            if dropped > self._logged_drops and now - self._last_log >= 5.0:
                logger.warning("WebSocket 行情入口过载，累计丢弃 %d 条旧 UI 推送", dropped)
                self._logged_drops = dropped
                self._last_log = now
            with self._lock:
                if not self._closed and self._pending:
                    self.loop.call_soon(self._drain)
                else:
                    self._scheduled = False

    async def wait_closed(self) -> None:
        if self._closed_event is None:
            self._closed_event = asyncio.Event()
        if self.closed:
            return
        await self._closed_event.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()
        if self._closed_event is not None:
            self._closed_event.set()

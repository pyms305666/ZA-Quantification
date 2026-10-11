"""线程安全的订阅意图与恢复：连接回调只唤醒 worker，不阻塞行情线程。"""
from __future__ import annotations

import threading
from typing import Optional

from .client import TqClient, TqClientError, SymbolNotFoundError
from .instruments import EXCHANGES, InstrumentManager, canonical_instrument


class SubscriptionManager:
    RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

    def __init__(self, client: TqClient, instruments: InstrumentManager) -> None:
        self._client = client
        self._instruments = instruments
        self._lock = threading.RLock()
        # 网络操作串行；状态锁绝不跨命令等待，状态接口/连接回调仍可立即返回。
        self._operations = threading.Lock()
        self._subscribed: dict[str, str] = {}
        self._subscribed_epochs: dict[str, int] = {}
        self._unconfirmed: set[str] = set()
        self._aliases: dict[str, str] = {}
        self._pending: dict[str, int] = {}
        self._raw_cancel: dict[str, int] = {}
        self._canonical_cancel: dict[str, int] = {}
        self._epoch = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: Optional[threading.Thread] = None

    @staticmethod
    def _raw_key(raw: str) -> str:
        return (raw or "").strip().upper()

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._worker = threading.Thread(target=self._recover, name="subscription-recovery", daemon=True)
            self._worker.start()
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def close(self, timeout: float = 3.0) -> None:
        self.request_stop()
        with self._lock:
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        with self._lock:
            if worker is not None and not worker.is_alive() and self._worker is worker:
                self._worker = None

    def on_connected(self) -> None:
        """在 DIFF loop 线程中调用：只通知，SDK 命令全部由恢复线程执行。"""
        self._wake.set()

    def _normalize(self, raw: str) -> Optional[str]:
        """保持公共 normalize 的候选顺序，同时保留裸代码查询的临时错误。"""
        value = (raw or "").strip()
        if not value:
            return None
        upper = value.upper()
        if "." in upper:
            exchange, instrument = upper.split(".", 1)
            if exchange in EXCHANGES and instrument:
                return f"{exchange}.{canonical_instrument(exchange, instrument)}"
            return None
        temporary = None
        for exchange in EXCHANGES:
            candidate = f"{exchange}.{canonical_instrument(exchange, upper)}"
            try:
                self._client.run_command("get_instrument", candidate, timeout=8.0)
                return candidate
            except SymbolNotFoundError:
                continue
            except TqClientError as error:
                temporary = error
        if temporary is not None:
            raise temporary
        return None

    def _current(self, raw: str, epoch: int, normalized: Optional[str] = None) -> bool:
        return (not self._stop.is_set()
                and self._pending.get(raw) == epoch
                and self._raw_cancel.get(self._raw_key(raw), 0) < epoch
                and (normalized is None or self._canonical_cancel.get(normalized, 0) < epoch))

    def _finish(self, raw: str, epoch: int) -> None:
        with self._lock:
            if self._pending.get(raw) == epoch:
                self._pending.pop(raw, None)

    def _attempt(self, raw: str, epoch: int, seen: Optional[set[str]] = None,
                 recovery: bool = False) -> tuple:
        """返回 (状态, 规范代码, 原因)；恢复不创建新 epoch。"""
        with self._operations:
            with self._lock:
                if not self._current(raw, epoch):
                    self._finish(raw, epoch)
                    return "cancelled", None, "订阅请求已取消"
            if not self._client.connected:
                return "retry", None, "天勤未连接，已挂起待重连"
            try:
                normalized = self._normalize(raw)
                if normalized is None:
                    self._finish(raw, epoch)
                    return "failed", None, "合约代码无法解析"
                with self._lock:
                    self._aliases[self._raw_key(raw)] = normalized
                    if not self._current(raw, epoch, normalized):
                        self._finish(raw, epoch)
                        return "cancelled", normalized, "订阅请求已取消"
                    if seen is not None and normalized in seen:
                        self._finish(raw, epoch)
                        return "duplicate", normalized, ""
                    if seen is not None:
                        seen.add(normalized)
                    if normalized in self._subscribed.values() and not (recovery and normalized in self._unconfirmed):
                        # 幂等请求仍是新的订阅意图；迟到的旧退订不能清掉它。
                        self._subscribed[raw] = normalized
                        self._subscribed_epochs[raw] = epoch
                        if normalized not in self._unconfirmed:
                            self._finish(raw, epoch)
                        return "skipped", normalized, ""
                catalog_unavailable = not getattr(self._client, "catalog_ready", True)
                instrument = None
                if not catalog_unavailable:
                    try:
                        instrument = self._instruments.get(normalized)
                    except TqClientError:
                        catalog_unavailable = True
                reason = ""
                if not catalog_unavailable and instrument is None:
                    reason = "合约不存在"
                elif instrument is not None and instrument.expired:
                    reason = "合约已过期"
                elif instrument is not None and instrument.expire_rest_days is not None and instrument.expire_rest_days <= 1:
                    reason = f"合约临近交割（剩余 {instrument.expire_rest_days} 天），拒绝订阅"
                if reason:
                    self._finish(raw, epoch)
                    return "failed", normalized, reason
                with self._lock:
                    if not self._current(raw, epoch, normalized):
                        self._finish(raw, epoch)
                        return "cancelled", normalized, "订阅请求已取消"
                queue_subscription = getattr(self._client, "queue_subscription", None)
                queued = (not recovery and not getattr(self._client, "catalog_complete", True)
                          and callable(queue_subscription))
                if queued:
                    queue_subscription(normalized)
                else:
                    self._client.run_command("subscribe", normalized, timeout=10.0)
                with self._lock:
                    if not self._current(raw, epoch, normalized):
                        self._finish(raw, epoch)
                        return "cancelled", normalized, "订阅请求已取消"
                    self._subscribed[raw] = normalized
                    self._subscribed_epochs[raw] = epoch
                    if queued:
                        self._unconfirmed.add(normalized)
                    else:
                        self._unconfirmed.discard(normalized)
                        self._pending.pop(raw, None)
                return "subscribed", normalized, ""
            except SymbolNotFoundError as error:
                self._finish(raw, epoch)
                return "failed", None, str(error)
            except TqClientError as error:
                return "retry", None, str(error)

    def subscribe(self, symbols: list[str]) -> dict:
        output = {"subscribed": [], "skipped": [], "failed": []}
        seen: set[str] = set()
        raw_seen: set[str] = set()
        for raw in symbols or []:
            if raw in raw_seen:
                continue
            raw_seen.add(raw)
            with self._lock:
                if self._stop.is_set():
                    output["failed"].append({"symbol": raw, "reason": "订阅管理器已关闭"})
                    continue
                self._epoch += 1
                epoch = self._epoch
                self._pending[raw] = epoch
            status, normalized, reason = self._attempt(raw, epoch, seen)
            if status in ("subscribed", "skipped"):
                output[status].append(normalized)
            elif status != "duplicate":
                output["failed"].append({"symbol": raw, "reason": reason})
            with self._lock:
                remains = raw in self._pending
            if remains:
                self._wake.set()
        return output

    def unsubscribe(self, symbols: list[str]) -> dict:
        output = {"unsubscribed": [], "missing": []}
        for raw in symbols or []:
            key = self._raw_key(raw)
            with self._lock:
                self._epoch += 1
                epoch = self._epoch
                self._raw_cancel[key] = epoch
                known_pending = any(self._raw_key(value) == key for value in self._pending)
                for value in list(self._pending):
                    if self._raw_key(value) == key:
                        self._pending.pop(value, None)
                normalized = self._aliases.get(key)
            try:
                normalized = normalized or self._normalize(raw)
            except TqClientError:
                normalized = None
            if normalized is None:
                output["missing"].append(raw)
                continue
            with self._lock:
                self._canonical_cancel[normalized] = max(epoch, self._canonical_cancel.get(normalized, 0))
                pending_alias = any(value_epoch <= epoch and self._aliases.get(self._raw_key(value)) == normalized
                                    for value, value_epoch in self._pending.items())
                # 本地意图先取消：网络超时也不能让之后的新订阅被旧映射 skipped。
                for value in list(self._subscribed):
                    if self._subscribed[value] == normalized and self._subscribed_epochs.get(value, 0) <= epoch:
                        self._subscribed.pop(value, None)
                        self._subscribed_epochs.pop(value, None)
                self._unconfirmed.discard(normalized)
            with self._operations:
                with self._lock:
                    newer = any(value_epoch > epoch and self._aliases.get(self._raw_key(value)) == normalized
                                for value, value_epoch in self._pending.items())
                    newer = newer or any(value == normalized and self._subscribed_epochs.get(key, 0) > epoch
                                         for key, value in self._subscribed.items())
                if newer:
                    output["unsubscribed"].append(normalized)
                    continue
                if self._client.connected:
                    try:
                        self._client.run_command("unsubscribe", normalized, timeout=5.0)
                    except TqClientError:
                        output["missing"].append(raw)
                        continue
                elif not (known_pending or pending_alias):
                    output["missing"].append(raw)
                    continue
                with self._lock:
                    for value, value_epoch in list(self._pending.items()):
                        if value_epoch <= epoch and self._aliases.get(self._raw_key(value)) == normalized:
                            self._pending.pop(value, None)
                output["unsubscribed"].append(normalized)
        return output

    def subscribed(self) -> list[str]:
        with self._lock:
            snapshot = list(self._subscribed.values())
        return sorted(set(snapshot))

    def _recover(self) -> None:
        delay_index = 0
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            while not self._stop.is_set() and self._client.connected:
                with self._lock:
                    pending = list(self._pending.items())
                if not pending:
                    break
                for raw, epoch in pending:
                    if self._stop.is_set():
                        return
                    self._attempt(raw, epoch, recovery=True)
                with self._lock:
                    remains = bool(self._pending)
                if not remains:
                    delay_index = 0
                    break
                delay = self.RETRY_DELAYS[min(delay_index, len(self.RETRY_DELAYS) - 1)]
                notified = self._wake.wait(delay)
                self._wake.clear()
                delay_index = 0 if notified else delay_index + 1

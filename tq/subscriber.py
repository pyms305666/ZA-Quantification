"""动态订阅：软件侧按需订阅 / 退订，幂等，失败带原因。

          客户端
            │  subscribe / unsubscribe
            ▼
    SubscriptionManager
      ├── 已订阅？ → 跳过
      └── 未订阅？ → TqClient.subscribe（get_quote）→ 事件循环线程
                        ↓
                     Quote 实体 → is_changing → MarketQuote → 缓存 + 推送
"""

from __future__ import annotations

from typing import Optional

from .client import TqClient, TqClientError
from .instruments import InstrumentManager, normalize_symbol


class SubscriptionManager:
    def __init__(self, client: TqClient, instruments: InstrumentManager) -> None:
        self._client = client
        self._instruments = instruments
        self._subscribed: dict[str, str] = {}  # 原始写法 -> 归一化代码
        # 缺陷 A/F：连接建立后自动重放挂起的订阅。
        # 前端 onopen 只发一次且不重试，连接未就绪时整批被拒就永久丢失；
        # 这里记录 pending，连接就绪后按表重放，不依赖前端。
        self._pending: set[str] = set()

    def subscribe(self, symbols: list[str]) -> dict:
        """批量订阅。返回 ``{subscribed, skipped, failed}``，failed 每项含原因。"""
        if not self._client.connected:
            # 缺陷 A：未连接时不再"整批拒绝即丢"，先记入 pending，连接建立后由
            # on_connected 自动重放（见 on_connected）。
            for raw in symbols or []:
                self._pending.add(raw)
            return {"subscribed": [], "skipped": [],
                    "failed": [{"symbol": raw, "reason": "天勤未连接，已挂起待重连"} for raw in (symbols or [])]}
        subscribed: list[str] = []
        skipped: list[str] = []
        failed: list[dict] = []
        seen: set[str] = set()
        for raw in symbols or []:
            normalized = normalize_symbol(self._client, raw)
            if normalized is None:
                failed.append({"symbol": raw, "reason": "合约代码无法解析"})
                continue
            if normalized in self._subscribed.values() or normalized in seen:
                if normalized not in seen:
                    seen.add(normalized)
                    skipped.append(normalized)
                continue
            seen.add(normalized)
            catalog_unavailable = not getattr(self._client, "catalog_ready", True)
            instrument = None
            if not catalog_unavailable:
                try:
                    instrument = self._instruments.get(normalized)
                except TqClientError:
                    # 目录未就绪/查询失败（如"合约目录后台下载中"）：降级放行，
                    # 由行情服务器校验合约是否存在，规避"目录下载中误报合约不存在"。
                    catalog_unavailable = True
            if not catalog_unavailable and instrument is None:
                # 目录已就绪但查无此合约（get 仅在 SymbolNotFound 时返回 None）：严格拒绝
                failed.append({"symbol": raw, "reason": "合约不存在"})
                continue
            if instrument is not None:
                if instrument.expired:
                    failed.append({"symbol": raw, "reason": "合约已过期"})
                    continue
                if instrument.expire_rest_days is not None and instrument.expire_rest_days <= 1:
                    failed.append({"symbol": raw, "reason": f"合约临近交割（剩余 {instrument.expire_rest_days} 天），拒绝订阅"})
                    continue
            # 目录未就绪（跳过本地查询）或有目录记录 → 继续提交订阅
            try:
                self._client.run_command("subscribe", normalized, timeout=10.0)
            except TqClientError as error:
                failed.append({"symbol": raw, "reason": str(error)})
                continue
            self._subscribed[raw] = normalized
            subscribed.append(normalized)
        return {"subscribed": subscribed, "skipped": skipped, "failed": failed}

    def unsubscribe(self, symbols: list[str]) -> dict:
        unsubscribed: list[str] = []
        missing: list[str] = []
        for raw in symbols or []:
            normalized = self._normalize_known(raw)
            if normalized is None:
                missing.append(raw)
                continue
            try:
                self._client.run_command("unsubscribe", normalized, timeout=5.0)
            except TqClientError:
                missing.append(raw)
                continue
            self._subscribed = {k: v for k, v in self._subscribed.items() if v != normalized}
            unsubscribed.append(normalized)
        return {"unsubscribed": unsubscribed, "missing": missing}

    def subscribed(self) -> list[str]:
        return sorted(set(self._subscribed.values()))

    def on_connected(self) -> None:
        """连接建立（含重连）后重放挂起的订阅（缺陷 A/F 后端核心）。

        由 Services.build_services 注册为 DiffClient 的 on_connected 回调，
        在每次 WebSocket 连接建立时被调用。重放后清空 pending；仍在 pending
        说明此刻又掉线了，下一轮重连会再次重放。
        """
        pending = list(self._pending)
        self._pending.clear()
        if pending:
            self.subscribe(pending)

    def _normalize_known(self, raw: str) -> Optional[str]:
        normalized = self._subscribed.get(raw)
        if normalized is not None:
            return normalized
        for value in self._subscribed.values():
            if value == raw:
                return value
        return normalize_symbol(self._client, raw) if raw else None

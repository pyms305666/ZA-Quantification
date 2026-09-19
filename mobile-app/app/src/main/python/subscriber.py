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
        """订阅管理器：前端 WS 的订阅请求统一经这里进入客户端命令层。

        Args:
            client: 行情客户端（提交 subscribe/unsubscribe 命令）。
            instruments: 合约管理器（订阅前校验存在性/过期/临近交割）。
        """
        self._client = client
        self._instruments = instruments
        self._subscribed: dict[str, str] = {}  # 原始写法 -> 归一化代码
        # 缺陷 A/F：连接建立后自动重放挂起的订阅。
        # 前端 onopen 只发一次且不重试，连接未就绪时整批被拒就永久丢失；
        # 这里记录 pending，连接就绪后按表重放，不依赖前端。
        self._pending: set[str] = set()

    def subscribe(self, symbols: list[str]) -> dict:
        """批量订阅合约，逐只校验并分流，全程幂等（重复订阅静默跳过）。

        每只合约的判定链：
        1. 未连接 → 记入 pending（重连后 on_connected 自动重放），报"已挂起"；
        2. 代码无法规范化 → failed；
        3. 已订阅/批内重复 → skipped；
        4. 目录已完整且查无此合约 → failed"合约不存在"（严格拒绝）；
        5. 已过期 / 剩余 ≤1 天（临近交割）→ failed；
        6. 目录不完整 → queue_subscription 异步排队（不阻塞 HTTP）；
        7. 其余 → 正常提交订阅命令。

        Args:
            symbols: 任意写法的合约代码列表（可混合大小写/有无前缀）。

        Returns:
            {"subscribed": [规范化代码], "skipped": [规范化代码],
             "failed": [{"symbol": 原始输入, "reason": 原因}]}
        """
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
            if not getattr(self._client, "catalog_complete", True):
                queue_subscription = getattr(self._client, "queue_subscription", None)
                if callable(queue_subscription):
                    queue_subscription(normalized)
                    self._subscribed[raw] = normalized
                    subscribed.append(normalized)
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
        """批量退订：支持原始写法或规范化代码两种输入，幂等。

        Args:
            symbols: 要退订的合约代码列表。

        Returns:
            {"unsubscribed": [规范化代码], "missing": [未识别/退订失败的原始输入]}
        """
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
        """当前全部已订阅合约的规范化代码（去重排序）。"""
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
        """退订用：优先按订阅表反查规范化代码（避免重新走可能失败的目录查询）。"""
        normalized = self._subscribed.get(raw)
        if normalized is not None:
            return normalized
        for value in self._subscribed.values():
            if value == raw:
                return value
        return normalize_symbol(self._client, raw) if raw else None

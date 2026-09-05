"""合约发现：不手工维护合约列表，从 TqSdk 动态建立自己的合约目录。

- 期货：query_quotes(FUTURE) 全量拉取（首次调用后缓存）。
- 期权：按标的懒查询 query_options(underlying)，不默认订阅。
- 记录统一为 market.model.Instrument 结构（symbol / exchange / instrument_id /
  name / expired / price_tick / volume_multiple）。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from market.model import EXCHANGES, Instrument
from .client import TqClient, TqClientError

# 国内交易所合约代码大小写规范：上期所/大商所/能源/广期所小写，中金所/郑商所大写。
EXCHANGE_INSTRUMENT_CASE: dict[str, str] = {
    "SHFE": "lower", "DCE": "lower", "INE": "lower", "GFEX": "lower",
    "CZCE": "upper", "CFFEX": "upper",
}


def canonical_instrument(exchange: str, instrument: str) -> str:
    """按交易所规范返回合约代码大小写（如 IF2609、sr609）。"""
    case = EXCHANGE_INSTRUMENT_CASE.get(exchange.upper(), "lower")
    return instrument.upper() if case == "upper" else instrument.lower()


def build_symbol_variants(exchange: str, instrument: str) -> list[str]:
    """给定交易所与代码，生成去重后的候选写法（规范写法优先）。"""
    variants: list[str] = []
    canonical = canonical_instrument(exchange, instrument)
    for value in (canonical, instrument.upper(), instrument.lower(), instrument):
        candidate = f"{exchange.upper()}.{value}"
        if candidate not in variants:
            variants.append(candidate)
    return variants


class InstrumentManager:
    def __init__(self, client: TqClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._futures: Optional[list[str]] = None
        self._info_cache: dict[str, Instrument] = {}
        self._failed_cache: dict[str, float] = {}  # 失败查询的冷却（避免轮询反复触发超时命令）
        self._failed_cooldown = 30.0
        # B 路线：期货代码列表 + 逐批合约信息全部由 TqApi 事件循环上的后台协程完成，
        # 本管理器只提供钩子，HTTP 层永远不阻塞等目录。
        client.set_catalog_hooks(
            self._needs_futures, self._on_futures,
            self._next_catalog_batch, self._on_catalog_result)

    # ----------------------------- 目录后台协程钩子（均在事件循环线程内被调用）

    def _needs_futures(self) -> bool:
        with self._lock:
            return self._futures is None

    def _on_futures(self, symbols: list[str]) -> None:
        with self._lock:
            self._futures = list(symbols)

    def _next_catalog_batch(self) -> Optional[list[str]]:
        """取下一批待查合约（<=50 个，跳过已缓存与冷却中的）；无待查返回 None。"""
        with self._lock:
            if self._futures is None:
                return None
            now = time.monotonic()
            pending: list[str] = []
            for symbol in self._futures:
                if symbol in self._info_cache:
                    continue
                cooled = self._failed_cache.get(symbol)
                if cooled is not None and now - cooled < self._failed_cooldown:
                    continue
                pending.append(symbol)
                if len(pending) >= 50:
                    break
            return pending or None

    def _on_catalog_result(self, batch: list[str], records: Optional[list[dict]]) -> None:
        """批量查询完成：逐个写入缓存；records 为 None 表示整批失败，整批冷却。"""
        with self._lock:
            now = time.monotonic()
            if records is None:
                for symbol in batch:
                    self._failed_cache[symbol] = now
                return
            for symbol, record in zip(batch, records):
                if not record:
                    self._failed_cache[symbol] = now
                    continue
                self._info_cache[symbol] = Instrument(
                    symbol=record["symbol"],
                    exchange=record["exchange"],
                    instrument_id=record["instrument_id"],
                    name=record["name"],
                    kind=record["kind"],
                    expired=record["expired"],
                    price_tick=record["price_tick"],
                    volume_multiple=record["volume_multiple"],
                )

    # -------------------------------------------------- 对外接口（全部非阻塞）

    def progress(self) -> dict:
        """目录加载进度（供 status / instruments 接口向前端展示）。"""
        with self._lock:
            total = len(self._futures) if self._futures is not None else None
            cached = len(self._info_cache)
            done = total is not None and cached >= total
            return {"futures_total": total, "info_cached": cached, "done": done}

    def futures(self) -> list[str]:
        """已知的全部期货行情代码（只读缓存，列表由后台协程填充）。"""
        with self._lock:
            return list(self._futures or [])

    def get(self, symbol: str) -> Optional[Instrument]:
        """单个合约的目录记录（优先读缓存；未命中走单条查询，失败冷却）。"""
        normalized = normalize_symbol(self._client, symbol)
        if normalized is None:
            return None
        with self._lock:
            cached = self._info_cache.get(normalized)
            if cached is not None:
                return cached
            cooled = self._failed_cache.get(normalized)
            if cooled is not None:
                if time.monotonic() - cooled < 10.0:
                    return None
                self._failed_cache.pop(normalized, None)
        try:
            record = self._client.run_command("get_instrument", normalized, timeout=8.0)
        except TqClientError:
            with self._lock:
                self._failed_cache[normalized] = time.monotonic()
            return None
        item = Instrument(
            symbol=record["symbol"],
            exchange=record["exchange"],
            instrument_id=record["instrument_id"],
            name=record["name"],
            kind=record["kind"],
            expired=record["expired"],
            price_tick=record["price_tick"],
            volume_multiple=record["volume_multiple"],
            expire_rest_days=record.get("expire_rest_days"),
        )
        with self._lock:
            self._info_cache[normalized] = item
        return item

    def kick_catalog(self) -> None:
        """确保目录后台协程已启动（幂等，连接未就绪时静默跳过）。"""
        try:
            self._client.run_command("start_catalog_worker", timeout=3.0)
        except TqClientError:
            pass

    def list(self, exchange: str = "", keyword: str = "", refresh: bool = False) -> list[dict]:
        """合约目录（只读缓存，绝不阻塞）。

        exchange 为 ``SHFE`` 等交易所代码；keyword 匹配代码/名称。
        尚未回写的合约等后台协程完成后，由前端按 progress 再次轮询获取。
        """
        self.kick_catalog()
        if refresh:
            with self._lock:
                self._futures = None
                self._info_cache.clear()
                self._failed_cache.clear()
        exchange = exchange.upper()
        keyword_lower = (keyword or "").lower()
        with self._lock:
            snapshot = list(self._info_cache.values())
        output: list[Instrument] = []
        for item in snapshot:
            if exchange and item.exchange != exchange:
                continue
            if keyword_lower:
                haystack = f"{item.symbol} {item.instrument_id} {item.name}".lower()
                if keyword_lower not in haystack:
                    continue
            output.append(item)
        output.sort(key=lambda item: item.symbol)
        return [item.to_dict() for item in output]


def normalize_symbol(client: TqClient, symbol: str) -> Optional[str]:
    """把任意写法归一化成 TqSdk 规范代码（按交易所决定大小写）。

    已带交易所前缀时直接规范化；裸代码依次尝试六个交易所前缀，
    用 get_instrument 确认合约真实存在，避免手工维护 品种->交易所 映射表。
    """
    value = (symbol or "").strip()
    if not value:
        return None
    upper = value.upper()
    if "." in upper:
        exchange, instrument = upper.split(".", 1)
        if exchange in EXCHANGES and instrument:
            return f"{exchange}.{canonical_instrument(exchange, instrument)}"
        return None
    for exchange in EXCHANGES:
        candidate = f"{exchange}.{canonical_instrument(exchange, upper)}"
        try:
            client.run_command("get_instrument", candidate, timeout=8.0)
            return candidate
        except Exception:
            continue
    return None

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

    def futures(self, refresh: bool = False) -> list[str]:
        """全部期货行情代码（如 ``SHFE.rb2610``），首次从 TqSdk 拉取后缓存。"""
        with self._lock:
            if self._futures is None or refresh:
                symbols = self._client.run_command("query_instruments", timeout=30.0)
                self._futures = symbols
            return list(self._futures)

    def list(self, exchange: str = "", keyword: str = "", refresh: bool = False) -> list[dict]:
        """合约目录。exchange 为 ``SHFE`` 等交易所代码；keyword 匹配代码/名称。

        先按代码本地预过滤，再对剩余合约批量查询信息（绝不逐合约单查）。
        """
        exchange = exchange.upper()
        code_keyword = keyword.lower()
        candidates: list[str] = []
        for symbol in self.futures(refresh=refresh):
            if exchange and not symbol.startswith(exchange + "."):
                continue
            if code_keyword and code_keyword not in symbol.lower():
                continue
            candidates.append(symbol)
        if not candidates:
            return []
        missing = [symbol for symbol in candidates if symbol not in self._info_cache]
        if missing:
            records = self._client.run_command("get_instruments_info", missing, timeout=120.0)
            with self._lock:
                for symbol, record in records.items():
                    if not record:
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
        output: list[Instrument] = []
        for symbol in candidates:
            item = self._info_cache.get(symbol)
            if item is None:
                continue
            if keyword:
                haystack = f"{item.symbol} {item.instrument_id} {item.name}".lower()
                if keyword.lower() not in haystack:
                    continue
            output.append(item)
        return [item.to_dict() for item in output]

    def get(self, symbol: str) -> Optional[Instrument]:
        """单个合约的目录记录（带缓存；失败结果冷却 30 秒）。"""
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

    def options(self, underlying: str) -> list[str]:
        """某标的所有期权行情代码（按需查询，不订阅）。"""
        normalized = normalize_symbol(self._client, underlying)
        if normalized is None:
            raise TqClientError(f"无法解析标的合约：{underlying}")
        return self._client.run_command("query_options", normalized, timeout=20.0)


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

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
from .client import SymbolNotFoundError, TqClient, TqClientError

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
        self._cached_complete = False   # _futures 建立时目录是否已完整
        self._info_cache: dict[str, Instrument] = {}
        self._failed_cache: dict[str, float] = {}  # 失败查询的冷却（避免轮询反复触发超时命令）

    def futures(self, refresh: bool = False) -> list[str]:
        """全部期货行情代码（如 ``SHFE.rb2610``），首次从 TqSdk 拉取后缓存。

        v1.1.3 桌面验收发现的缺陷：首查几乎总发生在冷启动、目录还是内置兜底时，
        若缓存不感知完整性，内置目录会被永久钉死（/status 轮询一直端着 156 条，
        完整目录下载完成后也不更新，直到重启）。因此缓存记录建立时的完整性，
        目录转为完整后的首次访问自动重查一次（仅一次，不增加常态开销）。
        """
        with self._lock:
            complete = bool(getattr(self._client, "catalog_complete", False))
            if self._futures is None or refresh or (complete and not self._cached_complete):
                symbols = self._client.run_command("query_instruments", timeout=30.0)
                self._futures = symbols
                self._cached_complete = complete
            return list(self._futures)

    def list(self, exchange: str = "", keyword: str = "", refresh: bool = False,
             limit: int = 0) -> list[dict]:
        """合约目录列表查询（搜索框与合约列表页的唯一入口）。

        执行流程：futures() 取全部候选 → 按交易所前缀过滤 → 关键字后置匹配
        （代码或中文名）→ 批量补齐 record（一次 get_instruments_info 命令，
        避免逐个查询的超时放大）→ 按 symbol 排序返回。

        Args:
            exchange: 交易所代码过滤（"SHFE" 等；空 = 全部）。
            keyword: 关键字；同时匹配代码与中文名（缺陷 E 修复：不再按代码预过滤）。
            refresh: True 时强制重查合约清单（绕过 _futures 缓存）。
            limit: 无关键字时的截断上限（目录未就绪时的启动保护）；0 = 不限制。

        Returns:
            精简 record 字典列表，按 symbol 排序。
        """
        exchange = exchange.upper()
        code_keyword = keyword.lower()
        candidates: list[str] = []
        for symbol in self.futures(refresh=refresh):
            if exchange and not symbol.startswith(exchange + "."):
                continue
            # 缺陷 E：关键字存在时不按代码预过滤——中文名（如"棕榈"）的合约代码
            # 里不含该字，会被提前丢弃，后置名称匹配永远收不到它。
            # 仅在"无关键字 + limit"时按代码截断（启动保护）。
            if not keyword and code_keyword and code_keyword not in symbol.lower():
                continue
            candidates.append(symbol)
            if limit and not keyword and len(candidates) >= limit:
                break
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
        """按代码查单只合约，返回面向展示/评估的 Instrument 对象。

        查询链路：normalize_symbol 规范化代码（自动补交易所前缀/统一大小写）
        → 走客户端命令 get_instrument 取 record → 转成 Instrument
        （失败有 _failed_cache 冷却，避免前端轮询反复触发慢查询）。

        Args:
            symbol: 任意形态的合约代码（"rb2610"/"SHFE.rb2610"/"SR609" 均可）。

        Returns:
            Instrument 实例；查不到返回 None。
        """
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
        except SymbolNotFoundError:
            # 目录已就绪但查无此合约：get 返回 None 仅此一种含义，供订阅层严格拒绝
            with self._lock:
                self._failed_cache[normalized] = time.monotonic()
            return None
        except TqClientError:
            # 目录未就绪等暂时性失败：向上抛，由订阅层决定降级
            raise
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

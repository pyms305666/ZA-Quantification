"""标准行情数据结构：对外接口只暴露本模块定义的数据，客户端无需知道 TqSdk 内部结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 国内期货交易所代码
EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")


def split_symbol(symbol: str) -> tuple[str, str]:
    """``SHFE.rb2610`` -> (``SHFE``, ``rb2610``)；无法拆分时 exchange 为空。"""
    value = (symbol or "").strip()
    if "." in value:
        exchange, _, instrument_id = value.partition(".")
        return exchange.strip().upper(), instrument_id.strip().lower()
    return "", value.lower()


@dataclass(frozen=True)
class QuoteLevel:
    """单档盘口。"""

    price: Optional[float]
    volume: Optional[float]

    def to_dict(self) -> dict:
        return {"price": self.price, "volume": self.volume}


@dataclass
class MarketQuote:
    """统一后的标准行情（五档盘口，买一在前）。"""

    symbol: str
    exchange: str
    instrument_id: str
    timestamp: Optional[int]  # epoch 毫秒
    last: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    pre_close: Optional[float]
    volume: Optional[float]  # 累计成交量（手）
    open_interest: Optional[float]  # 持仓量（手）
    bid: list[QuoteLevel] = field(default_factory=list)
    ask: list[QuoteLevel] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "instrument_id": self.instrument_id,
            "timestamp": self.timestamp,
            "last": self.last,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "pre_close": self.pre_close,
            "volume": self.volume,
            "open_interest": self.open_interest,
            "bid": [level.to_dict() for level in self.bid],
            "ask": [level.to_dict() for level in self.ask],
        }


@dataclass
class Instrument:
    """标准合约目录条目。"""

    symbol: str
    exchange: str
    instrument_id: str
    name: str = ""
    kind: str = "FUTURE"
    expired: bool = False
    price_tick: Optional[float] = None
    volume_multiple: Optional[int] = None
    expire_rest_days: Optional[int] = None  # 距到期剩余自然日

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "instrument_id": self.instrument_id,
            "name": self.name,
            "kind": self.kind,
            "expired": self.expired,
            "price_tick": self.price_tick,
            "volume_multiple": self.volume_multiple,
            "expire_rest_days": self.expire_rest_days,
        }

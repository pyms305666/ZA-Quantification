"""行情归一化：把 TqSdk 的 Quote 实体转换成统一的标准行情对象。

本模块不 import tqsdk，任何实现同样字段的行情对象（或测试替身）都可以转换。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from .model import MarketQuote, QuoteLevel, split_symbol


def _number(value: Any) -> Optional[float]:
    """把任意来源的字段值安全转成有限浮点数（None/非数值/NaN/Inf 一律归 None）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _epoch_ms(value: Any) -> Optional[int]:
    """把行情时间戳归一化为 epoch 毫秒整数（兼容 numpy datetime64 与普通数值）。"""
    if value is None:
        return None
    # TqSdk 的 datetime 是 numpy.datetime64(ns)，转成 epoch 毫秒。
    astype = getattr(value, "astype", None)
    if astype is not None:
        try:
            return int(astype("datetime64[ms]").astype("int64"))
        except (TypeError, ValueError):
            pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _levels(quote: Any, side: str) -> list[QuoteLevel]:
    """提取五档盘口的某一侧（bid/ask），遇到首个全空档位即截断。"""
    output: list[QuoteLevel] = []
    for index in range(1, 6):
        price = _number(getattr(quote, f"{side}_price{index}", None))
        volume = _number(getattr(quote, f"{side}_volume{index}", None))
        if price is None and volume is None:
            break  # 之后档位都为空，截断到有效深度
        output.append(QuoteLevel(price, volume))
    return output


def to_market_quote(symbol: str, quote: Any) -> Optional[MarketQuote]:
    """把 TqSdk 风格的 Quote 对象归一化为 :class:`MarketQuote`（行情链路的统一出口）。

    任何实现同样属性的对象（TqSdk 实体、DIFF 快照的 SimpleNamespace、测试替身）
    都可以转换——本模块不 import tqsdk。

    Args:
        symbol: 合约完整代码。 quote: 带 last_price/datetime/五档属性的行情对象。

    Returns:
        MarketQuote 实例；无有效最新价（尚未收到首笔行情）返回 None。
    """
    exchange, instrument_id = split_symbol(symbol)
    last = _number(getattr(quote, "last_price", None))
    if last is None:
        return None
    return MarketQuote(
        symbol=symbol,
        exchange=exchange,
        instrument_id=instrument_id,
        timestamp=_epoch_ms(getattr(quote, "datetime", None)),
        last=last,
        open=_number(getattr(quote, "open", None)),
        high=_number(getattr(quote, "high", None)),
        low=_number(getattr(quote, "low", None)),
        pre_close=_number(getattr(quote, "pre_close", None)),
        volume=_number(getattr(quote, "volume", None)),
        open_interest=_number(getattr(quote, "open_interest", None)),
        bid=_levels(quote, "bid"),
        ask=_levels(quote, "ask"),
    )

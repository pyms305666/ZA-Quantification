"""技术指标库：纯 Python 实现，输入收盘价/最高/最低等数值列表，输出最新指标值。

所有函数在数据不足时返回 None（调用方按“无该指标”处理，绝不伪造）。
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

Number = float


def sma(values: Sequence[Number], period: int) -> Optional[Number]:
    """简单移动平均（最新值）。

    Args: values: 数值序列（时间升序）。 period: 窗口长度。
    Returns: 最近 period 根的算术平均；数据不足或 period 非法返回 None。
    """
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[Number], period: int) -> Optional[Number]:
    """指数移动平均（最新值）：首个值用前 period 根的平均做种子。

    递推式 EMA(t) = EMA(t-1) + k×(close - EMA(t-1))，k = 2/(period+1)；
    与主流行情软件一致。

    Returns: 最新 EMA；数据不足或 period 非法返回 None。
    """
    if len(values) < period or period <= 0:
        return None
    seed = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1)
    value = seed
    for close in values[period:]:
        value = (close - value) * multiplier + value
    return value


def macd(closes: Sequence[Number], fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD：快慢 EMA 差值体系（参数 12/26/9）。

    Returns: (DIF=快EMA-慢EMA, DEA=DIF 的 signal 期 EMA, 柱=(DIF-DEA)×2)；
    序列长度不足 slow+signal 时返回 (None, None, None)。
    """
    if len(closes) < slow + signal:
        return None, None, None
    dif_list: list[float] = []
    # 用完整序列迭代计算 DIF 序列，保证 EMA 递推一致。
    fast_ema: Optional[float] = None
    slow_ema: Optional[float] = None
    fast_k = 2.0 / (fast + 1)
    slow_k = 2.0 / (slow + 1)
    for index, close in enumerate(closes):
        if index < slow - 1:
            continue
        window = closes[: index + 1]
        fast_ema = sum(window[-fast:]) / fast if fast_ema is None else (close - fast_ema) * fast_k + fast_ema
        slow_ema = sum(window[-slow:]) / slow if slow_ema is None else (close - slow_ema) * slow_k + slow_ema
        dif_list.append(fast_ema - slow_ema)
    dea: Optional[float] = None
    dea_k = 2.0 / (signal + 1)
    dif_history: list[float] = []
    for dif in dif_list:
        dif_history.append(dif)
        if len(dif_history) == signal:
            dea = sum(dif_history) / signal
        elif dea is not None:
            dea = (dif - dea) * dea_k + dea
    if dea is None:
        return None, None, None
    dif = dif_list[-1]
    return dif, dea, (dif - dea) * 2


def rsi(closes: Sequence[Number], period: int = 14) -> Optional[Number]:
    """RSI 相对强弱指数（Wilder 平滑，非简单均值）。

    Returns: 最新 RSI（0-100，>70 超买 / <30 超卖）；平均亏损为 0 时返回 100；
    数据不足返回 None。
    """
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def kdj(highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number],
        n: int = 9, k_period: int = 3, d_period: int = 3):
    """KDJ 随机指标（9,3,3）：K/D 为 RSV 的两级平滑，J=3K-2D。

    Returns: (K, D, J) 最新值；数据不足返回 (None, None, None)。
    """
    if len(closes) < n + k_period + d_period - 2:
        return None, None, None
    k_value = 50.0
    d_value = 50.0
    for index in range(n - 1, len(closes)):
        window_high = max(highs[index - n + 1: index + 1])
        window_low = min(lows[index - n + 1: index + 1])
        span = window_high - window_low
        rsv = 50.0 if span <= 0 else (closes[index] - window_low) / span * 100.0
        k_value = (k_value * (k_period - 1) + rsv) / k_period
        d_value = (d_value * (d_period - 1) + k_value) / d_period
    j_value = 3 * k_value - 2 * d_value
    return k_value, d_value, j_value


def boll(closes: Sequence[Number], period: int = 20, width: float = 2.0):
    """布林带：中轨为 SMA(period)，上下轨为中轨 ± width 倍标准差。

    Returns: (中轨, 上轨, 下轨)；中轨不可得时返回 (None, None, None)。
    """
    mid = sma(closes, period)
    if mid is None:
        return None, None, None
    window = closes[-period:]
    variance = sum((value - mid) ** 2 for value in window) / period
    stddev = math.sqrt(variance)
    return mid, mid + width * stddev, mid - width * stddev


def atr(highs: Sequence[Number], lows: Sequence[Number], closes: Sequence[Number],
        period: int = 14) -> Optional[Number]:
    """ATR 平均真实波幅（Wilder 平滑）：衡量波动水平，用于止损距离计算。

    真实波幅 = max(当根高低差, |最高-前收|, |最低-前收|)。
    Returns: 最新 ATR；数据不足返回 None。
    """
    if len(closes) < period + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(closes)):
        true_range = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        ranges.append(true_range)
    value = sum(ranges[:period]) / period
    for index in range(period, len(ranges)):
        value = (value * (period - 1) + ranges[index]) / period
    return value

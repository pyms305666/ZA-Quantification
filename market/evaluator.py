"""决策评估引擎：多周期、多因子评分，输出做多/做空/观望建议与交易参数。

评分构成（0-100，多空对称）：
- 趋势因子 40 分：日线 + 60 分钟 EMA20/60 排列、MACD 方向、多周期共振
- 动量因子 25 分：5 分钟/60 分钟 20 周期突破、RSI 区间、KDJ 金叉死叉
- 量仓因子 20 分：放量确认、持仓量增减与价格方向
- 风险因子 15 分：ATR 波动水平、布林带位置

信号门槛：总分 >= 60 且多空分差 >= 15，否则观望。
风控：单笔最大亏损（默认 900 元）→ 手数 = 亏损额 ÷（止损距离 × 合约乘数）。

只输出评估建议，不自动交易。
"""

from __future__ import annotations

import math
from typing import Optional

from config import RiskConfig
from .indicators import atr, boll, ema, kdj, macd, rsi, sma
from .model import Instrument

PERIOD_DAILY = 86400
PERIOD_H60 = 3600
PERIOD_M15 = 900
PERIOD_M5 = 300

SIGNAL_LONG = "做多"
SIGNAL_SHORT = "做空"
SIGNAL_FLAT = "观望"

MIN_SCORE = 60
MIN_GAP = 15


def _closes(bars: list[dict]) -> list[float]:
    return [float(bar["close"]) for bar in bars]


def _highs(bars: list[dict]) -> list[float]:
    return [float(bar["high"]) for bar in bars]


def _lows(bars: list[dict]) -> list[float]:
    return [float(bar["low"]) for bar in bars]


def _volumes(bars: list[dict]) -> list[float]:
    return [float(bar["volume"]) for bar in bars]


def _round_tick(value: float, tick: float, upward: bool = False) -> float:
    if tick <= 0:
        return value
    rounded = math.ceil(value / tick) * tick if upward else math.floor(value / tick) * tick
    return round(rounded, max(0, int(-math.floor(math.log10(tick))) + 1))


def _ma_state(ema_short: Optional[float], ema_long: Optional[float], price: float) -> int:
    """均线状态：1=多头排列，-1=空头排列，0=粘合（死区 0.05% × 价格）。"""
    if ema_short is None or ema_long is None:
        return 0
    threshold = max(abs(price), 1.0) * 0.0005
    if ema_short - ema_long > threshold:
        return 1
    if ema_long - ema_short > threshold:
        return -1
    return 0


def _macd_state(closes: list[float]) -> int:
    """MACD 状态：1=金叉，-1=死叉，0=粘合。

    死区取 MACD 自身量级的 0.1%（下限 0.001）：线性序列 DIF==DEA 判为粘合，
    真实趋势（DIF 与 DEA 拉开）正常触发。
    """
    dif, dea, _ = macd(closes)
    if dif is None or dea is None:
        return 0
    threshold = max(abs(dif), abs(dea), 1.0) * 1e-3
    gap = dif - dea
    if gap > threshold:
        return 1
    if gap < -threshold:
        return -1
    return 0


def _trend_factor(daily: list[dict], h60: list[dict]) -> tuple[float, float, list[str]]:
    """趋势因子（满分 40）。返回 (多分, 空分, 依据列表)。"""
    long_score, short_score = 0.0, 0.0
    notes: list[str] = []
    daily_closes = _closes(daily)
    h60_closes = _closes(h60)

    # 日线（24 分）
    if len(daily_closes) >= 30:
        ema20, ema60 = ema(daily_closes, 20), ema(daily_closes, 60)
        ma_state = _ma_state(ema20, ema60, daily_closes[-1])
        macd_state = _macd_state(daily_closes)
        if ma_state > 0:
            long_score += 6
            notes.append("日线 EMA20>EMA60 多头排列（+6）")
        elif ma_state < 0:
            short_score += 6
            notes.append("日线 EMA20<EMA60 空头排列（+6）")
        if ema20 is not None:
            threshold = max(abs(daily_closes[-1]), 1.0) * 0.0005
            if daily_closes[-1] > ema20 + threshold:
                long_score += 6
                notes.append("日线收盘站上 EMA20（+6）")
            elif daily_closes[-1] < ema20 - threshold:
                short_score += 6
                notes.append("日线收盘跌破 EMA20（+6）")
        if macd_state > 0:
            long_score += 6
            notes.append("日线 MACD 金叉（+6）")
        elif macd_state < 0:
            short_score += 6
            notes.append("日线 MACD 死叉（+6）")
        if ma_state > 0 and macd_state > 0:
            long_score += 6
            notes.append("日线均线+MACD 多头共振（+6）")
        elif ma_state < 0 and macd_state < 0:
            short_score += 6
            notes.append("日线均线+MACD 空头共振（+6）")

    # 60 分钟（16 分）
    if len(h60_closes) >= 30:
        ema20, ema60 = ema(h60_closes, 20), ema(h60_closes, 60)
        ma_state = _ma_state(ema20, ema60, h60_closes[-1])
        macd_state = _macd_state(h60_closes)
        if ma_state > 0:
            long_score += 4
            notes.append("60分 EMA20>EMA60 多头排列（+4）")
        elif ma_state < 0:
            short_score += 4
            notes.append("60分 EMA20<EMA60 空头排列（+4）")
        if macd_state > 0:
            long_score += 4
            notes.append("60分 MACD 金叉（+4）")
        elif macd_state < 0:
            short_score += 4
            notes.append("60分 MACD 死叉（+4）")
        daily_ema20 = ema(daily_closes, 20) if len(daily_closes) >= 30 else None
        daily_ema60 = ema(daily_closes, 60) if len(daily_closes) >= 30 else None
        daily_state = _ma_state(daily_ema20, daily_ema60, daily_closes[-1] if daily_closes else 0.0)
        if daily_state > 0 and ma_state > 0:
            long_score += 8
            notes.append("日线+60分 多头共振（+8）")
        elif daily_state < 0 and ma_state < 0:
            short_score += 8
            notes.append("日线+60分 空头共振（+8）")

    return min(long_score, 40.0), min(short_score, 40.0), notes


def _momentum_factor(m5: list[dict], h60: list[dict]) -> tuple[float, float, list[str]]:
    """动量因子（满分 25）。"""
    long_score, short_score = 0.0, 0.0
    notes: list[str] = []
    m5_closes = _closes(m5)
    m5_highs = _highs(m5)
    m5_lows = _lows(m5)

    if len(m5) >= 22:
        breakout_high = max(m5_highs[-21:-1])
        breakout_low = min(m5_lows[-21:-1])
        if m5_closes[-1] > breakout_high:
            long_score += 8
            notes.append(f"5分钟放量突破20根高点 {breakout_high:.2f}（+8）")
        elif m5_closes[-1] < breakout_low:
            short_score += 8
            notes.append(f"5分钟跌破20根低点 {breakout_low:.2f}（+8）")
        rsi14 = rsi(m5_closes, 14)
        if rsi14 is not None:
            if 50 <= rsi14 <= 70:
                long_score += 5
                notes.append(f"RSI(14)={rsi14:.1f} 多头区间（+5）")
            elif 30 <= rsi14 < 50:
                short_score += 5
                notes.append(f"RSI(14)={rsi14:.1f} 空头区间（+5）")
            elif rsi14 > 70:
                short_score += 3
                notes.append(f"RSI(14)={rsi14:.1f} 超买，追多风险（空+3）")
            elif rsi14 < 30:
                long_score += 3
                notes.append(f"RSI(14)={rsi14:.1f} 超卖，追空风险（多+3）")
        k_value, d_value, _ = kdj(m5_highs, m5_lows, m5_closes)
        if k_value is not None and d_value is not None:
            if k_value > d_value:
                long_score += 6
                notes.append("KDJ 金叉（+6）")
            elif k_value < d_value:
                short_score += 6
                notes.append("KDJ 死叉（+6）")

    if len(h60) >= 22:
        h60_highs, h60_lows, h60_closes = _highs(h60), _lows(h60), _closes(h60)
        if h60_closes[-1] > max(h60_highs[-21:-1]):
            long_score += 6
            notes.append("60分钟突破20根高点（+6）")
        elif h60_closes[-1] < min(h60_lows[-21:-1]):
            short_score += 6
            notes.append("60分钟跌破20根低点（+6）")

    return min(long_score, 25.0), min(short_score, 25.0), notes


def _volume_oi_factor(m5: list[dict], daily: list[dict], quote: dict) -> tuple[float, float, list[str]]:
    """量仓因子（满分 20）。"""
    long_score, short_score = 0.0, 0.0
    notes: list[str] = []
    volumes = _volumes(m5)
    if len(m5) >= 22 and m5[-1].get("volume", 0) > 0:
        average = sum(volumes[-21:-1]) / 20
        last_volume = volumes[-1]
        if last_volume >= average * 1.5:
            rising = m5[-1]["close"] >= m5[-1]["open"]
            if rising:
                long_score += 7
                notes.append("5分钟放量阳线确认（+7）")
            else:
                short_score += 7
                notes.append("5分钟放量阴线确认（+7）")
    last_price = float(quote.get("last") or 0)
    open_interest = float(quote.get("open_interest") or 0)
    if last_price > 0 and open_interest > 0:
        pre_oi = float(quote.get("pre_open_interest") or 0)
        oi_delta = open_interest - pre_oi if pre_oi > 0 else 0.0
        if oi_delta > 0:
            if quote.get("direction") in (None, ""):
                change = last_price - float(quote.get("pre_close") or last_price)
                rising = change >= 0
            else:
                rising = quote["direction"] == "buy"
            if rising:
                long_score += 6
                notes.append(f"持仓量增加 {oi_delta:.0f} 手，增仓上行（+6）")
            else:
                short_score += 6
                notes.append(f"持仓量增加 {oi_delta:.0f} 手，增仓下行（+6）")
        elif oi_delta < 0:
            if quote.get("direction") in (None, ""):
                change = last_price - float(quote.get("pre_close") or last_price)
                falling = change < 0
            else:
                falling = quote["direction"] == "sell"
            if falling:
                long_score += 4
                notes.append("持仓量减少但价格上行，空头离场（+4）")
            else:
                short_score += 4
                notes.append("持仓量减少但价格下行，多头离场（+4）")
    # 当日累计量能对比昨日（日线最后两根的成交量）
    daily_volumes = _volumes(daily)
    if len(daily_volumes) >= 3 and daily_volumes[-1] > 0 and daily_volumes[-2] > 0:
        if daily_volumes[-1] > daily_volumes[-2] * 1.3:
            rising = last_price >= float(quote.get("pre_close") or last_price)
            if rising:
                long_score += 7
                notes.append("当日成交显著放大且价格上行（+7）")
            else:
                short_score += 7
                notes.append("当日成交显著放大且价格下行（+7）")
    return min(long_score, 20.0), min(short_score, 20.0), notes


def _risk_factor(m5: list[dict], daily: list[dict], quote: dict) -> tuple[float, float, list[str]]:
    """风险因子（满分 15，多空共用）。"""
    notes: list[str] = []
    base = 10.0
    last = float(quote.get("last") or 0)
    if last > 0:
        m5_highs, m5_lows, m5_closes = _highs(m5), _lows(m5), _closes(m5)
        atr_value = atr(m5_highs, m5_lows, m5_closes)
        if atr_value is not None and atr_value > 0:
            atr_pct = atr_value / last * 100
            if atr_pct < 1.0:
                base += 3
                notes.append(f"ATR={atr_value:.2f}（{atr_pct:.2f}%）波动温和（+3）")
            elif atr_pct > 3.0:
                base -= 4
                notes.append(f"ATR={atr_value:.2f}（{atr_pct:.2f}%）波动过大，风险上调（-4）")
            else:
                base += 1
                notes.append(f"ATR={atr_value:.2f}（{atr_pct:.2f}%）波动正常（+1）")
        if len(m5_closes) >= 20:
            mid, upper, lower = boll(m5_closes)
            if mid is not None and upper is not None and lower is not None and upper > lower:
                position = (m5_closes[-1] - lower) / (upper - lower)
                if 0.25 <= position <= 0.75:
                    base += 2
                    notes.append("价格位于布林带中部，追价风险低（+2）")
    return max(0.0, min(15.0, base)), max(0.0, min(15.0, base)), notes


def evaluate(instrument: Instrument, quote: dict, klines: dict[int, list[dict]],
             risk: RiskConfig) -> dict:
    """综合评估一个合约。``klines`` 键为周期秒数，值为标准 K 线 dict 列表。"""
    # 清洗：剔除缺失关键价格的 K 线（部分免费行情字段可能缺失）。
    klines = {period: [bar for bar in bars if bar.get("close") is not None]
              for period, bars in klines.items()}
    daily = klines.get(PERIOD_DAILY) or []
    h60 = klines.get(PERIOD_H60) or []
    m5 = klines.get(PERIOD_M5) or []
    last = float(quote.get("last") or 0)

    if last <= 0 or len(m5) < 30 or len(h60) < 30 or len(daily) < 30:
        return {
            "pending": False,
            "direction": SIGNAL_FLAT, "direction_en": "FLAT",
            "score_long": 0, "score_short": 0, "score": 0,
            "entry": last if last > 0 else None,
            "stop": None, "target1": None, "target2": None, "target_points": None,
            "risk_amount": None, "risk_percent": None, "contracts": None,
            "multiplier": instrument.volume_multiple, "tick_size": instrument.price_tick,
            "rationale": ["历史K线或实时行情不足，暂不评估"], "data_ok": False,
        }

    trend_long, trend_short, trend_notes = _trend_factor(daily, h60)
    momentum_long, momentum_short, momentum_notes = _momentum_factor(m5, h60)
    volume_long, volume_short, volume_notes = _volume_oi_factor(m5, daily, quote)
    risk_long, risk_short, risk_notes = _risk_factor(m5, daily, quote)

    score_long = round(trend_long + momentum_long + volume_long + risk_long, 1)
    score_short = round(trend_short + momentum_short + volume_short + risk_short, 1)
    rationale = trend_notes + momentum_notes + volume_notes + risk_notes

    direction = SIGNAL_FLAT
    direction_en = "FLAT"
    if score_long >= MIN_SCORE and score_long - score_short >= MIN_GAP:
        direction, direction_en = SIGNAL_LONG, "LONG"
    elif score_short >= MIN_SCORE and score_short - score_long >= MIN_GAP:
        direction, direction_en = SIGNAL_SHORT, "SHORT"

    tick = float(instrument.price_tick or 1.0)
    multiplier = int(instrument.volume_multiple or 10)
    m5_highs, m5_lows, m5_closes = _highs(m5), _lows(m5), _closes(m5)
    atr_value = atr(m5_highs, m5_lows, m5_closes) or (tick * 2)

    entry = last
    stop = target1 = target2 = None
    contracts = None
    if direction != SIGNAL_FLAT:
        distance = max(atr_value * 1.5, tick * 2)
        if direction == SIGNAL_LONG:
            stop = _round_tick(entry - distance, tick, upward=False)
            distance_actual = entry - stop
            target1 = _round_tick(entry + distance_actual * 1.5, tick, upward=True)
            target2 = _round_tick(entry + distance_actual * 3.0, tick, upward=True)
        else:
            stop = _round_tick(entry + distance, tick, upward=True)
            distance_actual = stop - entry
            target1 = _round_tick(entry - distance_actual * 1.5, tick, upward=False)
            target2 = _round_tick(entry - distance_actual * 3.0, tick, upward=False)
        effective_risk = min(float(risk.max_loss_per_trade),
                             float(risk.account_equity) * float(risk.risk_percent) / 100.0)
        raw_count = int(effective_risk // max(distance_actual * multiplier, 1e-9))
        contracts = max(1, min(int(risk.max_contracts), raw_count))

    target_points = round(abs(target2 - entry), 2) if target2 is not None else None
    risk_amount = min(float(risk.max_loss_per_trade),
                      float(risk.account_equity) * float(risk.risk_percent) / 100.0) if contracts else None

    return {
        "pending": False,
        "direction": direction,
        "direction_en": direction_en,
        "score_long": score_long,
        "score_short": score_short,
        "score": max(score_long, score_short),
        "entry": entry,
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "target_points": target_points,
        "risk_amount": risk_amount,
        "risk_percent": round(risk_amount / risk.account_equity * 100, 2) if risk_amount else None,
        "contracts": contracts,
        "multiplier": multiplier,
        "tick_size": tick,
        "rationale": rationale,
        "data_ok": True,
    }

"""Holding-horizon profiles, input validation and time-aware data preparation.

All dates use Asia/Shanghai (UTC+8). Session data comes from the quote source;
it is not a holiday calendar. Missing metadata is reported rather than guessed.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from config import RiskConfig

CN = timezone(timedelta(hours=8))
WEEK = 604800
LABELS = {60: "1分钟", 300: "5分钟", 900: "15分钟", 3600: "60分钟",
          86400: "日线", WEEK: "周线"}
PROFILES = {
    "ultra": dict(label="超短线", holding="5–30 分钟，不隔夜", execution=60,
                  confirm=300, trend=900, max_days=1, stale_seconds=60),
    "short": dict(label="短线", holding="1–3 个交易日", execution=900,
                  confirm=3600, trend=86400, max_days=7, stale_seconds=180),
    "medium": dict(label="中线", holding="2–4 周", execution=3600,
                   confirm=86400, trend=WEEK, max_days=28, stale_seconds=300),
    "long": dict(label="长线", holding="1–3 个月", execution=86400,
                 confirm=86400, trend=WEEK, max_days=93, stale_seconds=300),
}
PROFILES = {key: dict(p, min_score=60, min_gap=15, stop_atr=1.5,
                      target1_r=1.5, target2_r=3.0) for key, p in PROFILES.items()}
RISK_LIMITS = {
    "account_equity": (0.01, 1e12), "max_loss_per_trade": (0.01, 1e12),
    "risk_percent": (0.0001, 100), "max_contracts": (1, 1000000),
}


def validate_risk(base: RiskConfig, values=None) -> RiskConfig:
    data = asdict(base)
    for key, bounds in RISK_LIMITS.items():
        raw = (values or {}).get(key)
        if raw is not None:
            data[key] = raw
        try:
            value = float(data[key])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("风险参数 %s 必须是有效数字" % key)
        if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
            raise ValueError("风险参数 %s 超出允许范围" % key)
        if key == "max_contracts":
            if value != int(value):
                raise ValueError("最大手数必须为整数")
            value = int(value)
        data[key] = value
    return RiskConfig(**data)


def parse_request(params, base):
    mode = params.get("mode") or "legacy"
    if mode != "legacy" and mode not in PROFILES:
        raise ValueError("不支持的评估模式")
    return mode, validate_risk(base, params)


def requests_for(mode):
    if mode == "legacy":
        return {86400: 200, 3600: 200, 900: 200, 300: 200}
    if mode not in PROFILES:
        raise ValueError("不支持的评估模式")
    p = PROFILES[mode]
    periods = {p[k] for k in ("execution", "confirm", "trend")}
    weekly = WEEK in periods
    periods.discard(WEEK)
    if weekly:
        periods.add(86400)
    return {period: 400 if weekly and period == 86400 else 200
            for period in sorted(periods)}


def profile_catalog(risk):
    return {"default_mode": "short", "profile_version": 1,
            "risk_defaults": asdict(validate_risk(risk)), "risk_limits": RISK_LIMITS,
            "profiles": [{"id": key, **p, "periods": list(dict.fromkeys(
                LABELS[p[k]] for k in ("execution", "confirm", "trend")))}
                for key, p in PROFILES.items()]}


def _date(ms):
    return datetime.fromtimestamp(float(ms) / 1000, CN)


def clean_bars(bars):
    output = {}
    for bar in bars:
        try:
            if not all(math.isfinite(float(bar[k])) for k in
                       ("datetime", "open", "high", "low", "close", "volume")):
                continue
            if bar["high"] < max(bar["open"], bar["close"], bar["low"]) or bar["low"] > min(bar["open"], bar["close"]):
                continue
            _date(bar["datetime"])
            output[bar["datetime"]] = bar
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
    return [output[key] for key in sorted(output)]


def completed_bars(bars, period, now_ms):
    now = _date(now_ms)
    # Daily candles are labelled by trading date. Drop today's daily candle
    # conservatively, even after the day close; never treat it as final early.
    if period == 86400:
        return [b for b in clean_bars(bars) if _date(b["datetime"]).date() < now.date()]
    return [b for b in clean_bars(bars) if b["datetime"] + period * 1000 <= now_ms]


def weekly_bars(daily, now_ms):
    buckets = {}
    current = _date(now_ms).isocalendar()[:2]
    for bar in daily:
        key = _date(bar["datetime"]).isocalendar()[:2]
        if key >= current:
            continue
        buckets.setdefault(key, []).append(bar)
    result = []
    # The first observed week may be truncated by the requested history window.
    for key in sorted(buckets)[1:]:
        bars = buckets[key]
        result.append(dict(datetime=bars[0]["datetime"], open=bars[0]["open"],
                           high=max(b["high"] for b in bars), low=min(b["low"] for b in bars),
                           close=bars[-1]["close"], volume=sum(b["volume"] for b in bars),
                           open_interest=bars[-1].get("open_interest")))
    return result


def _seconds(value):
    h, m, s = map(int, value.split(":"))
    if not (0 <= h <= 48 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError("invalid session")
    return h * 3600 + m * 60 + s


def session_info(trading_time, now_ms):
    """Match source-provided sessions, including 25:00/02:00 night closes.

    'scheduled' means the time falls in a regular session, not confirmation
    that an exchange is open on a holiday. Quote freshness is checked separately.
    """
    if not isinstance(trading_time, dict):
        return {"status": "unknown", "minutes_to_close": None}
    now = _date(now_ms)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    valid = False
    for kind in ("day", "night"):
        for pair in trading_time.get(kind, []) or []:
            try:
                start, end = map(_seconds, pair)
                if end <= start:
                    end += 86400
                valid = True
                for offset in (0, -1):
                    anchor = midnight + timedelta(days=offset)
                    if anchor.weekday() >= 5:
                        continue
                    a, b = anchor + timedelta(seconds=start), anchor + timedelta(seconds=end)
                    if a <= now < b:
                        return {"status": "scheduled", "minutes_to_close": round((b-now).total_seconds()/60, 2),
                                "close_time": b.isoformat()}
            except (TypeError, ValueError):
                continue
    return {"status": "outside" if valid else "unknown", "minutes_to_close": None}


def prepare(mode, klines, quote, instrument, now_ms):
    p = PROFILES[mode]
    bars = {period: completed_bars(klines.get(period, []), period, now_ms)
            for period in requests_for(mode)}
    if p["trend"] == WEEK:
        bars[WEEK] = weekly_bars(bars[86400], now_ms)
    required = {}
    for role, minimum in (("execution", 30), ("confirm", 60), ("trend", 60)):
        period = p[role]
        if period == WEEK:
            minimum = 35  # weekly EMA10/20 and MACD(12,26,9), never require 60 weeks
        required[period] = max(required.get(period, 0), minimum)
    missing = ["%s需%d根已完成K线，当前%d根" % (LABELS[k], n, len(bars.get(k, [])))
               for k, n in required.items() if len(bars.get(k, [])) < n]
    warnings = []
    timestamp = quote.get("timestamp")
    try:
        age = (now_ms - float(timestamp)) / 1000
        fresh = math.isfinite(age) and -5 <= age <= p["stale_seconds"]
    except (TypeError, ValueError, OverflowError):
        fresh = False
    session = session_info(quote.get("trading_time"), now_ms)
    if not fresh:
        warnings.append("行情时间缺失或已过期：仅展示历史技术方向，不提供当前开仓手数。")
    if session["status"] == "outside":
        warnings.append("当前不在行情源提供的常规交易时段内。")
    elif session["status"] == "unknown":
        warnings.append("未取得有效交易时段，无法确认收盘时间；请核对交易所安排。")
    if mode == "ultra":
        warnings.append("计划持有5–30分钟且不隔夜；系统仅作提示，不会自动平仓。")
        minutes = session["minutes_to_close"]
        if minutes is not None and minutes < 5:
            warnings.append("距本时段收盘不足5分钟，继续展示技术方向，但剩余时间不足最低计划持有期。")
        elif minutes is not None and minutes < 30:
            warnings.append("距本时段收盘不足30分钟，请缩短计划持有时间并在收盘前退出。")
    expiry = quote.get("expire_datetime") or getattr(instrument, "expire_datetime", None)
    remaining = None
    try:
        if expiry and math.isfinite(float(expiry)):
            remaining = max(0, math.floor((float(expiry) - now_ms/1000)/86400))
    except (TypeError, ValueError, OverflowError):
        pass
    if remaining is None:
        warnings.append("未取得可靠到期日期，无法核对计划持有期；交割及经纪商提前平仓要求需另行确认。")
    elif remaining <= p["max_days"]:
        warnings.append("距到期约%d个自然日，可能无法覆盖%s；请核对交割及经纪商提前平仓要求。" % (remaining, p["holding"]))
    meta = dict(mode=mode, mode_label=p["label"], holding=p["holding"], profile_version=1,
                periods=[LABELS[k] for k in required], atr_period=LABELS[p["execution"]],
                min_score=p["min_score"], min_gap=p["min_gap"], stop_atr=p["stop_atr"],
                target1_r=p["target1_r"], target2_r=p["target2_r"],
                evaluated_at=now_ms, quote_timestamp=timestamp,
                data_timestamp=min((bars[k][-1]["datetime"] for k in required if bars.get(k)), default=None),
                quote_fresh=fresh, session=session, expire_rest_days=remaining, warnings=warnings,
                data_requirements=[dict(period=LABELS[k], required=n, available=len(bars.get(k, []))) for k, n in required.items()])
    return p, bars, missing, meta

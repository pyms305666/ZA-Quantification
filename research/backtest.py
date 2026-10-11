"""Causal OHLC replay of the released evaluator with explicit execution rules.

Signals use completed bars, fills use the next execution candle's open. Minute
bars determine exits; ambiguous stop/target touches resolve to the stop first.
This is a modelling study, not an exchange matching engine or tick simulation.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import calendar
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import math
from statistics import mean, stdev

from config import RiskConfig
from market.decision_profiles import CN, PROFILES, clean_bars, requests_for, session_info
from market.evaluator import evaluate, _round_tick
from market.model import Instrument


@dataclass(frozen=True)
class Settings:
    initial_balance: float = 50000
    fee_per_side: float = 5
    slippage_ticks: int = 1
    risk_cash: float = 900
    risk_percent: float = 1.8
    max_contracts: int = 10
    margin_rate: float = 0.15
    target_r: float = 3

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid setting: " + key)
        if self.initial_balance <= 0 or not 0 < self.margin_rate <= 1 or self.target_r <= 0:
            raise ValueError("invalid balance/margin/target")
        if not 0 < self.risk_percent <= 100 or self.risk_cash <= 0:
            raise ValueError("invalid risk limits")
        if self.max_contracts < 1 or int(self.max_contracts) != self.max_contracts:
            raise ValueError("max_contracts must be a positive integer")
        if int(self.slippage_ticks) != self.slippage_ticks:
            raise ValueError("slippage_ticks must be an integer")


def local_time(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, CN)


def add_months(timestamp, months):
    current = local_time(timestamp)
    total = current.year * 12 + current.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    result = current.replace(year=year, month=month,
                             day=min(current.day, calendar.monthrange(year, month)[1]))
    return int(result.timestamp() * 1000)


class History:
    def __init__(self, payload):
        self.meta = payload["instrument"]
        self.bars = {int(p): clean_bars(rows) for p, rows in payload["bars"].items()}
        if not self.bars.get(60) or not self.bars.get(86400):
            raise ValueError("minute and daily history are required")
        self.times = {p: [b["datetime"] for b in rows] for p, rows in self.bars.items()}
        self.days = sorted({local_time(b["datetime"]).date() for b in self.bars[86400]})
        self.day_indices = {d: i for i, d in enumerate(self.days)}
        self.requested_start = date.fromisoformat(payload["requested_start"])
        self.requested_end = date.fromisoformat(payload["requested_end"])
        self.instrument = Instrument(symbol=self.meta["symbol"], exchange=self.meta["symbol"].split(".")[0],
            instrument_id=self.meta["symbol"].split(".")[-1], price_tick=self.meta["price_tick"],
            volume_multiple=self.meta["volume_multiple"], expire_datetime=self.meta.get("expire_datetime"))

    def trading_day(self, timestamp):
        current = local_time(timestamp)
        candidate = current.date() + timedelta(days=1) if current.hour >= 18 else current.date()
        i = bisect_left(self.days, candidate)
        return self.days[i] if i < len(self.days) else None

    def completed(self, mode, timestamp):
        result = {}
        for p, count in requests_for(mode).items():
            if p not in self.bars:
                raise ValueError("history is missing period " + str(p))
            if p == 86400:
                midnight = int(local_time(timestamp).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
                end = bisect_left(self.times[p], midnight)
            else:
                end = bisect_right(self.times[p], timestamp - p * 1000)
            result[p] = self.bars[p][max(0, end-count):end]
        return result

    def group(self, mode, timestamp):
        p = PROFILES[mode]["execution"]
        if p == 86400:
            return self.trading_day(timestamp)
        i = bisect_right(self.times[p], timestamp) - 1
        if i >= 0 and timestamp < self.times[p][i] + p*1000:
            return self.times[p][i]
        return None


def position_size(entry, stop, side, balance, tick, multiplier, settings):
    """Keep expected stop loss plus round-trip costs within the risk allowance."""
    if side * (entry - stop) <= 0 or balance <= 0:
        return 0, 0
    allowance = min(settings.risk_cash, balance*settings.risk_percent/100)
    expected_exit = stop - side*tick*settings.slippage_ticks
    per_lot = abs(entry-expected_exit)*multiplier + 2*settings.fee_per_side
    by_risk = int(allowance // per_lot)
    by_margin = int(balance // (abs(entry)*multiplier*settings.margin_rate + settings.fee_per_side))
    return max(0, min(settings.max_contracts, by_risk, by_margin)), allowance


def price_exit(position, bar, slip):
    """Return an executable reference price/reason and ambiguity flag."""
    side, stop, target = position["side"], position["stop"], position["target"]
    if side*(bar["open"]-stop) <= 0:
        return bar["open"]-side*slip, "gap_stop", False
    if side*(bar["open"]-target) >= 0:
        return target-side*slip, "gap_target", False
    stop_hit = bar["low"] <= stop if side == 1 else bar["high"] >= stop
    target_hit = bar["high"] >= target if side == 1 else bar["low"] <= target
    if stop_hit:
        return stop-side*slip, "stop", target_hit
    if target_hit:
        return target-side*slip, "target", False
    return None, None, False


def summarize(trades, curve, settings, maximum_drawdown):
    end_balance = curve[-1]["equity"] if curve else settings.initial_balance
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] < 0]
    values = [settings.initial_balance] + [point["equity"] for point in curve]
    daily_returns = [values[i]/values[i-1]-1 for i in range(1,len(values)) if values[i-1]>0]
    volatility = stdev(daily_returns) if len(daily_returns)>1 else 0
    return dict(ending_equity=round(end_balance,2), net_profit=round(end_balance-settings.initial_balance,2),
        return_percent=round((end_balance/settings.initial_balance-1)*100,4),
        max_drawdown_percent=round(maximum_drawdown*100,4), trades=len(trades),
        win_rate_percent=round(len(wins)/len(trades)*100,4) if trades else None,
        profit_factor=round(sum(wins)/abs(sum(losses)),4) if losses else None,
        total_fees=round(sum(t["fees"] for t in trades),2),
        modelled_slippage_cost=round(sum(t["slippage_cost"] for t in trades),2),
        average_net_trade=round(sum(t["net_pnl"] for t in trades)/len(trades),2) if trades else None,
        daily_sharpe=round(mean(daily_returns)/volatility*math.sqrt(252),4) if volatility else None,
        ambiguous_bars=sum(t["ambiguous"] for t in trades),
        stop_losses_over_budget=sum(-t["net_pnl"]>t["risk_budget"]+1e-6 for t in trades if t["reason"] in ("stop","gap_stop")))


def simulate(history, mode, settings=Settings(), start_date=None, signal_fn=evaluate, progress=None):
    if mode not in PROFILES:
        raise ValueError("unknown horizon")
    start_date = start_date or history.requested_start
    rows = [b for b in history.bars[60] if (d:=history.trading_day(b["datetime"])) is not None
            and start_date <= d <= history.requested_end]
    if not rows:
        raise ValueError("no minute data in the requested interval")
    keys = [history.group(mode,b["datetime"]) for b in rows]
    days = [history.trading_day(b["datetime"]) for b in rows]
    sessions = history.meta.get("trading_time",{})
    tick, multiplier = history.instrument.price_tick, history.instrument.volume_multiple
    slip = tick * settings.slippage_ticks
    balance = settings.initial_balance
    peak = balance
    drawdown = 0
    position = pending = None
    trades, curve = [], []
    skips = dict(warmup=0, zero_volume=0, risk_or_margin=0, invalid_gap=0, short_session=0, no_group=0)
    first_eligible = None
    signals = 0

    def close(price, timestamp, reason, ambiguous=False):
        nonlocal balance, position
        lots = position["lots"]
        pnl = (price-position["entry"])*position["side"]*lots*multiplier
        fee = settings.fee_per_side*lots
        balance += pnl-fee
        trades.append(dict(mode=mode, entry_time=position["entry_time"], exit_time=timestamp,
            side="long" if position["side"]==1 else "short", entry=position["entry"], exit=price,
            lots=lots, stop=position["stop"], target=position["target"], signal_time=position["signal_time"],
            score=position["score"], risk_budget=position["risk_budget"], reason=reason, ambiguous=ambiguous,
            gross_pnl=round(pnl,8), fees=round(2*fee,8), net_pnl=round(pnl-2*fee,8),
            slippage_cost=2*slip*lots*multiplier, holding_minutes=round((timestamp-position["entry_time"])/60000,4)))
        position = None

    for i, bar in enumerate(rows):
        if progress and i % 5000 == 0:
            progress(mode, i, len(rows))
        timestamp, day, key = bar["datetime"], days[i], keys[i]
        end_time = timestamp+60000
        day_end = i == len(rows)-1 or day != days[i+1]
        group_end = i == len(rows)-1 or key != keys[i+1]
        if position and position.get("forced_exit"):
            close(bar["open"]-position["side"]*slip,timestamp,position["forced_exit"])
        if position and mode in ("medium","long") and timestamp >= position["deadline"]:
            close(bar["open"]-position["side"]*slip,timestamp,"time_limit")
        if pending and key != pending["group"] and position is None:
            order, pending = pending, None
            if bar["volume"] <= 0:
                skips["zero_volume"] += 1
            elif history.instrument.expire_datetime and timestamp/1000 >= history.instrument.expire_datetime-86400:
                skips["invalid_gap"] += 1
            else:
                session = session_info(sessions,timestamp)
                if mode == "ultra" and (session["status"] != "scheduled" or session["minutes_to_close"] < 5):
                    skips["short_session"] += 1
                else:
                    side = order["side"]
                    entry = bar["open"] + side*slip
                    stop = order["stop"]
                    if side*(entry-stop) <= 0:
                        skips["invalid_gap"] += 1
                    else:
                        lots, allowance = position_size(entry,stop,side,balance,tick,multiplier,settings)
                        if lots == 0:
                            skips["risk_or_margin"] += 1
                        else:
                            distance = abs(entry-stop)
                            target = _round_tick(entry+side*distance*settings.target_r,tick,upward=side==1)
                            deadline = add_months(timestamp,3) if mode=="long" else timestamp+28*86400000
                            position = dict(side=side,entry=entry,stop=stop,target=target,lots=lots,
                                entry_time=timestamp,signal_time=order["signal_time"],entry_day=day,
                                deadline=deadline,score=order["score"],risk_budget=allowance)
                            balance -= lots*settings.fee_per_side
        if position:
            price, reason, ambiguous = price_exit(position,bar,slip)
            if price is not None:
                close(price,timestamp if reason.startswith("gap_") else end_time,reason,ambiguous)
        if position:
            close_session = session_info(sessions,timestamp).get("minutes_to_close")
            ultra_due = mode=="ultra" and (end_time-position["entry_time"]>=30*60000 or
                                           (close_session is not None and close_session<=1))
            short_due = mode=="short" and day_end and history.day_indices[day]-history.day_indices[position["entry_day"]]>=2
            if ultra_due or short_due or i==len(rows)-1:
                close(bar["close"]-position["side"]*slip,end_time,
                      "end_of_sample" if i==len(rows)-1 else "session_or_time_limit" if ultra_due else "time_limit")
        equity = balance if position is None else balance+(bar["close"]-position["entry"])*position["side"]*position["lots"]*multiplier
        peak = max(peak,equity)
        drawdown = max(drawdown,(peak-equity)/peak)
        if position and equity < abs(bar["close"])*multiplier*position["lots"]*settings.margin_rate:
            position["forced_exit"]="margin_call"
        if day_end:
            curve.append(dict(trading_day=day.isoformat(),timestamp=end_time,equity=round(equity,8)))
        if group_end and key is not None and position is None and balance>0 and i<len(rows)-1:
            quote=dict(last=bar["close"],timestamp=end_time,expire_datetime=history.instrument.expire_datetime,
                       trading_time=sessions)
            risk=RiskConfig(account_equity=balance,max_loss_per_trade=settings.risk_cash,
                            risk_percent=settings.risk_percent,max_contracts=settings.max_contracts)
            result=signal_fn(history.instrument,quote,history.completed(mode,end_time),risk,mode,end_time)
            if not result["data_ok"]:
                skips["warmup"] += 1
            else:
                if first_eligible is None:
                    first_eligible=end_time
                if result.get("direction_en") in ("LONG","SHORT") and result.get("stop") is not None:
                    signals += 1
                    pending=dict(side=1 if result["direction_en"]=="LONG" else -1,stop=result["stop"],
                                 score=result["score"],signal_time=end_time,group=key)
        elif group_end and key is None:
            skips["no_group"] += 1
    assert position is None
    assert abs(balance-settings.initial_balance-sum(t["net_pnl"] for t in trades))<0.001
    return dict(mode=mode,label=PROFILES[mode]["label"],settings=asdict(settings),
        requested_start=start_date.isoformat(),requested_end=history.requested_end.isoformat(),
        observed_start=rows[0]["datetime"],observed_end=rows[-1]["datetime"]+60000,
        first_eligible=first_eligible,signals=signals,skips=skips,
        stats=summarize(trades,curve,settings,drawdown),trades=trades,equity=curve)

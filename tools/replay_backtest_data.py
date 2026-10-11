"""Capture the permitted TqSdk backtest replay for a reproducible local study.

Requires the optional TqSdk package and an existing account with backtest access.
Uses TqSim exclusively, never a broker account, and submits no orders.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import gzip
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def capture(symbol, start, end, output):
    from config import load_config
    from tqsdk import TqApi, TqAuth, TqBacktest, TqSim, BacktestFinished

    credentials = load_config().tqsdk
    if not credentials.complete:
        raise ValueError("请先在项目配置中设置行情账户")
    api = TqApi(TqSim(init_balance=50000),
                auth=TqAuth(credentials.account, credentials.password),
                backtest=TqBacktest(start_dt=start, end_dt=end),
                debug=False, disable_print=True, web_gui=False)
    periods = (60, 300, 900, 3600, 86400)
    records = {p: {} for p in periods}
    metadata = {}
    updates = 0
    started = last_progress = time.monotonic()

    def harvest(period, frame, full=False):
        rows = frame.to_dict("records") if full else frame.tail(3).to_dict("records")
        for row in rows:
            if not all(math.isfinite(float(row[k])) for k in ("datetime", "open", "high", "low", "close", "volume")):
                continue
            timestamp = int(row["datetime"]) // 1000000
            if timestamp <= 0:
                continue
            oi = row.get("close_oi")
            records[period][timestamp] = dict(datetime=timestamp,
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), volume=float(row["volume"]),
                open_interest=float(oi) if oi is not None and math.isfinite(float(oi)) else None)

    try:
        quote = api.get_quote(symbol)
        frames = {p: api.get_kline_serial(symbol, p, data_length=400 if p == 86400 else 200) for p in periods}
        for p, frame in frames.items():
            harvest(p, frame, True)
        while True:
            if not api.wait_update(deadline=time.time() + 60):
                raise TimeoutError("历史回放连续60秒没有响应")
            updates += 1
            for p, frame in frames.items():
                if api.is_changing(frame):
                    harvest(p, frame)
            if time.monotonic() - last_progress >= 20:
                print("replay", quote.datetime, "minute bars", len(records[60]),
                      "elapsed", round(time.monotonic()-started), "seconds", flush=True)
                last_progress = time.monotonic()
    except BacktestFinished:
        for p, frame in frames.items():
            harvest(p, frame, True)
        metadata = {"symbol": symbol, "price_tick": float(quote.price_tick),
                    "volume_multiple": int(quote.volume_multiple),
                    "expire_datetime": float(quote.expire_datetime),
                    "trading_time": {key: [list(pair) for pair in getattr(quote.trading_time, key)] for key in ("day", "night")}}
    finally:
        api.close()
    payload = dict(source="TqSdk TqBacktest historical replay", sdk_version=version("tqsdk"),
                   requested_start=str(start), requested_end=str(end),
                   captured_at=datetime.now(timezone.utc).isoformat(), instrument=metadata,
                   bars={str(p): [records[p][t] for t in sorted(records[p])] for p in periods})
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
    print("saved replay:", output, {p: len(records[p]) for p in periods}, "updates", updates, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SHFE.rb2610")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 9, 26))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 24))
    parser.add_argument("--output", type=Path, default=ROOT / ".tqsdk/backtests/rb2610-20250926-20260924.json.gz")
    args = parser.parse_args()
    capture(args.symbol, args.start, args.end, args.output)

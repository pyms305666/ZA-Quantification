import unittest
from datetime import datetime, date

from market.decision_profiles import CN
from research.backtest import History, Settings, add_months, local_time, position_size, price_exit, simulate


def ms(day, hour=9, minute=0):
    return int(datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=CN).timestamp()*1000)


def candle(timestamp, price=100, volume=100):
    return dict(datetime=timestamp, open=price, high=price+1, low=price-1,
                close=price, volume=volume, open_interest=100)


def history(days=("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24")):
    bars = {p: [] for p in (60,300,900,3600,86400)}
    for day in days:
        for minute in range(30):
            bars[60].append(candle(ms(day,9,minute)))
        for period in (300,900,3600):
            for minute in range(0,30,period//60):
                bars[period].append(candle(ms(day,9,minute)))
        bars[86400].append(candle(ms(day,0)))
    return History(dict(instrument=dict(symbol="SHFE.rb2610",price_tick=1,volume_multiple=10,
        trading_time=dict(day=[["09:00:00","15:00:00"]],night=[["21:00:00","23:00:00"]])),
        requested_start=days[0],requested_end=days[-1],bars=bars))


def once():
    calls = []
    def signal(instrument, quote, bars, risk, mode, now):
        calls.append(now)
        return dict(data_ok=True,direction_en="LONG" if len(calls)==1 else "FLAT",stop=98,score=70)
    return signal, calls


class BacktestTests(unittest.TestCase):
    def test_completed_bars_exclude_future_and_unfinished(self):
        h=history()
        rows=h.completed("ultra",ms("2026-09-21",9,14))
        self.assertEqual(len(rows[60]),14)
        self.assertEqual(len(rows[300]),2)
        self.assertEqual(rows[900],[])
        self.assertEqual(h.completed("long",ms("2026-09-22",15))[86400][-1]["datetime"],ms("2026-09-21",0))

    def test_stop_first_when_both_levels_touched(self):
        p=dict(side=1,stop=98,target=106)
        self.assertEqual(price_exit(p,dict(open=100,low=97,high=107),1),(97,"stop",True))
        p=dict(side=-1,stop=102,target=94)
        self.assertEqual(price_exit(p,dict(open=100,low=93,high=103),1),(103,"stop",True))

    def test_gap_stop_and_conservative_gap_target(self):
        p=dict(side=1,stop=98,target=106)
        self.assertEqual(price_exit(p,dict(open=95,low=94,high=97),1),(94,"gap_stop",False))
        self.assertEqual(price_exit(p,dict(open=110,low=109,high=111),1),(105,"gap_target",False))

    def test_size_includes_round_trip_costs(self):
        lots,budget=position_size(101,98,1,50000,1,10,Settings())
        self.assertEqual((lots,budget),(10,900))
        self.assertEqual(position_size(101,98,1,50000,1,10,Settings(risk_cash=49))[0],0)
        self.assertEqual(position_size(3001,2998,1,1000,1,10,Settings(risk_percent=100))[0],0)

    def test_next_minute_entry_fees_and_reconciliation(self):
        fn,_=once()
        r=simulate(history(),"ultra",Settings(max_contracts=1),signal_fn=fn)
        t=r["trades"][0]
        self.assertEqual(t["signal_time"],ms("2026-09-21",9,1))
        self.assertEqual(t["entry_time"],ms("2026-09-21",9,1))
        self.assertEqual(t["entry"],101)
        self.assertEqual(t["fees"],10)
        self.assertEqual(t["net_pnl"],-30)
        self.assertEqual(r["stats"]["ending_equity"],49970)

    def test_next_execution_candle_entry(self):
        fn,_=once()
        r=simulate(history(),"short",Settings(max_contracts=1),signal_fn=fn)
        self.assertEqual(r["trades"][0]["entry_time"],ms("2026-09-21",9,15))

    def test_short_limit_counts_trading_days_across_weekend(self):
        fn,_=once()
        r=simulate(history(("2026-09-24","2026-09-25","2026-09-28","2026-09-29")),
                   "short",Settings(max_contracts=1),signal_fn=fn)
        self.assertEqual(r["trades"][0]["exit_time"],ms("2026-09-28",9,30))
        self.assertEqual(r["trades"][0]["reason"],"time_limit")

    def test_ultra_closes_before_next_session(self):
        h=history()
        h.meta["trading_time"]["day"]=[["09:00:00","09:10:00"]]
        fn,_=once()
        r=simulate(h,"ultra",Settings(max_contracts=1),signal_fn=fn)
        self.assertEqual(r["trades"][0]["exit_time"],ms("2026-09-21",9,10))

    def test_month_end_deadline(self):
        self.assertEqual(local_time(add_months(ms("2026-11-30"),3)).date(),date(2027,2,28))

    def test_future_price_change_does_not_change_earlier_trade(self):
        original=history()
        changed=history()
        boundary=ms("2026-09-24",0)
        for period,rows in changed.bars.items():
            for row in rows:
                if row["datetime"]>=boundary:
                    for field in ("open","high","low","close"):
                        row[field]+=1000
        a,_=once(); b,_=once()
        self.assertEqual(simulate(original,"ultra",signal_fn=a)["trades"],
                         simulate(changed,"ultra",signal_fn=b)["trades"])

    def test_invalid_settings_rejected(self):
        for kwargs in (dict(margin_rate=0),dict(slippage_ticks=1.5),dict(risk_cash=float("nan"))):
            with self.assertRaises(ValueError): Settings(**kwargs)


if __name__=="__main__":
    unittest.main()

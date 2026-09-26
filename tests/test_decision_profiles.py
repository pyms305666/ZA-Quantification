"""Behavior tests for horizon selection and risk limits; synthetic, not backtests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from config import RiskConfig
from market.decision_profiles import (CN, PROFILES, completed_bars, parse_request,
                                     prepare, requests_for, session_info, weekly_bars)
from market.evaluator import evaluate, _volume_oi_factor
from market.model import Instrument
from market.processor import to_market_quote
from tests.test_decision import make_bars, trend_up, trend_down, quote_for


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=CN).timestamp() * 1000)


NOW = ms("2026-09-28T14:58:00")
INSTRUMENT = Instrument("SHFE.rb2705", "SHFE", "rb2705", price_tick=1,
                        volume_multiple=10, expire_datetime=NOW/1000 + 200*86400)
SESSIONS = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"],
                    ["13:30:00", "15:00:00"]], "night": [["21:00:00", "25:00:00"]]}


def history():
    data = {}
    for period in (60, 300, 900, 3600):
        bars = make_bars(trend_up(150, step=period/300))
        for i, bar in enumerate(bars):
            bar["datetime"] = NOW - (151-i)*period*1000
            bar["open_interest"] = 100000 + i*100
        data[period] = bars
    dates = []
    cursor = datetime.fromtimestamp(NOW/1000, CN).replace(hour=0, minute=0)
    while len(dates) < 400:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            dates.append(int(cursor.timestamp()*1000))
    daily = make_bars(trend_up(400, step=2))
    for date, bar in zip(reversed(dates), daily):
        bar["datetime"] = date
    data[86400] = daily
    return data


def fresh_quote():
    return dict(quote_for(3300), timestamp=NOW, trading_time=SESSIONS,
                expire_datetime=INSTRUMENT.expire_datetime)


class ProfileTests(unittest.TestCase):
    def test_each_mode_selects_distinct_inputs_and_labels(self):
        data = history()
        results = {mode: evaluate(INSTRUMENT, fresh_quote(), data, RiskConfig(), mode, NOW)
                   for mode in PROFILES}
        for mode, result in results.items():
            with self.subTest(mode=mode):
                self.assertTrue(result["data_ok"], result["rationale"])
                self.assertEqual(result["mode"], mode)
                self.assertEqual(result["holding"], PROFILES[mode]["holding"])
        self.assertEqual(results["ultra"]["periods"], ["1分钟", "5分钟", "15分钟"])
        self.assertEqual(results["long"]["atr_period"], "日线")
        self.assertNotIn("日线", " ".join(results["ultra"]["rationale"]))
        # Give ultra an opposite trend; short retains daily/hourly trend evidence.
        for period in (60, 300, 900):
            for old, replacement in zip(data[period], make_bars(trend_down(150))):
                replacement["datetime"] = old["datetime"]
                old.update(replacement)
        ultra = evaluate(INSTRUMENT, fresh_quote(), data, RiskConfig(), "ultra", NOW)
        short = evaluate(INSTRUMENT, fresh_quote(), data, RiskConfig(), "short", NOW)
        self.assertNotEqual(ultra["score_long"], short["score_long"])
        self.assertGreater(ultra["score_short"], ultra["score_long"])

    def test_weekly_history_shortage_is_explicit(self):
        data = history()
        data[86400] = data[86400][-100:]
        result = evaluate(INSTRUMENT, fresh_quote(), data, RiskConfig(), "long", NOW)
        self.assertFalse(result["data_ok"])
        self.assertIn("周线需35根", " ".join(result["rationale"]))
        self.assertIsNone(result["contracts"])

    def test_unfinished_and_current_week_bars_excluded(self):
        bars = make_bars([100, 200, 99999])
        for bar, stamp in zip(bars, [NOW-120000, NOW-60000, NOW-30000]):
            bar["datetime"] = stamp
        self.assertEqual([b["close"] for b in completed_bars(bars, 60, NOW)], [100, 200])
        daily = history()[86400]
        before = weekly_bars(daily, NOW)
        daily += [dict(daily[-1], datetime=NOW, close=999999, high=999999)]
        self.assertEqual(weekly_bars(daily, NOW), before)
        first_week = datetime.fromtimestamp(daily[0]["datetime"]/1000, CN).isocalendar()[:2]
        self.assertNotEqual(datetime.fromtimestamp(before[0]["datetime"]/1000, CN).isocalendar()[:2], first_week)

    def test_close_warning_does_not_override_direction(self):
        quote = fresh_quote()
        near = evaluate(INSTRUMENT, quote, history(), RiskConfig(), "ultra", NOW)
        no_schedule = evaluate(INSTRUMENT, dict(quote, trading_time={}), history(), RiskConfig(), "ultra", NOW)
        self.assertEqual(near["direction"], no_schedule["direction"])
        self.assertIn("不足5分钟", " ".join(near["warnings"]))

    def test_expiry_uses_absolute_time_not_cached_days(self):
        quote = dict(fresh_quote(), expire_datetime=NOW/1000+10*86400)
        result = evaluate(INSTRUMENT, quote, history(), RiskConfig(), "long", NOW)
        self.assertEqual(result["expire_rest_days"], 10)
        self.assertIn("可能无法覆盖", " ".join(result["warnings"]))

    def test_old_quote_has_no_current_position_size(self):
        result = evaluate(INSTRUMENT, dict(fresh_quote(), timestamp=NOW-3600000),
                          history(), RiskConfig(), "ultra", NOW)
        self.assertFalse(result["quote_fresh"])
        self.assertIsNone(result["contracts"])
        self.assertIn("已过期", " ".join(result["warnings"]))

    def test_expired_contract_has_no_current_position_size(self):
        quote = dict(fresh_quote(), expire_datetime=NOW/1000-86400)
        result = evaluate(INSTRUMENT, quote, history(), RiskConfig(), "ultra", NOW)
        self.assertIsNone(result["contracts"])
        self.assertIn("合约已到期", " ".join(result["warnings"]))

    def test_per_mode_request_only_fetches_needed_periods(self):
        self.assertEqual(set(requests_for("ultra")), {60, 300, 900})
        self.assertEqual(requests_for("long"), {86400: 400})
        self.assertEqual(parse_request({}, RiskConfig())[0], "legacy")

    def test_invalid_settings_rejected_including_nonfinite(self):
        for raw in ({"mode":"unknown"}, {"risk_percent":"NaN"}, {"account_equity":0},
                    {"max_loss_per_trade":"Infinity"}, {"max_contracts":1.5}, {"risk_percent":101}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_request(raw, RiskConfig())


class RiskTests(unittest.TestCase):
    def legacy(self, risk, instrument=INSTRUMENT):
        bars = {86400:make_bars(trend_up(120)), 3600:make_bars(trend_up(90)),
                300:make_bars(trend_up(60))}
        return evaluate(instrument, quote_for(bars[300][-1]["close"]), bars, risk)

    def test_one_lot_over_budget_gives_zero_not_one(self):
        result = self.legacy(RiskConfig(max_loss_per_trade=0.01))
        self.assertEqual(result["direction"], "做多")
        self.assertEqual(result["contracts"], 0)
        self.assertEqual(result["risk_amount"], 0)
        self.assertEqual(result["risk_percent"], 0)

    def test_tighter_cap_and_real_cost(self):
        for risk, cap in [(RiskConfig(max_loss_per_trade=100),100),
                          (RiskConfig(account_equity=10000,risk_percent=0.5),50)]:
            result = self.legacy(risk)
            self.assertEqual(result["risk_budget"], cap)
            self.assertLessEqual(result["risk_amount"], cap)
            self.assertAlmostEqual(result["risk_amount"], result["contracts"]*result["one_lot_risk"])

    def test_missing_multiplier_never_assumed(self):
        result = self.legacy(RiskConfig(), Instrument("SHFE.rb2705","SHFE","rb2705"))
        self.assertIsNone(result["contracts"])
        self.assertIsNone(result["stop"])

    def test_decreasing_oi_labels_follow_price_direction(self):
        for last, expected in [(101,"价格上行"),(99,"价格下行")]:
            _, _, notes = _volume_oi_factor([], [], {"last":last,"pre_close":100,
                "open_interest":90,"pre_open_interest":100})
            self.assertIn(expected, " ".join(notes))


class SessionAndQuoteTests(unittest.TestCase):
    def test_night_beyond_24_and_midnight_format(self):
        for end in ("25:00:00", "01:00:00"):
            result = session_info({"night":[["21:00:00",end]]}, ms("2026-09-26T00:58:00"))
            self.assertEqual(result["status"], "scheduled")
            self.assertEqual(result["minutes_to_close"], 2)
        self.assertEqual(session_info(SESSIONS, ms("2026-09-26T14:58:00"))["status"], "outside")
        self.assertEqual(session_info({}, NOW)["status"], "unknown")

    def test_processor_preserves_timing_and_oi(self):
        quote = to_market_quote("SHFE.rb2705", SimpleNamespace(last_price=3300,
            datetime="2026-09-28 14:58:00", pre_open_interest=123,
            trading_time=SESSIONS, expire_datetime=INSTRUMENT.expire_datetime))
        self.assertEqual(quote.timestamp, NOW)
        self.assertEqual(quote.to_dict()["pre_open_interest"], 123)
        self.assertEqual(quote.to_dict()["trading_time"], SESSIONS)


if __name__ == "__main__":
    unittest.main()

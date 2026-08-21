"""指标库与决策引擎测试（合成数据，不依赖网络）。"""

from __future__ import annotations

import math
import unittest

from config import RiskConfig
from market.evaluator import evaluate
from market.indicators import atr, boll, ema, kdj, macd, rsi, sma
from market.model import Instrument

RB2610 = Instrument("SHFE.rb2610", "SHFE", "rb2610", "螺纹钢2610", "FUTURE", False, 1.0, 10)
RISK = RiskConfig(account_equity=50_000, max_loss_per_trade=900, risk_percent=1.8, max_contracts=10)


def make_bars(closes: list[float], volume: float = 100_000.0, open_interest: float = 0.0) -> list[dict]:
    bars = []
    for index, close in enumerate(closes):
        open_ = closes[index - 1] if index > 0 else close - 1
        bars.append({
            "datetime": 1_700_000_000_000 + index * 300_000,
            "open": open_, "high": max(open_, close) + 0.5, "low": min(open_, close) - 0.5,
            "close": close, "volume": volume, "open_interest": open_interest,
        })
    return bars


def _wiggle(index: int) -> float:
    """震荡序列的确定性小幅扰动。"""
    return (index % 5 - 2) * 0.3


def trend_up(length: int, start: float = 3000.0, step: float = 2.0) -> list[float]:
    """上行趋势：线性漂移 + 正弦波动，末尾 4 根加速上行冲刺（DIF 持续上升）。"""
    values = [start + index * step + 3.0 * math.sin(index * 0.7) for index in range(length)]
    base = values[-5]
    values[-4:] = [base + 1.0, base + 3.0, base + 6.0, base + 10.0]
    return values


def trend_down(length: int, start: float = 3200.0, step: float = 2.0) -> list[float]:
    values = [start - index * step + 3.0 * math.sin(index * 0.7) for index in range(length)]
    base = values[-5]
    values[-4:] = [base - 1.0, base - 3.0, base - 6.0, base - 10.0]
    return values


def flat(length: int, value: float = 3000.0) -> list[float]:
    return [value + (index % 3 - 1) * 0.5 + _wiggle(index) for index in range(length)]


def quote_for(last: float, volume: int = 100_000, oi: int = 1_000_000,
             pre_oi: int = 990_000, pre_close: float | None = None) -> dict:
    return {
        "symbol": "SHFE.rb2610", "exchange": "SHFE", "instrument_id": "rb2610",
        "timestamp": 1_700_000_000_000, "last": last, "open": last - 5, "high": last + 8,
        "low": last - 8, "pre_close": pre_close or last - 10, "volume": volume,
        "open_interest": oi, "bid": [], "ask": [],
    }


class IndicatorTests(unittest.TestCase):
    def test_ema_of_constant_is_constant(self):
        values = [100.0] * 30
        self.assertAlmostEqual(ema(values, 20), 100.0, places=6)

    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 3), 3.0)
        self.assertIsNone(sma([1, 2], 3))

    def test_macd_rising_is_positive(self):
        dif, dea, hist = macd(trend_up(80))
        self.assertIsNotNone(dif)
        self.assertGreater(dif, 0)  # type: ignore[operator]
        self.assertIsNotNone(hist)

    def test_macd_insufficient(self):
        self.assertEqual(macd([1, 2, 3]), (None, None, None))

    def test_rsi_rising_approaches_100(self):
        self.assertGreater(rsi(trend_up(40)), 80)  # type: ignore[operator]

    def test_kdj_rising_cross(self):
        # 小幅回落后连续两根创新高：RSV 从低位回到 100，K 上穿 D。
        closes = [float(index) for index in range(21)] + [19.5, 19.8, 22.0, 25.0]
        highs = [value + 1 for value in closes]
        lows = [value - 1 for value in closes]
        k, d, j = kdj(highs, lows, closes)
        self.assertIsNotNone(k)
        self.assertGreater(k, d)  # type: ignore[operator]

    def test_boll_mid_equals_sma(self):
        values = trend_up(30)
        mid, upper, lower = boll(values)
        self.assertAlmostEqual(mid, sma(values, 20), places=6)  # type: ignore[arg-type]
        self.assertGreater(upper, mid)  # type: ignore[operator]
        self.assertLess(lower, mid)  # type: ignore[operator]

    def test_atr_of_steady_moves(self):
        highs = [v + 2 for v in trend_up(30)]
        lows = [v - 2 for v in trend_up(30)]
        value = atr(highs, lows, trend_up(30))
        self.assertIsNotNone(value)
        self.assertGreater(value, 3.0)  # 至少覆盖 高-低=4 的一半以上


class EvaluatorTests(unittest.TestCase):
    def _klines(self, daily: list[float], h60: list[float], m5: list[float]) -> dict:
        return {86400: make_bars(daily), 3600: make_bars(h60), 900: make_bars(m5), 300: make_bars(m5)}

    def _last_close(self, klines: dict) -> float:
        return float(klines[300][-1]["close"])

    def test_uptrend_gives_long_signal(self):
        klines = self._klines(trend_up(120), trend_up(90), trend_up(60))
        result = evaluate(RB2610, quote_for(self._last_close(klines)), klines, RISK)
        self.assertTrue(result["data_ok"])
        self.assertEqual(result["direction"], "做多")
        self.assertGreaterEqual(result["score_long"], 60)
        self.assertGreaterEqual(result["score_long"] - result["score_short"], 15)
        self.assertIsNotNone(result["stop"])
        self.assertLess(result["stop"], result["entry"])  # type: ignore[operator]
        self.assertGreater(result["target2"], result["target1"])  # type: ignore[operator]
        self.assertGreater(result["target_points"], 0)  # type: ignore[operator]
        self.assertEqual(result["risk_amount"], 900)
        self.assertGreaterEqual(result["contracts"], 1)
        self.assertTrue(result["rationale"])

    def test_downtrend_gives_short_signal(self):
        klines = self._klines(trend_down(120), trend_down(90), trend_down(60))
        last = self._last_close(klines)
        result = evaluate(RB2610, quote_for(last, pre_close=last + 12), klines, RISK)
        self.assertEqual(result["direction"], "做空")
        self.assertGreater(result["stop"], result["entry"])  # type: ignore[operator]
        self.assertLess(result["target2"], result["entry"])  # type: ignore[operator]

    def test_flat_market_gives_watch(self):
        klines = self._klines(flat(120), flat(90), flat(60))
        result = evaluate(RB2610, quote_for(self._last_close(klines), pre_close=self._last_close(klines) - 2), klines, RISK)
        self.assertEqual(result["direction"], "观望")

    def test_insufficient_data(self):
        result = evaluate(RB2610, quote_for(3000), {86400: make_bars(flat(10)), 3600: [], 900: [], 300: []}, RISK)
        self.assertFalse(result["data_ok"])
        self.assertEqual(result["direction"], "观望")
        self.assertIn("不足", result["rationale"][0])

    def test_contracts_capped(self):
        small_tick = Instrument("SHFE.au2612", "SHFE", "au2612", "黄金2612", "FUTURE", False, 0.02, 1000)
        klines = self._klines(trend_up(120), trend_up(90), trend_up(60))
        result = evaluate(small_tick, quote_for(self._last_close(klines)), klines, RISK)
        self.assertEqual(result["direction"], "做多")
        self.assertLessEqual(result["contracts"], RISK.max_contracts)  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()

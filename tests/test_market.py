"""market 层测试：标准行情模型与归一化（不依赖 tqsdk / 网络）。"""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from market.cache import QuoteCache
from market.model import MarketQuote, QuoteLevel, split_symbol
from market.processor import to_market_quote


def make_tq_quote(**overrides):
    base = {
        "datetime": 1755770165123,
        "last_price": 3286.0, "open": 3270.0, "high": 3301.0, "low": 3265.0,
        "pre_close": 3280.0, "volume": 152340, "open_interest": 1839201,
        "bid_price1": 3285.0, "bid_volume1": 125,
        "bid_price2": 3284.0, "bid_volume2": 210,
        "bid_price3": 3283.0, "bid_volume3": 312,
        "bid_price4": 3282.0, "bid_volume4": 198,
        "bid_price5": 3281.0, "bid_volume5": 275,
        "ask_price1": 3286.0, "ask_volume1": 83,
        "ask_price2": 3287.0, "ask_volume2": 91,
        "ask_price3": 3288.0, "ask_volume3": 104,
        "ask_price4": 3289.0, "ask_volume4": 118,
        "ask_price5": 3290.0, "ask_volume5": 130,
        "expired": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class SplitSymbolTests(unittest.TestCase):
    def test_full_symbol(self):
        self.assertEqual(split_symbol("SHFE.rb2610"), ("SHFE", "rb2610"))

    def test_lowercase_exchange(self):
        self.assertEqual(split_symbol("dce.m2609"), ("DCE", "m2609"))

    def test_bare_code(self):
        self.assertEqual(split_symbol("rb2610"), ("", "rb2610"))


class ProcessorTests(unittest.TestCase):
    def test_full_mapping(self):
        quote = to_market_quote("SHFE.rb2610", make_tq_quote())
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.symbol, "SHFE.rb2610")
        self.assertEqual(quote.exchange, "SHFE")
        self.assertEqual(quote.instrument_id, "rb2610")
        self.assertEqual(quote.timestamp, 1755770165123)
        self.assertEqual(quote.last, 3286.0)
        self.assertEqual(quote.volume, 152340)
        self.assertEqual(quote.open_interest, 1839201)
        self.assertEqual(len(quote.bid), 5)
        self.assertEqual(len(quote.ask), 5)
        self.assertEqual(quote.bid[0], QuoteLevel(3285.0, 125))
        self.assertEqual(quote.ask[4], QuoteLevel(3290.0, 130))

    def test_nan_and_missing_fields(self):
        quote = to_market_quote("SHFE.rb2610", make_tq_quote(
            open=float("nan"), high=None, ask_price1=float("nan"), ask_volume1=float("nan"),
            ask_price2=float("nan"), ask_volume2=float("nan"),
        ))
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertIsNone(quote.open)
        self.assertIsNone(quote.high)
        self.assertEqual(len(quote.ask), 0)  # 空档截断

    def test_no_last_price_yet(self):
        quote = to_market_quote("SHFE.rb2610", make_tq_quote(last_price=float("nan")))
        self.assertIsNone(quote)

    def test_dict_shape(self):
        quote = to_market_quote("SHFE.rb2610", make_tq_quote())
        assert quote is not None
        payload = quote.to_dict()
        for key in ("symbol", "timestamp", "last", "open", "high", "low", "pre_close",
                    "volume", "open_interest", "bid", "ask"):
            self.assertIn(key, payload)
        self.assertEqual(payload["bid"][0], {"price": 3285.0, "volume": 125})


class QuoteCacheTests(unittest.TestCase):
    def test_set_get_all_clear(self):
        cache = QuoteCache()
        quote = to_market_quote("SHFE.rb2610", make_tq_quote())
        assert quote is not None
        self.assertIsNone(cache.get("SHFE.rb2610"))
        cache.set(quote)
        self.assertIs(cache.get("SHFE.rb2610"), quote)
        self.assertEqual(len(cache), 1)
        self.assertEqual(len(cache.all()), 1)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_concurrent_writes(self):
        import threading

        cache = QuoteCache()
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for _ in range(200):
                    quote = MarketQuote(
                        symbol=f"SHFE.rb{index:04d}", exchange="SHFE", instrument_id=f"rb{index:04d}",
                        timestamp=1, last=float(index), open=None, high=None, low=None, pre_close=None,
                        volume=None, open_interest=None,
                    )
                    cache.set(quote)
                    cache.get(quote.symbol)
            except Exception as error:  # pragma: no cover
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(cache), 8)


if __name__ == "__main__":
    unittest.main()

"""练习01 参考答案"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 引用项目代码

from market.cache import QuoteCache


class 带统计的柜子(QuoteCache):
    def count_by_exchange(self, exchange: str) -> int:
        with self._lock:                       # 和父类一致：共享数据要在锁内操作
            count = 0
            for quote in self._data.values():
                if quote.exchange == exchange:
                    count += 1
            return count
        # 更 Python 的一行写法：
        # return sum(1 for q in self._data.values() if q.exchange == exchange)


# 自检
if __name__ == "__main__":
    from market.model import MarketQuote
    柜子 = 带统计的柜子()
    for sym, ex in (("SHFE.rb2610", "SHFE"), ("SHFE.au2612", "SHFE"), ("DCE.m2609", "DCE")):
        柜子.set(MarketQuote(symbol=sym, exchange=ex, instrument_id=sym.split(".")[1],
                             timestamp=None, last=None, open=None, high=None, low=None,
                             pre_close=None, volume=None, open_interest=None))
    assert 柜子.count_by_exchange("SHFE") == 2
    assert 柜子.count_by_exchange("DCE") == 1
    print("✅ 练习01 答案验证通过")

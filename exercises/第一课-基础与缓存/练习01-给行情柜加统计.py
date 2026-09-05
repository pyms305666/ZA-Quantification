"""练习01：给行情柜加一个统计方法

目标：练习 类的继承、遍历字典、条件计数（docs/10 第 2 章）。
背景：QuoteCache 是项目的"带锁柜子"（market/cache.py）。现在要统计
      某个交易所（比如 SHFE）在柜子里有多少条行情。

做法：把 TODO 处补全，然后在本项目根目录运行：
    python "exercises/第一课-基础与缓存/练习01-给行情柜加统计.py"
看到三个 ✅ 即通过。

提示：
- self._data 是一个字典：{合约代码: MarketQuote}
- 遍历字典的值：for quote in self._data.values()
- 每条行情有 .exchange 属性（"SHFE"/"DCE"/...）
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 引用项目代码，别删

from market.cache import QuoteCache
from market.model import MarketQuote


def 假行情(symbol: str, exchange: str) -> MarketQuote:
    """造一条最小可用的测试行情（真实字段见 model.py）"""
    return MarketQuote(
        symbol=symbol, exchange=exchange, instrument_id=symbol.split(".")[1],
        timestamp=None, last=None, open=None, high=None, low=None,
        pre_close=None, volume=None, open_interest=None,
    )


class 带统计的柜子(QuoteCache):
    """继承 QuoteCache——柜子的原有功能全部保留，只加新本领"""

    def count_by_exchange(self, exchange: str) -> int:
        """统计柜子里指定交易所的行情条数"""
        # TODO: 实现（记得在 self._lock 内遍历，返回 int）
        return 0  # TODO 完成前先返回 0，让自检优雅显示 ❌


# ============ 以下是自检代码，不要改 ============
def 自检():
    柜子 = 带统计的柜子()
    柜子.set(假行情("SHFE.rb2610", "SHFE"))
    柜子.set(假行情("SHFE.au2612", "SHFE"))
    柜子.set(假行情("DCE.m2609", "DCE"))

    结果1 = 柜子.count_by_exchange("SHFE")
    结果2 = 柜子.count_by_exchange("DCE")
    结果3 = 柜子.count_by_exchange("CFFEX")

    print("✅ SHFE 统计正确（应为 2）" if 结果1 == 2 else f"❌ SHFE 统计={结果1}，应为 2")
    print("✅ DCE  统计正确（应为 1）" if 结果2 == 1 else f"❌ DCE 统计={结果2}，应为 1")
    print("✅ CFFEX 统计正确（应为 0）" if 结果3 == 0 else f"❌ CFFEX 统计={结果3}，应为 0")


if __name__ == "__main__":
    自检()

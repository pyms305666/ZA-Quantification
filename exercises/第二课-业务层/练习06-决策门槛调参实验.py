"""练习06：决策门槛调参实验（观察实验题）

目标：亲手感受 docs/11 §2.4 的两个门槛（MIN_SCORE=60、MIN_GAP=15）如何影响信号，
      并理解"为什么宁可观望也不硬给方向"。

做法：
1. 直接运行本文件（不需要网络，用合成数据）：
       python "exercises/第二课-业务层/练习06-决策门槛调参实验.py"
2. 观察"默认门槛"下的三个市场信号（涨→做多，跌→做空，震荡→观望）；
3. 按提示修改两个常量（在 TODO 处），再运行，把每次的观察写在"实验记录"里；
4. 完成第 7 章的思考题。

注意：本文件**只改这里自己的变量**，不改项目文件（改完记得还原）。"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import math
from market.evaluator import evaluate, MIN_SCORE, MIN_GAP
import market.evaluator as evaluator
from market.model import Instrument
from config import RiskConfig

# ---------- 合成行情生成器（与 tests/test_decision.py 同源思路） ----------

def trend_up(length, start=3000.0, step=2.0):
    values = [start + i * step + 3.0 * math.sin(i * 0.7) for i in range(length)]
    base = values[-5]
    values[-4:] = [base + 1.0, base + 3.0, base + 6.0, base + 10.0]
    return values


def trend_down(length, start=3200.0, step=2.0):
    values = [start - i * step + 3.0 * math.sin(i * 0.7) for i in range(length)]
    base = values[-5]
    values[-4:] = [base - 1.0, base - 3.0, base - 6.0, base - 10.0]
    return values


def flat(length, value=3000.0):
    return [value + (i % 3 - 1) * 0.5 + (i % 5 - 2) * 0.3 for i in range(length)]


def make_bars(closes):
    bars = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else close - 1
        bars.append({"datetime": 1700000000000 + i * 300000, "open": open_,
                     "high": max(open_, close) + 0.5, "low": min(open_, close) - 0.5,
                     "close": close, "volume": 100000.0, "open_interest": 0.0})
    return bars


RB = Instrument("SHFE.rb2610", "SHFE", "rb2610", "螺纹2610", "FUTURE", False, 1.0, 10)
RISK = RiskConfig(50000, 900, 1.8, 10)


def quote_for(last):
    return {"symbol": "SHFE.rb2610", "exchange": "SHFE", "instrument_id": "rb2610",
            "timestamp": 1700000000000, "last": last, "open": last - 5, "high": last + 8,
            "low": last - 8, "pre_close": last - 10, "volume": 100000,
            "open_interest": 1000000, "bid": [], "ask": []}


def 信号(名称, closes):
    k = make_bars(closes)
    klines = {86400: k, 3600: k, 900: k, 300: k}
    r = evaluate(RB, quote_for(closes[-1]), klines, RISK)
    print(f"  {名称}: {r['direction']}  多 {r['score_long']} / 空 {r['score_short']}")


# ---------- TODO 实验：修改下面两个数字做实验（改完记得还原 60 和 15） ----------
实验用_MIN_SCORE = 60      # TODO: 试 40 / 30
实验用_MIN_GAP = 15        # TODO: 试 5 / 0

evaluator.MIN_SCORE = 实验用_MIN_SCORE
evaluator.MIN_GAP = 实验用_MIN_GAP

print(f"=== 当前门槛：分数≥{实验用_MIN_SCORE} 且分差≥{实验用_MIN_GAP} ===")
信号("单边上涨市场", trend_up(120))
信号("单边下跌市场", trend_down(120))
信号("横盘震荡市场", flat(120))

# ---------- 实验记录（直接写在下面引号里提交） ----------
"""
实验记录：
1. 门槛降到 40/5 时，横盘震荡市场的信号变成了什么？
   答：

2. 你认为"更容易给信号"和"更谨慎"哪个更适合真实交易？为什么？
   答：

3. （还原 60/15 后）上涨市场多空分差是多少？离门槛 15 还差多少？
   答：
"""

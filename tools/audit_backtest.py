"""Reconcile actual fills and separate ledger costs from dynamic counterfactuals."""
import csv
from collections import Counter
from datetime import date, datetime
import gzip
import json
from pathlib import Path
from statistics import median
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from market.evaluator import evaluate
from market.decision_profiles import PROFILES
from research.backtest import History, Settings, simulate


def main():
    original=ROOT/'docs/回测/2026-09-26-rb2610'
    output=ROOT/'docs/回测/2026-09-26-成交核查'
    output.mkdir(parents=True,exist_ok=True)
    summary=json.loads((original/'results.json').read_text(encoding='utf-8'))
    with gzip.open(ROOT/'.tqsdk/backtests/rb2610-20250926-20260924.json.gz','rt',encoding='utf-8') as f:
        history=History(json.load(f))
    bars={b['datetime']:b for b in history.bars[60]}
    bridges=[]
    for group in ('baseline','common'):
        for result in summary[group]:
            trades=list(csv.DictReader((original/f"{group}-{result['mode']}-trades.csv").open(encoding='utf-8-sig')))
            reference=fees=slip=net=0
            distances=[]
            for t in trades:
                side=1 if t['side']=='long' else -1
                lots=int(t['lots']); entry=float(t['entry']); exit=float(t['exit'])
                tick=history.instrument.price_tick; multiple=history.instrument.volume_multiple
                start=int(datetime.fromisoformat(t['entry_time']).timestamp()*1000)
                end=int(datetime.fromisoformat(t['exit_time']).timestamp()*1000)
                assert abs(entry-(bars[start]['open']+side*tick))<1e-6
                assert float(t['fees'])==lots*10
                if t['reason'] in ('stop','target','gap_target'):
                    level=float(t['stop'] if t['reason']=='stop' else t['target'])
                    assert abs(exit-(level-side*tick))<1e-6
                elif t['reason'] in ('gap_stop','margin_call') or (t['reason']=='time_limit' and result['mode'] in ('medium','long')):
                    assert abs(exit-(bars[end]['open']-side*tick))<1e-6
                else:
                    assert abs(exit-(bars[end-60000]['close']-side*tick))<1e-6
                price_reference=((exit+side*tick)-(entry-side*tick))*side*lots*multiple
                actual_slip=2*tick*lots*multiple
                assert abs(price_reference-actual_slip-float(t['fees'])-float(t['net_pnl']))<.001
                reference+=price_reference; fees+=float(t['fees']); slip+=actual_slip; net+=float(t['net_pnl'])
                distances.append(abs(entry-float(t['stop']))/tick)
            assert abs(net-result['stats']['net_profit'])<.001
            bridges.append(dict(group=group,mode=result['mode'],label=result['label'],reference_pnl=reference,
                fees=fees,slippage=slip,net_pnl=net,trades=len(trades),
                median_stop_ticks=median(distances) if distances else None,
                exit_reasons=dict(Counter(t['reason'] for t in trades))))
    # Only direction/score/stop/data sufficiency are reused. They do not depend
    # on RiskConfig; quantities and execution are recomputed in every scenario.
    cache={}; last=[0]
    def signal(instrument,quote,klines,risk,mode,now):
        key=(mode,now)
        if key not in cache:
            r=evaluate(instrument,quote,klines,risk,mode,now)
            cache[key]={k:r.get(k) for k in ('data_ok','direction_en','score','stop')}
        return cache[key]
    def progress(mode,n,total):
        if time.monotonic()-last[0]>20:
            print('audit',mode,n,'/',total,flush=True); last[0]=time.monotonic()
    scenarios=[]
    start=date.fromisoformat(summary['audit']['common_start'])
    for fee,slippage in ((5,1),(0,0),(5,0),(0,1)):
        for mode in PROFILES:
            r=simulate(history,mode,Settings(fee_per_side=fee,slippage_ticks=slippage),start_date=start,
                       signal_fn=signal,progress=progress)
            if fee==5 and slippage==1:
                expected=next(x for x in summary['common'] if x['mode']==mode)
                assert r['stats']==expected['stats']
            scenarios.append(dict(mode=mode,label=r['label'],fee_round_trip=fee*2,slippage_per_side=slippage,
                                  stats=r['stats'],skips=r['skips']))
            print('scenario',fee,slippage,mode,r['stats']['net_profit'],r['stats']['trades'],flush=True)
    report=['# 成交模型核查与成本拆解','',
        '## 核查范围与结果','',
        '基于原缓存，逐笔核对全部 8 组账本的入场开盘价、退出原因对应价格、双边费用、滑点与净损益。基准参数重跑共同区间并与原统计逐项断言一致。', '',
        '尚未发现账本算术或手续费重复扣除错误。OHLC 撮合仍依赖成交假设：首分钟有成交视作可按开盘加滑点成交；没有盘口、成交量冲击、排队、涨跌停和部分成交，不能宣称真实成交已验证。', '',
        '模型行为会影响收益：超短线每个交易时段结束强平；目标按滑点后的入场距离重算；日线严格滞后一交易日；当前评分一满足门槛即在空仓时开仓，没有最小预期净收益过滤。以上是原报告所列研究规则，不是交易所规则。', '',
        '## 固定原有交易的损益桥接','',
        '保留原交易的时间、方向和手数，将成交价中的两侧滑点还原到参考价。参考价格损益 − 手续费 − 滑点 = 原净损益。该参考损益受到原来选出的交易、仓位及退出时间影响，不能称为独立零成本策略收益，也不能单独作为方向准确率。', '',
        '| 区间 | 模式 | 参考价格损益 | 手续费 | 滑点 | 净损益 | 止损距离中位数/跳 |','|---|---|---:|---:|---:|---:|---:|']
    for b in bridges:
        report.append(f"| {b['group']} | {b['label']} | {b['reference_pnl']:,.0f} | {b['fees']:,.0f} | {b['slippage']:,.0f} | {b['net_pnl']:,.0f} | {b['median_stop_ticks']} |")
    report+=['','## 改变费用后完整重跑共同区间','',
        '共同区间 2026-06-30 至 2026-09-24，各档各方案重新投入 5 万元。保持评分和持仓规则；费用改变后重新计算入场价、3R 目标、手数、保证金与交易路径。交易数可能不同，不能直接相减解释为纯成本贡献。此处同时给出固定账本桥接与动态反事实，避免混淆两者。','',
        '| 模式 | 开平费用/手 | 每边滑点/跳 | 净损益 | 收益率 | 最大回撤 | 交易数 |','|---|---:|---:|---:|---:|---:|---:|']
    for r in scenarios:
        s=r['stats']
        report.append(f"| {r['label']} | {r['fee_round_trip']} | {r['slippage_per_side']} | {s['net_profit']:,.0f} | {s['return_percent']:.2f}% | {s['max_drawdown_percent']:.2f}% | {s['trades']} |")
    report+=['','## 本次判断','',
        '去掉费用和滑点后，超短线仍亏 34,250 元，中线亏 2,700 元，长线亏 1,520 元；只有短线在共同区间盈利 3,490 元。不能把全部亏损都归因于成本。', '',
        '超短线原共同区间有 208 次普通止损、147 次时段/时限退出、7 次模型保证金强平、2 次跳空止损、10 次目标退出。止损距离中位数为 4 跳，每手双边 1 跳滑点加 10 元手续费等价于 3 跳，成本相对止损距离很大。中线的固定账本参考价格损益本身为负，長线仅 3 笔无法做稳定判断。', '',
        '因此优先问题是：评分门槛未经未来方向或止损/目标到达概率校准，频繁开仓没有足够的净收益过滤，分钟级止损与持仓退出规则不适配成本。以上是该单合约样本的诊断，不能当作全市场已证实规律。', '',
        '## 复现','', '`python tools/audit_backtest.py`。本次共享信号缓存只缓存与账户权益无关的方向、评分、止损和数据充足状态；每种费用方案重新撮合。原始信号不是方向预测概率，后续模型应直接检验未来收益标签，报告覆盖率并使用时间隔离的样本外验证。']
    (output/'核查报告.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
    (output/'audit.json').write_text(json.dumps(dict(bridges=bridges,scenarios=scenarios),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print('audit complete',flush=True)


if __name__=='__main__':main()

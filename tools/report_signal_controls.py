"""Report position-size attribution, matched random selection and Ridge comparison."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.research_signal_controls import OUT,CACHE

LABEL={'reference':'滚动树＋误差余量','frozen_anchor':'冻结初始树模型','conditional':'有条件替换树模型',
    'agreement':'有条件替换＋新旧同向','ridge_margin':'滚动岭回归＋误差余量'}
def number(v):return '—' if v is None else f'{v:.2f}'
def pct(v):return '—' if v is None else f'{v*100:.2f}%'

def main():
    result=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    check=json.loads((OUT/'核验记录.json').read_text(encoding='utf-8'));rows=result['results']
    lookup={r['arm']:r for r in rows};c=lookup['conditional'];lin=lookup['ridge_margin']
    lines=['# 固定一手、匹配随机筛选与线性模型对照','',
        '## 范围与结论边界','',
        '按事先保存的规则检查三件事：仓位变化是否影响此前盈利结论，筛选是否优于匹配随机对照，较简单的正则化线性模型是否改善表现。所有五组均披露；岭回归只使用一个固定正则化强度，没有搜索参数。',
        '区间为2025年10月1日至2026年9月24日，沿用八品种、15分钟信号与最长15分钟持仓、不隔夜、开平合计10元/手、各1跳滑点。保持风险和保证金约束，单一共享账户初始5万元。历史此前已被研究，不是新的独立测试。', '',
        f"本轮主要发现：有条件替换的原净收益{c['dynamic']['net']:.2f}元，缩为每笔1手后仍盈利{c['one_lot']['net']:.2f}元，因此此前盈利并非完全来自手数差异；但{pct(c['matched_random']['fraction_random_at_least_observed'])}的匹配随机对照不低于它，目前没有清晰的额外择时优势证据。固定岭回归一手净收益{lin['one_lot']['net']:.2f}元，本次简单线性候选未改善结果。",
        '保留机会相对全体被过滤机会的表现更好，并不自动说明筛选有效：品种、时段和方向构成可能不同，须同时看匹配随机对照。本轮不能认定候选可靠性和稳定性达标，软件算法不替换。', '',
        '## 1. 仓位大小与一手回放','',
        '| 模型 | 原变动手数笔数 | 原净收益元 | 原成交逐笔缩为1手净收益元 | 独立1手回放笔数 | 独立1手净收益元 | 1手胜率 | 1手回撤 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        d=r['dynamic'];s=r['one_lot']
        lines.append(f"| {LABEL[r['arm']]} | {d['trades']} | {d['net']:.2f} | {r['original_trades_rescaled_to_one_lot_net']:.2f} | {s['trades']} | {s['net']:.2f} | {pct(s['win_rate'])} | {pct(s['max_drawdown'])} |")
    lines += ['', '“原成交缩为1手”固定原来的入场、方向和离场，只将每笔净收益除以原手数，用于仓位归因。“独立1手回放”重新推进账户资金和持仓；若1手风险超过min(900元,权益1.8%)或保证金不足，则不成交。因此两者笔数和收益可能不同，不能混为一谈。', '',
        '## 2. 匹配随机筛选','',
        '| 模型 | 完成随机重复 | 尝试次数 | 候选1手收益元 | 随机中位数元 | 随机5%—95%范围元 | 随机不低于候选的比例 | 无其他选择的成交数 |',
        '|---|---:|---:|---:|---:|---|---:|---:|']
    for r in rows:
        q=r['matched_random'];bounds=q.get('random_5_95_net');interval='—' if bounds is None else f'{bounds[0]:.2f} 至 {bounds[1]:.2f}'
        lines.append(f"| {LABEL[r['arm']]} | {q['replicates']} | {q['attempts']} | {r['one_lot']['net']:.2f} | {number(q.get('random_median_net'))} | {interval} | {pct(q.get('fraction_random_at_least_observed'))} | {q.get('observations_in_no_alternative_strata',0)} |")
    lines += ['', '匹配月份、品种、15分钟时段和模型偏好方向，各组随机成交笔数与对应的一手候选完全一致。各匹配组内等概率无放回抽取；包含原观察值，避免人为排除候选本身。拒绝持仓重叠、资金不足或超风险预算的整组抽样。共计划1000次有效重复，每组最多尝试100000次；不足1000会原样披露，不放松匹配条件。',
        '表中尾部比例采用(1＋随机收益不低于观察值的次数)/(1＋有效重复次数)的小样本修正。滚动树参考组达到100000次尝试上限后仅得到77次可行对照，证据较弱；其他四组均达到1000次。',
        '这一对照衡量在相同品种/时段/方向配置下挑选具体机会的作用；它没有检验方向预测本身是否优于随机方向。资金可行性筛选会限制抽样分布，有些匹配组没有替代样本，故表中的比例不是独立的显著性证据，也不消除此前多轮研究产生的选择偏差。它不能单独作为部署门槛。', '',
        '## 3. 保留与过滤的机会','',
        '| 模型 | 保留机会数 | 保留每手平均净损益元 | 保留净盈利率 | 过滤机会数 | 过滤每手平均净损益元 | 过滤净盈利率 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        a=r['signal_groups']['retained'];b=r['signal_groups']['filtered']
        lines.append(f"| {LABEL[r['arm']]} | {a['opportunities']} | {number(a['mean_net_per_lot'])} | {pct(a['net_win_rate'])} | {b['opportunities']} | {number(b['mean_net_per_lot'])} | {pct(b['net_win_rate'])} |")
    lines += ['', '机会池只包含原成本过滤通过、按初始5万元可承担1手风险和保证金的时点。每个模型使用其自身最佳方向；同向过滤组使用过滤前的有条件替换模型方向。保留表示模型允许该机会，过滤表示观望。不同机会可能同时发生，这里统计逐机会净损益，不能相加宣称可同时执行的组合收益。实际成交数还受共享账户限制。', '',
        '## 4. 简单模型如何比较','',
        '岭回归与滚动树使用相同37项输入、24个月训练窗口、最近1个月校准、365日半衰期样本权重和分组误差余量。数值特征使用仅在训练样本拟合的StandardScaler；品种和时段类别独热编码。拟合做多/做空净R，R仍包含止损尺度及既定交易成本。',
        '固定Ridge alpha=100，对应加权残差平方和加100倍系数平方和；仅试这一项，不从结果反向选择正则化强度。模型仍是主效应线性关系，不能表达树模型的复杂交互。公平的直接复杂度对照是“滚动岭回归”对“滚动树”；有条件替换和冻结组另有更新规则，不能将全部差异归因于模型复杂度。', '',
        '## 5. 大盈利集中度','',
        '| 模型 | 独立1手回放净收益元 | 最大3笔净盈利合计元 | 去掉这3笔后的净收益元 |',
        '|---|---:|---:|---:|']
    for r in rows:lines.append(f"| {LABEL[r['arm']]} | {r['one_lot']['net']:.2f} | {r['top3_profit']:.2f} | {r['net_without_top3']:.2f} |")
    lines += ['', '仅用于描述集中度，不据此删交易或优化参数。样本不足3笔时使用全部已有交易。', '',
        '## 核验与软件替换','',f"研究程序核验：{'通过' if check['passed'] else '未通过'}。"]
    lines += ['- '+s for s in check['checks']]
    lines += ['', '本轮没有允许软件替换。之前保存的历史样本、盈利月份、收益/回撤、成本压力、新行情至少60个交易日和100笔模拟成交及桌面/手机集成要求继续生效。固定一手只是归因实验，不能代替原风险预算下的部署评估；减少手数导致的回撤下降也不能当作信号准确率提升。', '',
        '复现：','', '```powershell','python tools/research_signal_controls.py fit','python tools/research_signal_controls.py run',
        'python tools/research_signal_controls.py verify','python tools/report_signal_controls.py','```','',
        'protocol.json在训练前保存；results.json、核验记录.json及五组一手成交CSV保留结果。模型、随机抽样索引和对应收益位于.tqsdk/research/signal_controls；每一次随机抽样均可核对分组数量、风险可行性和账本。']
    (OUT/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('Signal control report saved',flush=True)

if __name__=='__main__':main()

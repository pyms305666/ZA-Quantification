"""Render factor diagnostics, all fixed candidates, and explicit research limits."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/研究/2026-09-27-决策因子与成本状态'


def pct(v):return '—' if v is None else f'{v:.2%}'


def money(v):return '—' if v is None else f'{v:.2f}'


def main():
    d=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    names={'all_factors':'全部原因子校准','without_trend':'去掉趋势','without_momentum':'去掉动量',
           'without_volume':'去掉量仓','without_risk':'去掉风险'}
    def label(key):
        if key in names:return names[key]
        return key.replace('cost_factors','成本三分类＋原因子').replace('cost_context','成本三分类＋行情状态')
    lines=['# 决策评估因子诊断与成本状态对照', '',
           '**本轮固定对照已完成，没有候选通过验证门槛，软件算法接入结论为不替换。** 目前可继续积累新行情证据，但不能把历史诊断称为已经提高软件胜率。', '',
           '## 使用的样本与预声明方案', '',
           '沿用此前直接还原原超短线评估器的76,406次八品种记录。训练截止2025-06-30，验证为2025年7–9月，后段评价为2025-10-01至2026-09-24。这些历史此前已经查看过，本轮属于假设诊断，不是新的独立检验。输入只使用预测时已知数据。', '',
           '所有模型都在运行前固定为标准化＋C=1逻辑回归，不搜索参数。五组二分类对照分别使用全部8项因子分值，或去掉一类因子；训练目标为非平价15分钟涨跌，观望阈值在验证段匹配原算法的出手覆盖率。', '',
           '另两组模型把目标改为“上涨足以覆盖成本 / 下跌足以覆盖成本 / 幅度不足”，包含价格不变样本。输入分别为原因子，或原因子＋35项历史行情特征＋8个品种标识。四个预先固定概率门槛为0.50、0.55、0.60、0.65，并要求所选方向概率高于幅度不足概率。合计13个候选全部保留结果。', '',
           '本轮验证候选要求：至少500次建议、覆盖率至少15%、15分钟方向准确率及超过成本比例高于原算法、平均一手终点净变动非负。这是本轮预先设定的筛选条件，不是永久产品要求；低覆盖率模型也保留供审查。选择决定先写入validation-selection.json，然后才披露后段结果。', '',
           '费用口径为每手开平10元＋每边1跳滑点。终点净变动未应用止损、盘口、仓位互斥和账户资金限制，且不同品种每手金额不同，不能累加为账户收益。零建议记作无法判断，不能算作零亏损的有效策略。', '',
           '## 因子诊断', '',
           '- 风险多分与风险空分在训练和验证数据中100%相同。它影响是否达到总分门槛，对多空分差贡献为零；这不是代码算术错误，也不能直接推断删除它会改善准确率。',
           '- 趋势与动量分差相关系数约0.149，趋势与量仓约0.037，动量与量仓约0.388。未出现足以说明“完全重复计分是主要问题”的高相关；误判仍需从预测目标、权重、市场状态和成本综合分析。',
           '- 原规则对因子的有利方向预先给正分，但训练得到的上涨条件权重并不总是同向。这提示固定加分规则缺少目标期限校准；回归系数受其他变量、标准化和市场阶段影响，不能凭负系数反向交易。',
           '- 去掉一类因子后的后段准确率大多仍在49%附近，简单删除指标没有解决信号优势问题。', '',
           '## 验证段：全部候选', '',
           '| 候选 | 建议数 | 覆盖率 | 15分钟准确率 | 超过成本比例 | 一手终点净变动/次（元） |',
           '|---|---:|---:|---:|---:|---:|']
    original=d['validation_selection']['original_validation']
    lines.append(f"| 原算法 | {original['signals']} | {pct(original['coverage'])} | {pct(original['accuracy'])} | {pct(original['cost_hurdle_hit'])} | {money(original['mean_net_one_lot'])} |")
    for key,value in d['validation_selection']['validation'].items():
        m=value['15'];lines.append(f"| {label(key)} | {m['signals']} | {pct(m['coverage'])} | {pct(m['accuracy'])} | {pct(m['cost_hurdle_hit'])} | {money(m['mean_net_one_lot'])} |")
    lines += ['', '选择结果：无候选通过。原始分值的成本三分类在四档门槛下均不给建议；这表示模型把握不足，不能称为改善盈利。加入行情状态的0.50门槛在验证段给84次建议、准确率64.29%，但覆盖率仅1.29%，不足以证明可靠性。', '',
              '## 固定后段结果', '',
              '| 候选 | 建议数 | 覆盖率 | 15分钟准确率 | 超过成本比例 | 一手终点净变动/次（元） |',
              '|---|---:|---:|---:|---:|---:|']
    original=d['test_original']['15']
    lines.append(f"| 原算法 | {original['signals']} | {pct(original['coverage'])} | {pct(original['accuracy'])} | {pct(original['cost_hurdle_hit'])} | {money(original['mean_net_one_lot'])} |")
    for key,value in d['test_candidates'].items():
        m=value['15'];lines.append(f"| {label(key)} | {m['signals']} | {pct(m['coverage'])} | {pct(m['accuracy'])} | {pct(m['cost_hurdle_hit'])} | {money(m['mean_net_one_lot'])} |")
    lines += ['', '验证段显示64.29%的候选在后段降至47.72%，一手终点净变动为−55.27元。更高概率门槛也没有稳定改善。所有候选均未被验证段选中，因此后段表是固定诊断披露，不能再据此挑选最高准确率或最小亏损版本接入软件。', '',
              '## 行情状态诊断', '',
              '三个状态均按训练数据的三等分阈值划分，验证和后段沿用相同阈值。以下列原算法表现，完整校准对照见results.json。', '',
              '| 状态 | 档位（低/中/高） | 验证建议数 | 验证准确率 | 验证终点净变动 | 后段准确率 | 后段终点净变动 |',
              '|---|---|---:|---:|---:|---:|---:|']
    correction={}
    for key,state in d['states'].items():
        name=key
        if key=='分钟路径效率':
            name='分钟实现波动/15分钟ATR'
            correction[key]=dict(actual_feature_index=28,actual_name=name,
                reason='Index 28 is minute_realized_vol_atr; true minute_path_efficiency is index 29. This corrects a diagnostic heading only, not model inputs or selection.')
        for bucket,title in [('0','低'),('1','中'),('2','高')]:
            v=state['validation'][bucket]['original'];t=state['test'][bucket]['original']
            lines.append(f"| {name} | {title} | {v['signals']} | {pct(v['accuracy'])} | {money(v['mean_net_one_lot'])} | {pct(t['accuracy'])} | {money(t['mean_net_one_lot'])} |")
    save=OUT/'状态字段说明.json';save.write_text(json.dumps(correction,ensure_ascii=False,indent=2),encoding='utf-8')
    lines += ['', '原始结果中的“分钟路径效率”状态标题有命名错误：其实际字段为分钟实现波动/ATR。这里已明确纠正，并保留原始结果与字段说明；模型使用的数值与选择结果不受影响。没有据此重新挑状态过滤条件。', '',
              '成本占止损空间比例较高时，后段原算法准确率及超过成本比例偏低，但验证段没有同样清晰的优势，而且三档验证终点净变动均为负；目前不能仅靠成本过滤宣称可靠。波动档位的表现也有时间变化，不能按后段表现事后删掉某一档。', '',
              '## 实时记录核验与接入结论', '',
              '对真正的观察程序run循环进行隔离核验，使用虚拟行情接口和时间、真实SQLite及冻结候选模型：正常预测完成15/30分钟结局；旧报价不产生预测；迟到终点标为缺失；预测落盘过晚不进入有效样本。四项通过，临时账本与真实前向账本隔离，详见观察程序核验.json。这确认程序路径可工作，不构成真实市场准确率证据。', '',
              '原算法与上一轮冻结候选的实时配对记录继续运行。本轮新候选全部未通过，因此不替换正在观察的候选、不改动桌面/手机方向算法。新行情的结果、连续性、分品种稳定性及可交易账户回放仍需验证；单靠历史方向准确率无法保证不增加亏损。', '',
              '已完成：原因子消融、成本三分类、行情状态扩展、全部候选的验证/后段披露、状态分组诊断、观察程序故障核验和真实后台进程检查。当前已有样本尚不能支持上线可靠模型；报告保留失败结果供后续研究。', '',
              '复现：`python tools/research_decision_factors.py`、`python tools/verify_decision_observer.py`、`python tools/report_decision_factors.py`。模型与预测在 `.tqsdk/research/decision_factors`；协议、验证选择、完整指标和核验记录位于本目录。实时报告见 [八品种前向状态](../2026-09-27-决策准确度多品种/研究报告.md)。', '']
    (OUT/'研究报告.md').write_text('\n'.join(lines),encoding='utf-8')
    decision=dict(replace_software=False,replace_forward_candidate=False,validation_selected=None,
        reason='No validation passer; fixed historical diagnostics do not establish reliable directional advantage or non-worsening trading risk.',
        offline_checks_complete=True,fresh_market_evidence_required=True)
    (OUT/'接入结论.json').write_text(json.dumps(decision,ensure_ascii=False,indent=2),encoding='utf-8')
    print(OUT/'研究报告.md')


if __name__=='__main__':main()

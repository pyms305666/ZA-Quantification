"""Report all replacement-gate results and blockers, without promoting a model."""
import csv
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.research_gated_15m import OUT

LABELS={'reference':'原滚动误差余量参考','frozen_anchor':'始终冻结初始候选','conditional':'有条件替换','agreement':'有条件替换＋新旧同向'}
def pct(v):return '—' if v is None else f'{v*100:.2f}%'
def num(v):return '—' if v is None else f'{v:.2f}'

def main():
    result=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    check=json.loads((OUT/'核验记录.json').read_text(encoding='utf-8'))
    tick_check=json.loads((OUT/'盘口核验记录.json').read_text(encoding='utf-8'))
    ticks=json.loads((OUT/'tick-audit.json').read_text(encoding='utf-8'))
    rows=result['results'];changes=[d for d in result['decisions'] if d['actually_changed']]
    lines=['# 有条件替换与新旧模型一致性研究','',
        '## 当前决定：不替换软件算法','',
        '用户已授权可靠性、稳定性达标后替换软件算法。本轮出现小样本正收益，但没有候选同时达到预先保存的全部门槛，也没有新行情前向证据，因此授权的触发条件尚未满足。桌面和手机市场算法源码指纹保持不变。',
        '研究中的“旧模型”指此前冻结的研究候选，不是对当前正式软件算法的同义称呼。上线前还需与正式算法同口径对照和桌面/手机集成验证。', '',
        '## 全部结果','',
        '统一历史区间2025年10月1日至2026年9月24日；15分钟K线信号、最长持仓15分钟、不隔夜。起始5万元，手续费每手开平10元、各1跳滑点，风险和手数上限沿用上一轮。最后一个月不完整；这段历史已被研究过，不是新独立测试。','',
        '| 策略 | 成交 | 净收益元 | 胜率 | 盈利因子 | 最大回撤 | 信号覆盖率 | 盈利月/12 | 历史门槛通过 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        s=r['historical']
        lines.append(f"| {LABELS[r['arm']]} | {s['trades']} | {s['net']:.2f} | {pct(s['win_rate'])} | {num(s['profit_factor'])} | {pct(s['max_drawdown'])} | {pct(s['coverage'])} | {r['positive_months']} | {'是' if r['historical_passed'] else '否'} |")
    fixed=next(r for r in rows if r['arm']=='frozen_anchor')['historical']['net']
    conditional=next(r for r in rows if r['arm']=='conditional')['historical']['net']
    lines += ['',f"12次月度检查中实际替换{len(changes)}次。一直冻结初始候选同样盈利，且有条件替换相对它的净收益差额为{conditional-fixed:+.2f}元。因此不能把相对原滚动参考的全部改善归因于替换机制。",'',
        '## 更新规则及因果边界','',
        '月M使用候选模型之前，先用M−2和M−1两个月验证。候选训练截止M−3月开始之前，M−3月仅作校准。例如2025年10月的候选：训练2023年7月至2025年6月，2025年7月校准，8—9月验证，10月使用。',
        '候选的24个月训练、1个月分组校准、误差余量公式沿用上一轮；在本轮运行前锁定四组策略。初始候选固定为2025年8月可用版本，不从结果中挑选最优起点。新月候选先与当时保留的旧候选比较，决策写入月度文件后才进入该月回放。',
        '替换条件：此前两个月至少20笔、总收益为正、盈利因子≥1.2、两个月均盈利、回撤≤6.8785%，并且净收益不低于旧候选。若旧候选在窗口内也有至少20笔，还要求回撤不高于旧候选；旧候选样本不足时只用绝对回撤上限，避免把零成交回撤误当成可比基准。条件不全满足则保留旧候选。',
        '同向组额外比较保留候选与当月最新滚动候选：两者最佳方向一致且各自扣除误差余量的净R得分≥0才允许考虑开仓，采用两者得分较小值排序。最新滚动候选只使用预测月开始之前已结束的训练与校准标签。',
        '所有策略仍采用相同成本/止损空间过滤、15分钟到期退出、1.5ATR止损、单一共享持仓、5万元连续账户、最多10手及min(900元,权益1.8%)风险预算。回测账户跨月不重置。保留旧模型或少交易也不能保证未来亏损不增加。','',
        '## 逐月更新决策','',
        '| 使用月 | 候选版本 | 验证笔数 | 验证净收益元 | 最终保留版本 | 实际更换 | 未通过条件 |',
        '|---|---|---:|---:|---|---|---|']
    rule_names=dict(enough_trades='不足20笔',net_positive='未盈利',profit_factor='盈利因子不足',drawdown_cap='回撤超限',not_worse_net='收益劣于旧版',not_worse_drawdown='回撤劣于旧版',both_months_positive='非两月均盈利')
    for d in result['decisions']:
        failed='、'.join(rule_names[k] for k,v in d['rules'].items() if not v) or '无'
        lines.append(f"| {d['month']} | {d['candidate']} | {d['candidate_validation']['trades']} | {d['candidate_validation']['net']:.2f} | {d['selected']} | {'是' if d['actually_changed'] else '否'} | {failed} |")
    lines += ['', '## 盈利集中度','',
        '下表仅描述对少数大盈利的依赖，不据此删除交易或再次调参；也不是显著性检验。', '',
        '| 候选 | 最大盈利单元 | 最大3笔盈利合计元 | 去掉最大3笔后的净收益元 |','|---|---:|---:|---:|']
    for arm in ['frozen_anchor','conditional','agreement']:
        with (OUT/f'{arm}-成交.csv').open(encoding='utf-8-sig',newline='') as f:pnl=[float(r['net']) for r in csv.DictReader(f)]
        top=sum(sorted(pnl,reverse=True)[:3])
        lines.append(f'| {LABELS[arm]} | {max(pnl):.2f} | {top:.2f} | {sum(pnl)-top:.2f} |')
    lines += ['', '## 全部候选交易的真实历史盘口核查','',
        f"覆盖三个候选成交清单的全部{ticks['unique_episodes']}个独立合约/入场时点，没有按盈亏挑样本。读取实际get_tick_serial买一卖一及挂单量。保持原成交时点和手数，按可执行对手价检查止损，分别计算显示报价及显示报价外再加各1跳。", '',
        '| 候选 | 可计价/全部成交 | 原账本净收益元 | 匹配样本原净收益元 | 显示报价净收益元 | 报价外各1跳净收益元 | 显示量不足 | 报价间隔超5秒 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm,q in ticks['results'].items():
        lines.append(f"| {LABELS[arm]} | {q['usable']}/{q['total']} | {q['original_net']:.2f} | {q['matched_original_net']:.2f} | {q['displayed_quote_net']:.2f} | {q['quote_plus_tick_net']:.2f} | {q['insufficient_displayed_depth']} | {q['episodes_with_quote_gap_over_5s']} |")
    lines += ['', '报价核查没有重新计算共享账户的入场安排和资金手数；实际盘口止损可能改变离场时间，故该合计不能当成完整组合收益。显示量不保证实际到达可成交，缺少排队、延迟和市场冲击仿真。缺报价样本不能按零损益处理，须对照有效样本的原收益。这里仍是历史盘口，不计入新行情前向交易数量。', '',
        '## 压力情景与部署门槛','',
        '| 策略 | 各2跳滑点成交 | 各2跳滑点净收益元 | 各2跳滑点回撤 |', '|---|---:|---:|---:|']
    for r in rows:
        s=r['stress_two_ticks'];lines.append(f"| {LABELS[r['arm']]} | {s['trades']} | {s['net']:.2f} | {pct(s['max_drawdown'])} |")
    lines += ['', '压力情景固定预测，重新计算成交、手数和共享资金路径；与上一节固定手数的盘口核查不同。', '',
        '历史筛选要求：至少100笔、净收益为正、盈利因子≥1.2、回撤不高于原滚动参考6.8785%、收益不低于原参考、12个月至少8个月盈利、各2跳滑点压力情景净收益非负。候选目前主要未通过成交数量和盈利月份要求。不能为保留小样本盈利而事后降低门槛。',
        '软件替换还要求新策略声明后的至少60个交易日、100笔新行情模拟成交，预测必须在结果发生前留痕，扣费后净收益为正、盈利因子≥1.2、回撤≤6.8785%，并完成真实盘口成交核查与桌面/手机集成验证。当前没有这样的新前向记录；历史回放不能补作前向记录。',
        '这些门槛是研究与上线审查条件，不是未来不亏损保证。本轮未启动后台定时任务或真实交易。', '',
        '## 核验与复现','',f"程序核验：{'通过' if check['passed'] else '未通过'}。"]
    lines += ['- '+s for s in check['checks']]
    lines += ['',f"独立盘口逐条重算：{'通过' if tick_check['passed'] else '未通过'}，核对{tick_check['checked_trade_records']}条候选交易记录（策略间有重叠，共{tick_check['unique_episodes']}个时点）。这只证明价格计算和数据对应关系可复现，不证明实际挂单成交。"]
    lines += ['', '复现命令：','', '```powershell','python tools/research_gated_15m.py run','python tools/research_gated_15m.py verify',
        'python tools/audit_gated_tick_fills.py','python tools/verify_gated_ticks.py','python tools/report_gated_15m.py','```','',
        'protocol.json保存运行前声明；decisions/保存每月验证与选择；results.json保存全部结果；deployment-decision.json明确记录当前不替换及条件授权；核验记录.json、tick-audit.json及四组成交CSV保存证据。模型及预测位于.tqsdk/research/gated15m。']
    (OUT/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('Conditional replacement report saved',flush=True)

if __name__=='__main__':main()

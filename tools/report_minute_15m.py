"""Generate the fine-data comparison report from stored evidence."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/研究/2026-09-26-分钟路径模型'


def main():
    r=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    tick=json.loads((OUT/'tick-fill-audit.json').read_text(encoding='utf-8'))
    diagnostic=json.loads((OUT/'post-result-diagnostics.json').read_text(encoding='utf-8'))
    verification=json.loads((OUT/'核验记录.json').read_text(encoding='utf-8'))
    def pct(x):return '不可计算' if x is None else f'{100*x:.2f}%'
    a=r['audit'];lines=['# 分钟路径与历史盘口：15分钟模型验证','',
        '本轮沿用低成本组8品种，在相同样本、相同1分钟成交路径上，比较原28个条件特征与增加9个分钟内部特征的模型。信号仍为15分钟收盘、最长持有15/30分钟，不隔夜。','',
        '| 模型 | 验证通过 | 验证成交 | 验证净收益元 | 后段成交 | 后段净收益元 | 后段净盈利胜率 | 后段回撤 | 信号覆盖率 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,result in r['results'].items():
        v=result['selection']['choice']['validation'];s=result['historical']
        lines.append(f"| {name} | {'是' if result['selection']['approved'] else '否'} | {v['trades']} | {v['net']:.2f} | {s['trades']} | {s['net']:.2f} | {pct(s['win_rate'])} | {pct(s['max_drawdown'])} | {pct(s['coverage'])} |")
    selection=json.loads((OUT/'selection.json').read_text(encoding='utf-8'))
    cases=selection['comparisons']
    passed=sum(q['validation']['trades']>=100 and q['validation']['net']>0
        and (q['validation']['profit_factor'] or 0)>=1.2 and q['validation']['max_drawdown']<=.1 for q in cases)
    lines += ['',f'36组验证中通过研究门槛的组合：{passed}组。', '',
        '| 冻结候选 | 最长持有 | 净R门槛 | 净盈利概率门槛 |', '|---|---:|---:|---:|']
    for name,result in r['results'].items():
        c=result['selection']['choice']
        lines.append(f"| {name} | {c['horizon']*15}分钟 | {c['edge']} | {pct(c['floor'])} |")
    if not passed:
        lines += ['', '本轮没有得到达到筛选要求的可用盈利模型。后段回放仅用于诊断失败原因，不构成上线依据。']
    else:
        lines += ['', '通过验证门槛仅代表进入后段评价。本轮分钟特征30分钟候选在验证期盈利，但后段亏损且回撤明显扩大，因此仍不能认定为可用盈利模型。']
    lines += ['', '## 相同参数对照与失败定位', '',
        '以下为看到结果后追加的描述性拆分，不重新选择模型。两组均固定净R门槛0、概率门槛0；因此可以在相同持仓期下比较特征变化。上表各自冻结候选的持仓期不同，不能将上表全部差额归因于新增特征。', '',
        '| 特征 | 持仓上限 | 后段成交 | 后段净收益元 | 净盈利胜率 | 最大回撤 |',
        '|---|---:|---:|---:|---:|---:|']
    for q in diagnostic['same_settings_comparison']:
        s=q['historical']
        lines.append(f"| {q['model']} | {q['horizon']*15}分钟 | {s['trades']} | {s['net']:.2f} | {pct(s['win_rate'])} | {pct(s['max_drawdown'])} |")
    q=diagnostic['selected_policy_breakdown']['minute']
    lines += ['',f"分钟模型后段平均盈利单收益{q['average_win']:.2f}元、平均亏损单损失{q['average_loss']:.2f}元。胜率下降时，盈亏幅度未形成补偿。以下保留全部月份（首尾为不完整月）和品种；不能按这些已知结果再次筛选并宣称独立验证。", '',
        '| 拆分 | 成交 | 净收益元 | 净盈利胜率 |','|---|---:|---:|---:|']
    for g in q['groups']:
        lines.append(f"| {g['kind']}/{g['label']} | {g['trades']} | {g['net']:.2f} | {pct(g['win_rate'])} |")
    lines += ['',
        '研究筛选要求：验证至少100笔、净收益为正、盈利因子≥1.2、回撤≤10%（暂定研究限制），胜率完整披露。盈利因子为总盈利/总亏损。在主模型拟合前增加仅净期望门槛的12组对照，保留原24组概率门槛；正期望不要求胜率必然超过50%。这是对此前筛选假设的适应性修订，初版规则另存protocol-initial.json。未通过时展示诊断候选，不表示可以交易。后段历史此前已查看，不能称新独立测试。','',
        '## 数据是否可靠','',
        f"完整请求清单488个合约月份，读取使用账户获授权的公开TqBacktest/get_kline_serial。原机会{a['source_opportunities']}条，具备连续15分钟输入和30分钟未来路径{a['complete']}条，与原15分钟OHLC及输入柱成交量相符{a['matched']}条。缺失文件{a['missing_files']}个。",
        f"零成交量入场分钟{a['zero_volume_entry_minutes']}条；OHLC不能证明这些时点有可执行价格，仍需盘口检查。缺报价/不一致样本如有被明确剔除，覆盖率以可评价机会为分母。实际训练/校准/验证/后段条数：{r['counts']}。",'',
        '| 最长持有 | 双向机会数 | 分钟路径导致每手亏损加深的次数 | 最大额外每手损失元 | 平均每手差额元 |',
        '|---|---:|---:|---:|---:|']
    for horizon,q in a['execution_resolution_audit'].items():
        lines.append(f"| {int(horizon)*15}分钟 | {q['direction_opportunities']} | {q['worsened']} | {q['maximum_extra_loss_per_lot']:.2f} | {q['mean_pnl_difference']:.4f} |")
    old=r['previous_frozen_policy_minute_replay']
    lines += ['',
        f"上一轮低成本模型的原信号和阈值在匹配样本上改用分钟路径回放：{old['trades']}笔、净收益{old['net']:.2f}元、净盈利胜率{pct(old['win_rate'])}。这项没有重新训练，用于区分执行分辨率与模型变化。",'',
        '## 新增信息及公平对照','',
        '新增已完成15分钟内部的实现波动、路径效率、前5/后5分钟涨跌、最后3分钟成交量占比、分钟涨跌方向加权成交量、近似VWAP偏离、分钟收益自相关及最大单分钟变化。方向加权成交量只是代理变量，不能称真实主动买卖量或订单流。所有输入时间严格早于信号形成时刻。',
        '两个模型用同一数据集、同一近期权重、同一模型参数和独立7月概率校准。主训练2023年7月至2025年6月，校准2025年7月，验证2025年8月1日至9月25日，后段2025年9月26日至2026年9月24日。每组2持有期×3净R门槛×3概率门槛，共36组；概率门槛0表示不额外要求50%/55%盈利分值。规则预先保存，冻结验证选择后才计算后段收益。',
        '做多/做空分别估计成本后净R与净盈利概率。合格方向需净R、概率以及默认成本/止损空间≤0.35同时满足，再在合格方向中选预计净R较大的方向。期末退出、1.5ATR止损，不设固定止盈。止损按每分钟路径检查；分钟开盘跳过止损时按更差开盘价，分钟内触价仍只是成交假设。',
        '数学定义：方向d取+1或−1，每手净损益Y=d×(退出成交价−入场成交价)×合约乘数−10；成交价已包含两端不利滑点。风险尺度R=止损距离×乘数+10+2×最小跳动×乘数。分别拟合μ_d(x)=E[Y/R|x]和p_d(x)=P(Y>0|x)，只在μ_d超过净R门槛且p_d超过概率门槛时考虑该方向。这是由历史样本估计的条件期望，不是保证未来收益的公式。',
        '手续费每手开平合计10元、各1跳不利滑点，5万元共享账户、最多1个持仓、最多10手、单笔预算min(900元,权益1.8%)，保证金统一15%假设；缺少历史逐日规格及真实保证金档案。','',
        '## 真实Tick探测与5笔核查','',
        '公开get_tick_serial实测取得历史买一卖一、挂单量、累计成交量和成交额，二至五档为空。探测成功只说明这些时点可取得最优一档，不等于全部年份或五档数据完整。没有把K线生成的模拟quote当作真实盘口。',
        '按时间顺序取上一轮前5笔交易，使用首个信号后报价及持有期末报价；最多容忍5秒报价等待。止损以对手方可执行一档报价触发，和原柱内止损定义略有不同。保持原手数和每手10元手续费，分别计算显示报价、显示报价再加各1跳。','',
        '| 合约/入场毫秒时间 | 原模型净收益元 | 显示报价净收益元 | 报价外再各1跳元 | 显示量足够 |',
        '|---|---:|---:|---:|---|']
    for q in tick['results']:
        if not q['usable']:
            lines.append(f"| {q['symbol']}/{q['entry_ms']} | {q['original_net']:.2f} | 数据不足 | 数据不足 | 未验证 |")
        else:
            zero,one=q['variants']
            lines.append(f"| {q['symbol']}/{q['entry_ms']} | {q['original_net']:.2f} | {zero['net']:.2f} | {one['net']:.2f} | {'是' if zero['enough_displayed_depth'] and one['enough_displayed_depth'] else '否'} |")
    lines += ['',
        '显示挂单量不保证订单抵达后仍可成交；没有排队、延迟和市场冲击模型。5笔为成交诊断，不能估计总体胜率或训练订单流模型。本轮分钟模型没有使用Tick特征。','',
        '## 固定成交账本的成本拆解','',
        '保持实际成交时点、退出决定和手数不变，只还原账本中计入的手续费和两端滑点。这是会计拆分；重新设置零成本会改变止损、手数及后续可成交机会，结果见下一节。','',
        '| 模型 | 计入成本前元 | 手续费元 | 滑点元 | 实际净收益元 | 零量入场成交 |',
        '|---|---:|---:|---:|---:|---:|']
    for name,q in diagnostic['selected_policy_breakdown'].items():
        c=q['fixed_ledger_costs']
        lines.append(f"| {name} | {c['gross_before_accounted_costs']:.2f} | {c['fees']:.2f} | {c['slippage']:.2f} | {c['net']:.2f} | {q['zero_volume_entry_trades']} |")
    lines += ['', '本轮候选的固定账本毛收益很薄，不能覆盖既定交易成本。更细的分钟特征没有在后段带来可靠收益改善；一个验证期达标组合并不足以支持部署。下一步若继续研究，应预先固定滚动训练与筛选流程，并增加真实盘口的成片样本，用新前向区间检验；不能仅从已知亏损月份和品种中反复删选。']
    lines += ['', '## 成本压力','',
        '| 模型/费用与滑点 | 成交 | 净收益元 | 胜率 | 回撤 |','|---|---:|---:|---:|---:|']
    for name,result in r['results'].items():
        for label,s in result['cost_stress'].items():
            lines.append(f"| {name}/{label} | {s['trades']} | {s['net']:.2f} | {pct(s['win_rate'])} | {pct(s['max_drawdown'])} |")
    lines += ['',
        '压力情景固定预测和门槛，重新计算执行、手数与资金路径，收益之差不能直接等同固定交易的成本贡献。反复研究同一历史仍有选择偏差；无论此次结果好坏，都需要新的前向数据才能判断部署可用性。','',
        '## 文件与复现','',
        f"核验结果：{'通过' if verification['passed'] else '未通过'}。核对独立止损/费用算例、24个原始分钟输入窗口、数据/模型/代码指纹、保存预测重算、精确回放以及账本/持仓/日期边界。核验通过表示研究实现可复现，不表示策略有效。",
        '数据、模型、预测缓存位于.tqsdk/research/fine15m；protocol.json、selection.json、results.json、data-audit.json、tick-fill-audit.json、核验记录.json及两组成交CSV保留证据。',
        '依次运行tools/collect_fine_minutes.py、tools/prepare_minute_model.py、tools/research_minute_15m.py、tools/audit_tick_fills.py、tools/verify_minute_15m.py、tools/diagnose_minute_15m.py、tools/report_minute_15m.py。protocol.json已保存，本次规则在下载完成之前声明；--declare仅用于从预设生成规则。',
        '官方说明：[历史回测中真实Tick与K线生成quote的区别](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.backtest.html)、[Tick序列接口](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html#tqsdk.TqApi.get_tick_serial)。']
    (OUT/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':main()

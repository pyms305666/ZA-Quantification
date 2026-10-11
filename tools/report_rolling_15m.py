"""Readable report and closed-trade equity figure for all frozen rolling arms."""
import csv
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pandas as pd
from tools.research_rolling_15m import OUT

NAMES={'static':'原冻结模型','rolling_global':'每月重训＋整体校准',
    'rolling_bins':'每月重训＋分组校准','rolling_margin':'分组校准＋误差余量'}

def pct(value):return '—' if value is None else f'{100*value:.2f}%'
def number(value):return '—' if value is None else f'{value:.2f}'

def main():
    result=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    check=json.loads((OUT/'核验记录.json').read_text(encoding='utf-8'))
    rows=result['results'];passed=[r for r in rows if r['research_screen_passed']]
    lines=['# 每月滚动训练与开仓误差余量研究','',
        '## 结论','',
        f"预先固定8组对照，{len(passed)}组达到本轮研究筛选要求。{'达到历史筛选要求仍需新前向检验。' if passed else '本轮仍未获得达到筛选要求的模型，不能将交易减少或某个局部盈利视为稳定盈利。'}",
        '评价区间统一为2025年10月1日至2026年9月24日，最后一个月不完整。研究使用此前已查看过的历史，属于历史滚动评估，不是新独立测试。没有根据本轮结果重新挑阈值、品种、月份或最优模型。','',
        '| 模型 | 持仓上限 | 成交 | 净收益元 | 收益率 | 净盈利胜率 | 盈利因子 | 最大回撤 | 信号覆盖率 | 盈利月份/12 | 达标 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        s=r['historical']
        lines.append(f"| {NAMES[r['arm']]} | {r['horizon']*15}分钟 | {s['trades']} | {s['net']:.2f} | {pct(s['net']/50000)} | {pct(s['win_rate'])} | {number(s['profit_factor'])} | {pct(s['max_drawdown'])} | {pct(s['coverage'])} | {r['positive_months']} | {'是' if r['research_screen_passed'] else '否'} |")
    lines += ['', '研究门槛在拟合前保存：至少100笔、净收益为正、盈利因子≥1.2、最大回撤≤10%、12个月中至少8个月盈利。这些是暂定研究限制，不能替代用户实际风险承受能力。零成交或少量偶然盈利不视为通过。', '',
        '![各组平仓后账户权益](资金曲线.png)', '',
        '图中为平仓后资金曲线，表中回撤还考虑持仓期间的不利波动。各策略均从5万元开始，跨月连续计算资金和手数，没有每月重置账户。','',
        '## 是否有改善','',
        '| 持仓 | 整体校准滚动相对冻结模型净收益差额 | 分组校准相对整体校准差额 | 加误差余量相对分组校准差额 |',
        '|---|---:|---:|---:|']
    for h in (1,2):
        d={r['arm']:r['historical']['net'] for r in rows if r['horizon']==h}
        lines.append(f"| {h*15}分钟 | {d['rolling_global']-d['static']:+.2f}元 | {d['rolling_bins']-d['rolling_global']:+.2f}元 | {d['rolling_margin']-d['rolling_bins']:+.2f}元 |")
    lines += ['', '差额用于描述完整策略的变化，包含选中交易、方向、手数和资金路径变化，不是固定交易的单因素因果效应。降低亏损不等于得到正收益。', '',
        '## 数学模型和时间边界','',
        '沿用上一轮37项已知特征及8个低成本品种：菜油、苹果、硅铁、苯乙烯、棕榈油、液化石油气、豆油、低硫燃料油。品种名单在本轮评价开始之前已冻结，没有根据后段盈亏重新筛选。',
        '每月使用校准月之前24个月的样本训练，最近1个月仅作校准，再预测下一个月。例如预测2025年10月时：训练2023年9月至2025年8月，校准2025年9月。之后逐月平移；日内未来30分钟标签在区间边界处清除，保证训练标签在校准开始前结束、校准标签在预测月开始前结束。',
        '原冻结模型仍使用原训练和校准信息，在同一评价区间回放。滚动组沿用同一梯度提升树参数与365日半衰期权重；每个持仓期分别训练做多、做空两个净R回归器。没有增加盈利概率50%门槛。',
        '设每手扣费净损益为Y，风险尺度R=止损距离×合约乘数＋10元手续费＋两端各1跳滑点。回归器估计 f_d(x)≈E[Y/R|x]。整体校准使用上一月残差的均值修正。',
        '分组校准按训练集预测值的50%、90%分位点形成三组；校准样本按其预测值进入对应组。残差e_i=Y_i/R_i−f_d(x_i)，组内修正b=Σe_i/N。少于100条或10个交易日时回退整体校准；整体样本仍不足时观望。这里按上海自然日聚合，不能完全消除跨日相关。',
        '对校准组内G个日期，令S_g=Σ(日期g内的e_i−b)，按日期聚合的标准误 SE=√[G/(G−1)×ΣS_g²]/N。分组校准得分=f_d(x)+b；误差余量组得分=f_d(x)+b−1.645×SE。得分≥0且成本/止损空间≤0.35才参与开仓，选择合格方向中得分最高者。',
        '误差余量衡量校准偏差估计的不稳定程度，不包含模型全部误差、未来市场变化或个体交易风险；1.645倍系数是预先固定的启发式，不能解读为真实95%盈利保证。模型同日预测和重叠标签具有依赖性，按日期聚合只能部分处理。',
        '信号仍由15分钟K线收盘产生，最长持有15/30分钟、不隔夜，1.5ATR止损、无固定止盈。手续费每手开平合计10元，入场和离场各1跳不利滑点。5万元共享账户、全市场最多1个持仓、最多10手、单笔预算min(900元,权益1.8%)、保证金15%假设均保持不变。', '',
        '## 跨月份稳定性','',
        '| 月份 | 冻结15 | 滚动整体15 | 滚动分组15 | 误差余量15 | 冻结30 | 滚动整体30 | 滚动分组30 | 误差余量30 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for i in range(12):lines.append('| '+rows[0]['months'][i]['month']+' | '+' | '.join(f"{r['months'][i]['net']:.0f}" for r in rows)+' |')
    lines += ['', '表中单位为元，完整月度成交量和胜率保存在results.json。', '',
        '## 固定账本成本拆分','',
        '| 模型/持仓 | 计入成本前元 | 手续费元 | 滑点元 | 净收益元 |','|---|---:|---:|---:|---:|']
    for r in rows:
        c=r['fixed_ledger_costs']
        lines.append(f"| {NAMES[r['arm']]}/{r['horizon']*15} | {c['gross']:.2f} | {c['fee']:.2f} | {c['slippage']:.2f} | {c['net']:.2f} |")
    lines += ['', '固定成交时点、退出决定和手数，只还原计入账本的两项成本；不等于重新执行零成本策略。', '',
        '## 成本压力','',
        '| 模型/持仓 | 费用/单边滑点 | 成交 | 净收益元 | 回撤 |','|---|---|---:|---:|---:|']
    for r in rows:
        for label,s in r['cost_stress'].items():lines.append(f"| {NAMES[r['arm']]}/{r['horizon']*15} | {label} | {s['trades']} | {s['net']:.2f} | {pct(s['max_drawdown'])} |")
    lines += ['', '成本压力固定模型预测与开仓门槛，重新计算执行和资金路径，交易数量可能变化。', '',
        '## 核验与复现','',f"程序核验：{'通过' if check['passed'] else '未通过'}。"]
    lines += ['- '+s for s in check['checks']]
    lines += ['', '数据仍为上一轮已核对的84,207条分钟路径机会，缓存位于.tqsdk/research/fine15m/model-data.npz；滚动模型和分月预测位于.tqsdk/research/rolling15m。24个分月模型文件包含48个方向回归器、校准统计、样本索引和预测。数据、规则、源代码和模型指纹均保存。',
        '复现顺序：python tools/research_rolling_15m.py fit → evaluate → verify（每步均保留前面的脚本路径），再运行python tools/report_rolling_15m.py。protocol.json已保存，declare仅用于首次生成规则，拒绝覆盖。已有分月缓存仅在代码、数据和规则指纹一致时复用。',
        '这是一轮固定流程的历史研究，不包含新前向行情，也不执行真实交易。分钟OHLC仍不能证明真实盘口成交；合约规格和保证金缺少逐日历史档案。这些限制延续上一轮。无论历史结果如何，接入软件之前仍需新的前向数据和更完整成交验证。']
    (OUT/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans'];plt.rcParams['axes.unicode_minus']=False
    fig,axes=plt.subplots(1,2,figsize=(14,5),sharey=True)
    for ax,h in zip(axes,(1,2)):
        for r in [q for q in rows if q['horizon']==h]:
            with (OUT/f"{r['arm']}-{h}-成交.csv").open(encoding='utf-8-sig',newline='') as f:ledger=list(csv.DictReader(f))
            times=[pd.Timestamp('2025-10-01',tz='Asia/Shanghai')]+[pd.Timestamp(int(a['exit_ms']),unit='ms',tz='UTC').tz_convert('Asia/Shanghai') for a in ledger]+[pd.Timestamp('2026-09-25',tz='Asia/Shanghai')]
            balances=[50000]+[float(a['balance']) for a in ledger]+[50000+r['historical']['net']]
            ax.step(times,balances,where='post',label=NAMES[r['arm']],linewidth=1.4)
        ax.set_title(f'{h*15}分钟持仓上限');ax.axhline(50000,color='gray',linestyle='--',linewidth=.7)
        ax.grid(alpha=.2);ax.tick_params(axis='x',rotation=25);ax.legend(fontsize=8)
    axes[0].set_ylabel('平仓后账户权益（元）');fig.tight_layout();fig.savefig(OUT/'资金曲线.png',dpi=160);plt.close(fig)
    print('Report and equity figure saved',flush=True)

if __name__=='__main__':main()

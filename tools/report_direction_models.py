"""Write the research report from frozen independent-test outputs."""
import csv
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def pct(value):return '—' if value is None else f'{value*100:.2f}%'


def main():
    folder=ROOT/'docs/研究/2026-09-26-全商品方向模型'
    d=json.loads((folder/'results.json').read_text(encoding='utf-8'))
    coverage=d['coverage']
    empty=[r for r in coverage if r['daily']==0]
    gaps=[r for r in coverage if r['intraday_prefix_gap']]
    with (folder/'合约覆盖.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(coverage[0]));writer.writeheader();writer.writerows(coverage)
    lines=['# 国内商品期货方向预测模型：研究结果','',
        '## 本次工作','',
        '先完成 rb2610 成交核查与成本拆解，再读取近五年国内商品期货数据并建立四个独立方向模型。模型仅用于研究，没有修改桌面/手机评估器，也没有发布新版。', '',
        f"研究日期：2021-09-26 至 2026-09-24。覆盖行情源可列出的 {d['contracts']} 个研究期内或较晚到期的合约、{d['products']} 个品种，含已到期合约，不包含期权、中金所金融期货和连续合约。", '',
        f"日线总数 {sum(r['daily'] for r in coverage):,}，15 分钟线总数 {sum(r['intraday'] for r in coverage):,}。未返回有效日线的合约 {len(empty)} 个；仍有 15 分钟前段截断疑点 {len(gaps)} 个。全部合约逐项见合约覆盖.csv，缺失不填造。行情源目录本身的历史完备性未获得独立证明，因此这里的全量范围是本数据源可枚举的范围。", '',
        '## 数学定义与输出','',
        r'目标收益为 $r_{t,h}=C_{t+h}/C_t-1$。上涨标记为 1、下跌为 0；价格不变单独计数，不参与二分类训练，在出手准确率中不算正确。日线 h 分别取官方交易日历后第 1、10、40 个交易日；该日期缺少本合约价格则不生成标签，不以更晚报价替代。15 分钟模型要求相邻柱的时间相差恰好 15 分钟，跨休市时间的样本不用于该标签。', '',
        '逻辑回归：p(t) = 1 / [1 + exp(−β₀ − Σ β(j)·z(t,j))]，其中 z(t,j) = [x(t,j) − μ(j)] / σ(j)，标准化参数只从训练集估计。梯度提升：p(t) = sigmoid[F₀ + η·Σ f(m,x(t))]，使用深度受限的决策树。', '',
        '当 p(t) ≥ θ 时输出上涨；否则，当 p(t) ≤ 1−θ 时输出下跌；其余观望。p 是统计模型分值，尚未验证实盘概率校准。', '',
        '特征是过去 1/4/16/48 柱收益、4/16/48 柱均线偏离、ATR14、16 柱波动率、相对成交量、4 柱持仓变化、K 线振幅/实体/跳空、16 柱价格位置、距合约到期天数及日内时间。全部只依赖当前已完成柱和此前数据；合约规格使用数据源元数据，未重建历史规格调整。', '',
        '## 时间隔离与抽样','',
        '- 训练：2021-09-26 至 2024-09-25；验证：2024-09-26 至 2025-09-25；独立测试：2025-09-26 至 2026-09-24。按上海时间切分，日线视为当日日盘收盘已知。',
        '- 剔除未来目标跨越训练/验证/测试边界、超过样本末尾或到期的样本。测试标签不用于训练或调参，未随机打散时间。',
        '- 当前柱成交量和持仓量均至少 100 手，历史至少满足特征窗口。该过滤只用当时已知信息，但模型不覆盖所有低流动性时刻。',
        '- 缓存包括额外的日线历史预热和日内窗口余量；用于特征的预热数据不生成研究起点之前的标签。日线预热最多读取起点前 120 个自然日，保证早期样本也有过去窗口。',
        '- 逐合约读取全部已获取历史并构造候选样本；每品种、每档、每时间分区采用固定随机种子 20260926 的均匀优先级抽样，上限为训练 2,000、验证 600、测试 1,000 条，避免月份多的品种主导模型和限制内存。输出统计对应这一品种均衡研究样本，不是全市场每根柱的精确统计。',
        '- 两种预先规定的模型；梯度提升 120 轮、深度 4、最多 15 叶、学习率 0.05、L2=5，不使用隐式随机验证提前停止。只在验证集比较阈值 0.50/0.55/0.60/0.65/0.70。',
        '- 验证覆盖率至少 20%；选择相对同一出手子集上“始终涨、始终跌、上一柱动量”最好基准的准确率增量最大者，平分时用平衡准确率。测试集只评价已冻结的选择。', '',
        '## 独立测试结果','',
        '| 预测时长 | 模型 | 阈值 | 测试样本 | 出手覆盖率 | 方向准确率 | 平衡准确率 | 最好匹配基准 | 超过成本比例 |','|---|---|---:|---:|---:|---:|---:|---:|---:|']
    names={'15m':'15 分钟','1d':'1 交易日','10d':'10 交易日','40d':'40 交易日'}
    for h,r in d['results'].items():
        if r['status']=='insufficient_samples':
            lines.append(f"| {names[h]} | 样本不足 | — | — | — | — | — | — | — |");continue
        s=r['test'];benchmark=max(s['always_up_matched'],s['always_down_matched'],s['momentum_matched']) if s['active'] else None
        lines.append(f"| {names[h]} | {r['chosen_model']} | {r['threshold']} | {s['samples']:,} | {pct(s['coverage'])} | {pct(s['accuracy'])} | {pct(s['balanced_accuracy'])} | {pct(benchmark)} | {pct(s['cost_effective_accuracy'])} |")
    lines+=['', '![准确率与出手覆盖率](模型比较.png)', '',
        '本次判断：15 分钟相对匹配基准有小幅样本增益；1 日几乎无改善；10 日未达到提高准确率的目标；40 日有选择性样本增益但覆盖率低、季度间变化大，不能确认稳定性。当前四档模型都保留为研究版本，不据此替换现有客户端。', '',
        f"15 分钟测试样本含 {d['results']['15m']['test']['flat_samples']:,} 条价格不变记录，预测上涨/下跌对这些记录计未命中；因此原始方向准确率与只针对涨跌的平衡准确率不同，比较时应使用相同样本的基准。", '',
        '研究训练样本满足窗口和流动性过滤，各档训练覆盖 66–67 个品种，独立测试覆盖 74 个品种；部分测试品种在训练期尚无合格样本，属于跨品种外推，其清单保存在 results.json。80 个品种有日线记录不等于每个品种都有完整五年历史或足够的模型训练样本。']
    lines+=['','方向准确率包含所有出手样本，平衡准确率对上涨/下跌分别计算召回后取平均。覆盖率的分母是经过特征、标签与流动性筛选并抽样后的可评估样本，不能理解为全市场覆盖率。做多/做空各自准确率、AUC、价格不变数量、每品种统计、候选样本数和完整验证比较见 results.json。', '',
        '“超过成本比例”仅表示所预测方向的收盘价收益大于统一研究假设的每手开平 10 元加双边各 1 跳滑点。各品种真实手续费不同，未重建经纪商历史费用。该比例不是成交回测胜率或策略净收益，没有模拟开盘成交、仓位、止损、保证金、涨跌停或组合资金。', '',
        '## 判断边界','']
    for h,r in d['results'].items():
        if r['status']=='insufficient_samples':continue
        s=r['test']; benchmark=max(s['always_up_matched'],s['always_down_matched'],s['momentum_matched']) if s['active'] else None
        delta=(s['accuracy']-benchmark)*100 if benchmark is not None else None
        interval=s['accuracy_quarter_block_interval']
        if delta is None or interval is None:
            lines.append(f"- {names[h]}：独立测试未出手，不能报告出手准确率或判断改善。")
            continue
        lines.append(f"- {names[h]}：相对最好匹配基准增量 {delta:.2f} 个百分点；季度分块准确率区间 {pct(interval[0])}–{pct(interval[1])}（仅 {s['quarter_blocks']} 个季度块，属于粗略描述，不能证明统计显著或稳定）。")
        gain=s.get('gain_quarter_block_interval')
        if gain:lines.append(f"  同一季度分块下，相对基准增量区间 {gain[0]*100:.2f} 至 {gain[1]*100:.2f} 个百分点。")
    lines+=['','近一年独立测试仍不足以证明跨市场周期可靠，40 交易日标签高度重叠。模型选择在验证集上也存在选择误差。若未超过基准，须如实承认，不能因模型更复杂就称为改善；即使准确率提高，也不能据此断言交易盈利。','',
        '## 文件与复现','',
        '`results.json`：数据和代码指纹、版本、覆盖清单、验证/测试统计；`合约覆盖.csv`：全部合约的有效柱数和缺口；`.tqsdk/research/models/*.joblib`：本地训练模型与阈值；同目录 `*-test.npz`：冻结测试特征、标签和模型分值。模型和原始数据保存在忽略目录，没有包含账号密码或令牌。', '',
        '```powershell','python -m pip install -r requirements-research.txt','python tools/research_universe.py',
        'python tools/research_history.py','python tools/research_fill_prefix.py','python tools/research_warmup.py','python tools/train_direction_models.py',
        'python tools/report_direction_models.py','```','',
        f"数据内容汇总 SHA256：`{d['data_sha256']}`。包版本：`{d['versions']}`。各模型文件指纹、逻辑回归参数（适用时）及训练代码指纹保存在 results.json。", '',
        '行情接口说明：[TqSdk 核心 API](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html)。近期线的条数上限不能等同于五年完整历史；前段截断通过获授权 TqBacktest 过去时点快照补齐，未使用账户未授权的 DataDownloader。']
    (folder/'研究报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    heads=[(h,r) for h,r in d['results'].items() if r['status']!='insufficient_samples']
    x=np.arange(len(heads));width=.36
    fig,axes=plt.subplots(1,2,figsize=(11,4.5),layout='constrained')
    accuracy=[r['test']['accuracy']*100 for h,r in heads]
    baseline=[max(r['test'][k] for k in ('always_up_matched','always_down_matched','momentum_matched'))*100 for h,r in heads]
    coverage=[r['test']['coverage']*100 for h,r in heads]
    for bars in (axes[0].bar(x-width/2,accuracy,width,label='研究模型'),axes[0].bar(x+width/2,baseline,width,label='最好匹配基准')):
        axes[0].bar_label(bars,fmt='%.2f',padding=3,fontsize=9)
    axes[0].set_title('方向准确率（包含价格不变的未命中）')
    axes[0].legend(loc='upper left')
    bars=axes[1].bar(x,coverage,.55,color='#419184')
    axes[1].bar_label(bars,fmt='%.2f',padding=3)
    axes[1].set_title('出手覆盖率（合格抽样样本内）')
    for ax in axes:
        ax.set_xticks(x,[names[h] for h,r in heads]);ax.set_ylim(0,110);ax.set_ylabel('%');ax.grid(axis='y',alpha=.2)
    fig.savefig(folder/'模型比较.png',dpi=180);plt.close(fig)
    print('research report saved')


if __name__=='__main__':main()

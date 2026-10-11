"""Report the direct evaluator study and its separate live observation status."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/研究/2026-09-27-决策准确度多品种'
LIVE=ROOT/'.tqsdk/research/decision_forward'
NAMES={'CZCE.AP':'苹果','CZCE.OI':'菜油','CZCE.SF':'硅铁','DCE.eb':'苯乙烯','DCE.p':'棕榈油','DCE.pg':'液化石油气','DCE.y':'豆油','INE.lu':'低硫燃料油'}


def pct(v):return '—' if v is None else f'{v:.2%}'


def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def snapshot():
    status=json.loads((LIVE/'status.json').read_text(encoding='utf-8'))
    with sqlite3.connect((LIVE/'events.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN')
        events=db.execute('SELECT * FROM events ORDER BY seq').fetchall()
        state={k:json.loads(v) for k,v in db.execute('SELECT key,payload FROM state')}
    previous='0'*64;errors=[];predictions={};commits={};outcomes=0
    for n,(seq,wall,kind,body,prev,digest) in enumerate(events,1):
        expected=hashlib.sha256(canonical([seq,wall,kind,body,prev]).encode()).hexdigest()
        if n!=seq or prev!=previous or digest!=expected:errors.append(f'chain {seq}')
        previous=digest;v=json.loads(body)
        if kind=='prediction':
            predictions[seq]=v;path=LIVE/v['input_file']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=v['input_sha256']:errors.append(f'input {seq}')
        if kind=='prediction_committed':
            ref=v['prediction_seq']
            if ref not in predictions or ref>=seq:errors.append(f'commit {seq}')
            commits[ref]=v['committed_ms']
        if kind=='outcome':
            ref=v['prediction_seq'];outcomes+=1
            if ref not in commits or not commits[ref]<v['due']<=v['quote_ms']<=v['due']+5000:errors.append(f'outcome chronology {seq}')
    manifest=json.loads((LIVE/'manifest.json').read_text(encoding='utf-8'))
    for p,h in manifest['source_sha256'].items():
        if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h:errors.append(f'changed source {p}')
    if hashlib.sha256((LIVE/'calibration.joblib').read_bytes()).hexdigest()!=manifest['model_sha256']:errors.append('changed model')
    value=dict(status=status,heartbeat_age_seconds=round(time.time()-status['wall_ms']/1000,1),
        journal_events=len(events),saved_predictions=len(predictions),accepted_predictions=len(commits),
        observed_endpoints=outcomes,integrity_errors=errors,ledger_stats=state.get('stats',{}),
        process_liveness='not checked by report',deployment=False)
    (OUT/'前向状态.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return value


def main():
    r=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    data=json.loads((OUT/'data-audit.json').read_text(encoding='utf-8'))
    live=snapshot()
    lines=['# 软件现有超短线决策：八品种准确度与改进对照', '',
        '## 本轮判断', '',
        '**原算法在这份多品种样本中没有明确的15分钟方向优势。重新校准原因子权重提高了方向准确率，但未证明盈利改善，因此暂不替换软件。**', '',
        '这次直接使用 `market.evaluator.evaluate(..., mode="ultra")`，输入1分钟执行、5分钟确认、15分钟趋势数据；每15分钟评价一次。不是调用之前的收益研究模型冒充软件决策，也不是验证默认短线的1–3天持有目标。', '',
        '## 数据和时间隔离', '',
        f"- 历史覆盖：{data['range'][0]}至{data['range'][1]}；八品种、183个历史合约。", 
        f"- 既有84,207次研究机会中，保留{data['kept']:,}次；7,771次因过去分钟路径不完整而排除，30次因未来路径存在零成交量而排除。", 
        '- 八品种沿用此前已选研究集合，不代表国内全部商品期货。既有机会集本身已经要求未来标签完整，因此本报告覆盖率是样本内出手比例，不是市场全部时点的覆盖率。',
        '- 使用原生15分钟历史，逐根核对过去67根15分钟K线与完整分钟路径的OHLCV一致；5分钟K线只由5根连续分钟线组成。输入固定为最近200根已完成1/5分钟K线及至多200根已完成15分钟K线。',
        '- 实际软件接口可能返回包含形成中K线的200根窗口，本研究固定完成线窗口；实时点击价格和延迟也无法完全还原。历史缺少交易时段元数据时保留软件警告，只核对方向，不评价能否实际开仓。',
        f"- 权重校准：2025-07-01之前{r['split']['train_nonflat']:,}条非平价样本训练；2025年7–9月{r['split']['validation']:,}条验证；2025-10-01至2026-09-24的{r['split']['test']:,}条后段评价。", 
        '- 唯一候选是固定C=1逻辑回归，输入原有四类因子的8项多空分值；只在验证段按原算法覆盖率确定观望阈值，不搜索后段最优参数。概率是排除平价结局条件下的上涨概率，不能直接当界面胜率。',
        '- 这些历史此前已被研究过，后段评价不是全新的独立测试。当前尚未覆盖用户最初要求的完整5年分钟输入。', '',
        '## 后段整体结果', '',
        '| 版本 | 期限 | 建议数 | 覆盖率 | 方向准确率 | 剔除平价后 | 超过成本比例 | 每次终点净变动/手 |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for arm,name in [('side','原算法'),('calibrated_side','原因子权重校准')]:
        for h,m in r['test'][arm].items():
            lines.append(f"| {name} | {h}分钟 | {m['signals']} | {pct(m['coverage'])} | {pct(m['accuracy'])} | {pct(m['nonflat_accuracy'])} | {pct(m['cost_hurdle_hit'])} | {m['mean_net_one_lot']:.2f}元 |")
    lines += ['', '方向判定以信号时已知收盘价到终点价的变化为准；价格不变不算答对，观望不算答对。手续费每手开平10元，滑点每边1跳。终点净变动未应用止损、持仓互斥、盘口价差或市场冲击，且样本重叠，因此不是实盘交易胜率或可累加的账户收益。不同品种每手金额和信号分布不同，也不能将汇总差额全归因于权重变化。', '',
              '15分钟方向准确率增加3.31个百分点，出手覆盖率下降1.84个百分点；但每次信号的一手终点净变动由−30.89元变为−35.79元，未满足“风险与亏损表现不恶化”的接入要求。', '',
              '### 不确定性与简单基准', '']
    for arm,name in [('side','原算法'),('calibrated_side','权重校准')]:
        m=r['test'][arm]['15'];lo,hi=m['day_bootstrap_ci']
        lines.append(f"- {name}15分钟准确率95%按自然日整块重采样区间：{pct(lo)}–{pct(hi)}；在该版本同样的信号时点，始终做多为{pct(m['always_long_same_signals'])}，始终做空为{pct(m['always_short_same_signals'])}。")
    lines += ['', '同一天内所有品种与重叠标签一起重采样，以保留日内和品种间依赖；这仍不消除跨日相关和历史反复使用的偏差。基准不是交易建议。校准结果接近简单方向基准，不能称为稳定优势。', '',
              '## 分品种：15分钟', '', '| 品种 | 原建议数 | 原覆盖率 | 原准确率 | 校准建议数 | 校准覆盖率 | 校准准确率 |', '|---|---:|---:|---:|---:|---:|---:|']
    for product,g in r['groups']['product'].items():
        a,b=g['side']['15'],g['calibrated_side']['15']
        lines.append(f"| {NAMES[product]} | {a['signals']} | {pct(a['coverage'])} | {pct(a['accuracy'])} | {b['signals']} | {pct(b['coverage'])} | {pct(b['accuracy'])} |")
    lines += ['', '苹果较好的后段结果是事后观察，不能据此只保留苹果、删除其他品种并宣称已得到可靠模型。', '',
              '## 分月份：15分钟', '', '| 月份 | 原准确率 | 校准准确率 | 原/校准建议数 |', '|---|---:|---:|---:|']
    for month,g in r['groups']['month'].items():
        a,b=g['side']['15'],g['calibrated_side']['15']
        lines.append(f"| {month} | {pct(a['accuracy'])} | {pct(b['accuracy'])} | {a['signals']}/{b['signals']} |")
    lines += ['', '12个月中10个月准确率高于原算法，但2026年5月和8月下降，且覆盖率与信号集合不同。不能把“多数月份提高”解释为不会增加亏损。', '',
              '## 原评估器的新行情直接对照', '',
              f"最近报告快照：状态 `{live['status']['state']}`，心跳距报告生成{live['heartbeat_age_seconds']}秒；已接受预测{live['accepted_predictions']}次、有效终点{live['observed_endpoints']}个。账本/冻结指纹检查：{'通过' if not live['integrity_errors'] else '失败'}。状态报告不独立证明进程存活。", '',
              '已增加独立观察程序：在同一合约、同一时点同时保存原评估器和固定权重候选的方向，预测先落盘，再观察15/30分钟后的新报价。包含观望记录；终点缺失或迟到标无效，重启/断线不回填历史结果；同一结果同时用于两组比较。没有下单或纸面仓位，也没有接入桌面/手机决策入口。', '',
              '监控合约沿用原后台采集器最新selected_symbols快照，记录其时间；可能滞后本次边界的持仓量排名。父采集器心跳超过30秒便不生成新预测。该观察程序使用201根请求取最近200根已完成K线；开始或换合约时预热不足则跳过。', '',
              '实时标签以预测时最新已知成交价为起点，以目标时刻之后5秒内首个被观察到的新报价为终点，和历史完成线收盘标签有时点差异。只评价终点方向，不声称路径可交易或盘口成交成功。本地哈希链不构成第三方时间公证。', '',
              '冻结文件见 `前向冻结方案.json`；动态快照见 `前向状态.json`。没有新行情时零预测、零结局是正常等待，不能用此前历史成绩填充前向成绩。', '',
              '### 运行方式', '', '在项目根目录PowerShell执行：', '', '```powershell',
              r'& .\tools\start_decision_accuracy.ps1',
              'python tools/forward_decision_accuracy.py status',
              'python tools/report_decision_multimarket.py',
              'python tools/forward_decision_accuracy.py stop', '```', '',
              '启动脚本同时确认依赖的原行情采集器已运行。停止本观察程序不会停止原采集器；若两者都要停止，再执行 `python tools/forward_research.py stop`。电脑需开机联网；未设置开机启动，重启后手动运行启动脚本。模型及数据在 `.tqsdk/research/decision_accuracy`，实时账本和输入在 `.tqsdk/research/decision_forward`；保留整个证据目录。', '',
              '## 本轮产物与下一步', '',
              '- 直接评估76,406个样本、分品种与分月统计、固定候选、源码/数据指纹均已保存；复现先运行 `audit_decision_multimarket.py build`，再运行 `analyze` 子命令。已有冻结版本变更时应新建实验，不覆盖当前证据。',
              '- 当前门槛未满足，软件方向算法保持原样。下一步用新的配对预测检验优势，并在训练/验证段研究如何区分可覆盖成本的走势；不能继续针对已看过的后段成绩挑参数或挑品种。', '']
    (OUT/'研究报告.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(dict(report=str(OUT/'研究报告.md'),live=live),ensure_ascii=False))


if __name__=='__main__':main()

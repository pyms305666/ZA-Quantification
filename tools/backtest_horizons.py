"""Run the four holding-horizon studies against a captured historical replay."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research.backtest import History, Settings, local_time, simulate
from market.decision_profiles import PROFILES


def timestamp(value):
    return local_time(value).isoformat() if value is not None else "无"


def save_csv(path, rows):
    if not rows:
        path.write_text("",encoding="utf-8-sig")
        return
    with path.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def table(results):
    lines=["| 模式 | 首次可评估 | 净收益（元） | 收益率 | 最大回撤 | 交易数 | 胜率 | 手续费（元） |",
           "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        s=r["stats"]
        win="—" if s["win_rate_percent"] is None else f'{s["win_rate_percent"]:.2f}%'
        lines.append(f'| {r["label"]} | {timestamp(r["first_eligible"])[:10]} | {s["net_profit"]:,.2f} | '
                     f'{s["return_percent"]:.2f}% | {s["max_drawdown_percent"]:.2f}% | {s["trades"]} | {win} | {s["total_fees"]:,.2f} |')
    return "\n".join(lines)


def plot(results, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"]=["Microsoft YaHei","SimHei","DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"]=False
    fig,ax=plt.subplots(figsize=(11,5.5),layout="constrained")
    for r in results:
        points=r["equity"]
        ax.plot([local_time(r["observed_start"])]+[local_time(p["timestamp"]) for p in points],
                [r["settings"]["initial_balance"]]+[p["equity"] for p in points],label=r["label"],linewidth=1.5)
    ax.axhline(50000,color="grey",linestyle="--",linewidth=.8)
    ax.set(title=title,ylabel="账户权益（元，已扣实际发生手续费）",xlabel="日期")
    ax.grid(alpha=.2)
    ax.legend()
    fig.savefig(path,dpi=180)
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data",type=Path,default=ROOT/".tqsdk/backtests/rb2610-20250926-20260924.json.gz")
    parser.add_argument("--output",type=Path,default=ROOT/"docs/回测/2026-09-26-rb2610")
    args=parser.parse_args()
    with gzip.open(args.data,"rt",encoding="utf-8") as stream: payload=json.load(stream)
    history=History(payload)
    settings=Settings()
    args.output.mkdir(parents=True,exist_ok=True)
    source_commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    digest=hashlib.sha256(args.data.read_bytes()).hexdigest()
    last_progress=[0]
    def progress(mode,done,total):
        if time.monotonic()-last_progress[0]>=20:
            print(mode,done,"/",total,flush=True)
            last_progress[0]=time.monotonic()
    results=[]
    for mode in PROFILES:
        r=simulate(history,mode,settings,progress=progress)
        results.append(r)
        print("baseline",mode,r["stats"],"first eligible",timestamp(r["first_eligible"]),flush=True)
    valid=[r["first_eligible"] for r in results if r["first_eligible"]]
    common_date=max(local_time(t).date() for t in valid) if len(valid)==len(PROFILES) else None
    common_start=next((d for d in history.days if common_date and d>common_date),None)
    shared=[]
    if common_start:
        for mode in PROFILES:
            r=simulate(history,mode,settings,start_date=common_start,progress=progress)
            shared.append(r)
            print("common",mode,r["stats"],flush=True)
    audit=dict(dataset_sha256=digest,source=payload["source"],sdk_version=payload["sdk_version"],
        evaluator_git_commit=source_commit,instrument=history.meta,requested_start=payload["requested_start"],
        requested_end=payload["requested_end"],captured_at=payload["captured_at"],
        coverage={str(p):dict(count=len(rows),first=timestamp(rows[0]["datetime"]),last=timestamp(rows[-1]["datetime"]),
                   zero_volume=sum(b["volume"]==0 for b in rows)) for p,rows in history.bars.items()},
        common_start=str(common_start) if common_start else None,settings=results[0]["settings"])
    compact={"audit":audit,"baseline":[],"common":[]}
    for name,runs in (("baseline",results),("common",shared)):
        for r in runs:
            compact[name].append({k:v for k,v in r.items() if k not in ("trades","equity")})
            trades=[dict(t,signal_time=timestamp(t["signal_time"]),entry_time=timestamp(t["entry_time"]),
                         exit_time=timestamp(t["exit_time"])) for t in r["trades"]]
            save_csv(args.output/f'{name}-{r["mode"]}-trades.csv',trades)
            save_csv(args.output/f'{name}-{r["mode"]}-equity.csv',r["equity"])
    (args.output/"results.json").write_text(json.dumps(compact,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    plot(results,args.output/"equity-baseline.png","SHFE.rb2610：完整可用历史，各档预热时间不同")
    if shared: plot(shared,args.output/"equity-common.png",f"SHFE.rb2610：共同起点 {common_start}，每档资金 5 万元")
    all_loss=all(r["stats"]["net_profit"]<0 for r in results+shared)
    findings=("完整历史与共同区间的四档结果均为亏损，当前评分加这组成交规则未显示正收益。" if all_loss
              else "收益情况见下表，需结合样本长度、成本和回撤理解。")
    findings+="超短线曲线后段持平不表示风险消失：亏损降低权益后，风险预算或一手保证金约束阻止继续开仓；各档跳过原因见 results.json。长线交易次数很少，不能以亏损较小推断其稳定有效。"
    report=f"""# rb2610 四档策略收益回测

## 范围与结论

请求范围：{payload['requested_start']} 至 {payload['requested_end']}；实际分钟线覆盖：{timestamp(history.bars[60][0]['datetime'])} 至 {timestamp(history.bars[60][-1]['datetime']+60000)}，首个交易日 {history.days[0]}。

本次运行是对 V1.2.1 评估器技术方向与止损信号的历史成交建模。原系统没有完整自动交易规则，以下规则按用户确认补齐。它不是实盘撮合验证，也没有修改、重新打包或发布桌面/手机软件。

{findings}

### 完整可用历史（各档独立预热）

{table(results)}

![完整历史权益曲线](equity-baseline.png)

### 共同可交易区间

所有档位完成指标预热后的下一个交易日为 {common_start}，分别重新投入 50,000 元，不继承此前持仓和权益。

{table(shared) if shared else '没有可供四档共同回测的区间。'}

![共同区间权益曲线](equity-common.png)

中长线需要至少 35 个完整周线，且剔除首个可能不完整的历史周。因此完整历史的收益不能直接作为四档排名；共同区间也仅约三个月，尤其长线交易数少，无法证明长期收益稳定性。本次单合约、单区间结果不能外推至其他合约或未来收益，未做参数择优。

## 交易规则

- 每档初始权益 50,000 元；单笔预计最大损失取 900 元和当时已实现账户权益的 1.8% 的较小值；最多 10 手。只有平仓后才可开下一笔，不叠加持仓。
- 手续费按用户指定：每手开平合计 10 元（开 5、平 5）；开平各加 1 跳不利滑点。该金额是研究参数，不声称是交易所实际收费。
- 手数按实际模型成交价到止损价的损失、预期止损滑点和双边手续费反推，再受保证金约束；一手超预算则跳过。跳空仍可能使实际止损超过预算，结果 JSON 单列计数。
- 保证金固定假设 15%，以成交价格乘 10 吨/手的名义金额计算；未重建历史交易所分阶段调整、经纪商加收或涨跌停，未计保证金资金利息。分钟收盘权益不足模型保证金时，下一分钟开盘模拟强平。
- 在执行周期结束后只使用已经完成的 K 线计算技术方向与止损；下一根执行 K 线的首分钟开盘成交，目标价取实际成交价到原止损距离的 3 倍；全仓平仓，不在 1.5R 分批退出。
- 执行周期：超短 1 分钟、短线 15 分钟、中线 60 分钟、长线日线；确认/趋势周期沿用 V1.2.1。日线严格保留原评估器规则：当天日线即使日盘收盘仍不作为已完成数据。因此长线收盘信号使用此前已完成日线，加当时收盘报价，下一交易日首个可用分钟成交。
- 回测使用评分方向和止损，自行计算含费用/保证金的手数；未照搬界面在非交易时段置空的建议手数。收盘边界生成的技术信号允许排队至下一执行 K 线。因此结果是所确认成交模型的收益，不能直接称为界面信号原样成交收益。
- 超短线最多持有 30 分钟，并在各交易时段结束时平仓，不跨夜；距离当前时段结束不足 5 分钟不新开仓。5 分钟是新仓时间余量，不阻止更早止损或止盈。这比界面“临近收盘仍给方向并提示”的开仓展示增加了成交约束。
- 短线最多持有 3 个交易日（入场日计第 1 日），第 3 日最后一分钟收盘退出；中线入场后 28 个自然日；长线入场后 3 个自然月，月末按目标月最后一天处理；期限后首个分钟开盘退出。持仓下限不阻止止损或止盈。
- 同一分钟同时触及止损和目标，保守按止损先发生；止损跳空用更差开盘价加滑点；目标有利跳空仍按目标价扣滑点；到期前 1 天不新开仓，样本最后一分钟强制平仓。

## 数据与计算限制

- 数据来自账户获授权的官方 TqSdk TqBacktest 历史回放；仅 TqSim，无真实账户订单。原始缓存保存在项目忽略目录 `.tqsdk/backtests/`，不提交账号、密码或令牌。
- 请求开始日早于该合约实际可用历史，未用主连、其他月份或人工填充补齐缺口。交易日由日线标签对齐，夜盘归入下一可用交易日。
- 分钟/5 分/15 分/60 分/日线数量：{', '.join(str(len(history.bars[p])) for p in (60,300,900,3600,86400))}。分钟零成交量柱：{sum(b['volume']==0 for b in history.bars[60])}；新仓首分钟成交量为零则该笔未成交。其余成交采用 OHLC 模型，不模拟盘口、成交量冲击、排队、部分成交或涨跌停无法成交；远月稀疏行情可能使结果偏离可实现收益。
- 指标数据按时间切片，未来 OHLC 不输入当前信号。交易量用于首分钟是否成交这一执行假设，而不是预测信号；最高/最低仅用于当前持仓退出。用于判定周期结束的下一根时间戳不参与价格预测。
- 权益为分钟收盘盯市，图表/CSV 为交易日最后一分钟采样；最大回撤由分钟收盘权益计算，未计算分钟内最差路径。未平仓时仅扣已发生开仓费用；样本末尾全部平仓。夏普采用日收益、252 日和零无风险利率。
- 手续费已计入净收益；结果另列模型滑点成本，仅用于解释成本来源，不能再次从净收益扣除。盈利因子按净盈利/净亏损绝对值，无亏损时为 null。

## 复现与交付文件

评估器基准 Git 提交：`{source_commit}`；TqSdk {payload['sdk_version']}；历史缓存 SHA256：`{digest}`。

```powershell
python -m pip install -r requirements-research.txt
python -m unittest tests.test_backtest -v
python tools/replay_backtest_data.py --symbol SHFE.rb2610 --start 2025-09-26 --end 2026-09-24
python tools/backtest_horizons.py
```

重新下载需要当前天勤账号具有历史回测权限；复用已有缓存计算不连接行情或交易账户。`results.json` 保存参数、覆盖、首次可评估时间、跳过原因和统计；`baseline-*-trades.csv` / `common-*-trades.csv` 是含时区逐笔交易；对应 `*-equity.csv` 是日末权益；两张 PNG 为独立可导出的权益图。

测试覆盖手续费、风险和保证金限额、下一周期入场、已完成数据过滤、同柱双触发、跳空成交、短线跨周末计时、超短时段平仓、自然月边界，以及修改未来价格不影响此前交易。

数据接口说明：[TqSdk 历史数据](https://doc.shinnytech.com/tqsdk/latest/usage/mddatas.html)、[官方回测快速入门](https://doc.shinnytech.com/tqsdk/latest/quickstart.html)。
"""
    (args.output/"回测报告.md").write_text(report,encoding="utf-8")
    print("saved",args.output,flush=True)


if __name__=="__main__": main()

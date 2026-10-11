"""Post-result descriptive diagnostics; never select or retrain a policy here."""
import csv
import json
from collections import defaultdict
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from research.minute_intraday import replay
from tools.research_15m_profit import save_json,summary
from tools.research_minute_15m import CACHE,OUT,ROOT,load_parts


def main():
    data=load_parts()[-1];paired=[];breakdown={}
    symbols=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    lookup={(int(row[0]),symbols[int(row[1])]):i for i,row in enumerate(data[1])}
    for horizon in (1,2):
        for name in ('baseline','minute'):
            saved=np.load(CACHE/f'{name}-{horizon}-test.npz')
            s=summary(replay(*data,saved['mean'],saved['prob'],horizon,0,0))
            paired.append(dict(model=name,horizon=horizon,edge=0,floor=0,historical=s))
    for name in ('baseline','minute'):
        with (OUT/f'{name}-成交.csv').open(encoding='utf-8-sig',newline='') as f:
            rows=list(csv.DictReader(f))
        groups=defaultdict(list)
        for row in rows:
            month=pd.Timestamp(int(row['entry_ms']),unit='ms',tz='UTC').tz_convert('Asia/Shanghai').strftime('%Y-%m')
            groups['month',month].append(float(row['net']))
            groups['product',row['product']].append(float(row['net']))
            groups['direction',row['direction']].append(float(row['net']))
        pnl=np.array([float(row['net']) for row in rows]);win=pnl[pnl>0];loss=pnl[pnl<0]
        fees=0.;slippage=0.;zero_volume=0
        for row in rows:
            i=lookup[int(row['entry_ms']),row['symbol']];lots=int(row['lots'])
            fees+=10*lots;slippage+=2*data[1][i,4]*data[1][i,5]*lots
            zero_volume+=int(data[3][i,0,4]<=0)
        for kind in ('month','product','direction'):
            assert abs(sum(sum(v) for (k,_),v in groups.items() if k==kind)-pnl.sum())<1e-7
        breakdown[name]=dict(trades=len(rows),average_win=float(win.mean()) if len(win) else None,
            average_loss=float(-loss.mean()) if len(loss) else None,
            zero_volume_entry_trades=zero_volume,
            fixed_ledger_costs=dict(net=float(pnl.sum()),fees=fees,slippage=slippage,
                gross_before_accounted_costs=float(pnl.sum()+fees+slippage),
                caveat='Fixed trade times, stop decisions and lots; not a new zero-cost execution replay.'),
            groups=[dict(kind=k,label=label,trades=len(v),net=sum(v),win_rate=float(np.mean(np.array(v)>0)))
                for (k,label),v in sorted(groups.items())])
    save_json(OUT/'post-result-diagnostics.json',dict(
        post_result_descriptive_only=True,no_new_selection=True,
        same_settings_comparison=paired,selected_policy_breakdown=breakdown))
    print(json.dumps(dict(paired=paired,breakdown=breakdown),ensure_ascii=False),flush=True)


if __name__=='__main__':main()

"""Capture permitted 1-minute snapshots for the frozen low-cost universe."""
from collections import defaultdict
import argparse
from datetime import datetime,timedelta
import gzip
import gc
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from config import load_config
from tqsdk import TqApi,TqAuth,TqSim,TqBacktest

FOLDER=ROOT/'.tqsdk/research/fine15m/minutes'


def plan():
    d=np.load(ROOT/'.tqsdk/research/conditional15m_low_cost/data.npz');m=d['m']
    symbols=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    month=pd.to_datetime(m[:,0],unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m')
    groups=defaultdict(list);history_times={}
    for label in sorted(set(month)):
        y,mo=map(int,label.split('-'));cutoff=datetime(y+int(mo==12),mo%12+1,1)
        cutoff=min(cutoff,datetime(2026,9,25))
        for si in np.unique(m[month==label,1]).astype(int):
            selected=m[(month==label)&(m[:,1]==si)]
            symbol=symbols[si]
            if symbol not in history_times:
                rows=json.loads(gzip.decompress((ROOT/f'.tqsdk/research/history/{symbol}.json.gz').read_bytes()))['bars']['900']
                history_times[symbol]=np.array(rows)[:,0]
            t=history_times[symbol];begin=int(selected[:,0].min()-900000)
            count=int(((t>=begin)&(t<int(cutoff.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000))).sum())
            groups[cutoff].append(dict(symbol=symbols[si],month=label,
                begin=begin,end=int(selected[:,0].max()+1800000),length=min(10000,max(200,count*15+60))))
    return groups


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--shards',type=int,default=1);parser.add_argument('--shard',type=int,default=0)
    parser.add_argument('--batch',type=int,default=6);parser.add_argument('--limit-months',type=int)
    args=parser.parse_args()
    if not 0<=args.shard<args.shards:raise ValueError('invalid shard')
    FOLDER.mkdir(parents=True,exist_ok=True);c=load_config().tqsdk
    groups=plan();done=0;total=sum(map(len,groups.values()));audit=[]
    selected_groups=sorted(groups.items())[args.shard::args.shards]
    if args.limit_months:selected_groups=selected_groups[:args.limit_months]
    for cutoff,items in selected_groups:
        pending=[a for a in items if not (FOLDER/f"{a['symbol']}-{a['month']}.json.gz").exists()]
        if not pending:done+=len(items);continue
        for offset in range(0,len(pending),args.batch):
            subset=pending[offset:offset+args.batch];frames={}
            print('request',args.shard,cutoff.isoformat(),[a['symbol'] for a in subset],flush=True)
            api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),
                backtest=TqBacktest(start_dt=cutoff,end_dt=cutoff+timedelta(days=21)),debug=False,disable_print=True)
            try:
                async def subscribe():
                    for a in subset:frames[a['symbol']]=api.get_kline_serial(a['symbol'],60,data_length=a['length'])
                task=api.create_task(subscribe());deadline=time.time()+180;last=time.time()
                while not task.done() or not all(api.is_serial_ready(f) for f in frames.values()):
                    api.wait_update(deadline=min(deadline,time.time()+5))
                    if time.time()-last>=20:
                        print('ready',sum(api.is_serial_ready(f) for f in frames.values()),'/',len(frames),flush=True);last=time.time()
                    if time.time()>=deadline:break
                task.result()
                for a in subset:
                    if not api.is_serial_ready(frames[a['symbol']]):
                        print('not ready; retained for retry',a['symbol'],flush=True);continue
                    f=frames[a['symbol']];fields=['datetime','open','high','low','close','volume','close_oi']
                    raw=f[fields].to_numpy();raw=raw[np.isfinite(raw).all(axis=1)&(raw[:,0]>0)]
                    raw[:,0]=np.floor(raw[:,0]/1e6)
                    valid=raw[(raw[:,0]>=a['begin'])&(raw[:,0]+60000<=a['end'])]
                    coverage=dict(raw_count=len(raw),raw_first=int(raw[0,0]) if len(raw) else None,
                        selected_count=len(valid),possible_prefix_gap=bool(len(raw)==a['length'] and raw[0,0]>a['begin']))
                    payload=dict(**a,source='public TqBacktest get_kline_serial 60',snapshot=str(cutoff),coverage=coverage,
                        columns=['time_ms','open','high','low','close','volume','oi'],bars=valid.tolist())
                    path=FOLDER/f"{a['symbol']}-{a['month']}.json.gz";tmp=path.with_suffix('.tmp')
                    with gzip.open(tmp,'wt',encoding='utf-8') as stream:json.dump(payload,stream,allow_nan=False)
                    tmp.replace(path);audit.append(dict(**a,**coverage));done+=1
                print('minute shard',args.shard,'snapshots',done,'cutoff',cutoff.isoformat(),flush=True)
            finally:
                api.close()
                frames.clear()
                task=None;f=None
                del api
                gc.collect()
    (FOLDER.parent/f'minute-download-run-{args.shard}.json').write_text(json.dumps(dict(total=total,new_snapshots=audit),indent=2),encoding='utf-8')
    remaining=sum(not (FOLDER/f"{a['symbol']}-{a['month']}.json.gz").exists() for items in groups.values() for a in items)
    print('minute shard pass complete; global cached',total-remaining,'remaining',remaining,flush=True)


if __name__=='__main__':main()

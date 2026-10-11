"""Read entitled daily and 15-minute history through public TqSdk APIs.

No DataDownloader or broker account. Batch closure bounds subscription memory;
per-contract files permit resuming without downloading completed contracts.
"""
import argparse
from datetime import datetime
import gzip
import json
import math
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from market.decision_profiles import CN


def main():
    from config import load_config
    from tqsdk import TqApi,TqAuth,TqSim
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--batch',type=int,default=64)
    parser.add_argument('--shards',type=int,default=1)
    parser.add_argument('--shard',type=int,default=0)
    args=parser.parse_args()
    folder=ROOT/'.tqsdk/research'
    with gzip.open(folder/'universe.json.gz','rt',encoding='utf-8') as f: universe=json.load(f)
    start=int(datetime.fromisoformat(universe['start']).replace(tzinfo=CN).timestamp()*1000)
    end=int(datetime.fromisoformat(universe['end']).replace(tzinfo=CN).timestamp()*1000)+86400000
    contracts=universe['contracts']
    if not 0<=args.shard<args.shards:raise ValueError('invalid shard index')
    contracts=contracts[args.shard::args.shards]
    if args.limit:contracts=contracts[:args.limit]
    cache=folder/'history';cache.mkdir(parents=True,exist_ok=True)
    todo=[c for c in contracts if not (cache/(c['instrument_id']+'.json.gz')).exists()]
    credentials=load_config().tqsdk
    for offset in range(0,len(todo),args.batch):
        batch=todo[offset:offset+args.batch]
        api=TqApi(TqSim(),auth=TqAuth(credentials.account,credentials.password),debug=False,disable_print=True)
        frames={}
        try:
            async def subscribe():
                for c in batch:
                    symbol=c['instrument_id']
                    frames[symbol]={86400:api.get_kline_serial(symbol,86400,data_length=1500),
                                    900:api.get_kline_serial(symbol,900,data_length=8000)}
            task=api.create_task(subscribe())
            deadline=time.time()+120
            while not task.done() or not all(api.is_serial_ready(f) for periods in frames.values() for f in periods.values()):
                if not api.wait_update(deadline=deadline):
                    waiting=[s for s,periods in frames.items() if not all(api.is_serial_ready(f) for f in periods.values())]
                    print('read timeout; retaining ready contracts, pending',waiting,flush=True)
                    break
            task.result()
            for c in batch:
                symbol=c['instrument_id']; data={}; audit={}
                if not all(api.is_serial_ready(f) for f in frames[symbol].values()):continue
                for period,frame in frames[symbol].items():
                    rows=[];raw_dates=[]
                    for b in frame.to_dict('records'):
                        keys=('datetime','open','high','low','close','volume','close_oi')
                        if not all(math.isfinite(float(b[k])) for k in keys):continue
                        timestamp=int(b['datetime'])//1000000
                        if timestamp<=0:continue
                        raw_dates.append(timestamp)
                        # Daily labels are calendar trading dates. Fifteen-minute
                        # rows include the previous evening for the first date.
                        lower=start if period==86400 else start-3*86400000
                        upper=end if period==86400 else end-9*3600000
                        if lower<=timestamp<upper:
                            rows.append([timestamp]+[float(b[k]) for k in keys[1:]])
                    data[str(period)]=rows
                    audit[str(period)]=dict(raw_count=len(raw_dates),raw_first=min(raw_dates) if raw_dates else None,
                        selected_count=len(rows),limit=1500 if period==86400 else 8000,
                        hit_limit=len(raw_dates)==(1500 if period==86400 else 8000))
                payload=dict(meta=c,start=universe['start'],end=universe['end'],columns=['datetime','open','high','low','close','volume','open_interest'],
                    source='public TqSdk get_kline_serial',coverage=audit,bars=data)
                temporary=cache/(symbol+'.json.gz.tmp')
                with gzip.open(temporary,'wt',encoding='utf-8') as f:json.dump(payload,f,ensure_ascii=False,allow_nan=False)
                temporary.replace(cache/(symbol+'.json.gz'))
            print('history shard',args.shard,offset+len(batch),'/',len(todo),'batch complete',flush=True)
        finally:api.close()
    print('history pass complete; coverage gaps require separate replay, never silent substitution',flush=True)
    missing=[c['instrument_id'] for c in contracts if not (cache/(c['instrument_id']+'.json.gz')).exists()]
    if missing:raise RuntimeError('contracts pending retry: '+str(missing))


if __name__=='__main__':main()

"""Actual historical L1 checks for every gated candidate trade; no live orders."""
import csv
from datetime import datetime,timedelta
import gc
import gzip
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from config import load_config
from tqsdk import TqApi,TqAuth,TqSim,TqBacktest
from tools.research_gated_15m import OUT,CACHE
from tools.research_rolling_15m import load,digest
from tools.research_15m_profit import save_json

def main():
    target=CACHE/'ticks';target.mkdir(exist_ok=True)
    ledgers={arm:list(csv.DictReader((OUT/f'{arm}-成交.csv').open(encoding='utf-8-sig'))) for arm in ['frozen_anchor','conditional','agreement']}
    episodes=sorted({(int(r['entry_ms']),r['symbol']) for rows in ledgers.values() for r in rows})
    frames={};config=load_config().tqsdk
    for j,(entry,symbol) in enumerate(episodes):
        path=target/f'{symbol}-{entry}.json.gz'
        if not path.exists():
            cutoff=datetime.fromtimestamp((entry+1800000)/1000,ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
            api=TqApi(TqSim(),auth=TqAuth(config.account,config.password),
                backtest=TqBacktest(start_dt=cutoff,end_dt=cutoff+timedelta(days=21)),debug=False,disable_print=True)
            box={};task=None
            try:
                async def subscribe():box['frame']=api.get_tick_serial(symbol,data_length=10000)
                task=api.create_task(subscribe());deadline=time.time()+150
                while not task.done() or 'frame' not in box or not api.is_serial_ready(box['frame']):
                    api.wait_update(deadline=min(deadline,time.time()+5))
                    if time.time()>=deadline:raise TimeoutError('Tick subscription timed out')
                task.result();columns=['datetime','last_price','bid_price1','bid_volume1','ask_price1','ask_volume1','volume','amount','open_interest']
                raw=box['frame'][columns].to_numpy();raw=raw[np.isfinite(raw).all(axis=1)&(raw[:,0]>0)]
                raw[:,0]/=1e6;raw=raw[(raw[:,0]>=entry-60000)&(raw[:,0]<=entry+1800000)]
                with gzip.open(path,'wt',encoding='utf-8') as f:json.dump(dict(source='actual get_tick_serial',columns=columns,rows=raw.tolist()),f,allow_nan=False)
            finally:
                api.close();box.clear();task=None;del api;gc.collect()
        raw=np.array(json.loads(gzip.decompress(path.read_bytes()))['rows'])
        if len(raw):raw=raw[(raw[:,2]>0)&(raw[:,4]>=raw[:,2])&(raw[:,3]>0)&(raw[:,5]>0)]
        frames[entry,symbol]=raw
        print('L1 snapshot',j+1,'/',len(episodes),symbol,flush=True)
    x,m,_,_=load();names=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    lookup={(int(row[0]),names[int(row[1])]):i for i,row in enumerate(m)};results={}
    for arm,ledger in ledgers.items():
        records=[]
        for r in ledger:
            entry=int(r['entry_ms']);symbol=r['symbol'];side=int(r['direction']);lots=int(r['lots']);raw=frames[entry,symbol]
            rec=dict(symbol=symbol,entry_ms=entry,original_net=float(r['net']),direction=side,lots=lots,usable=False)
            if not len(raw):rec['reason']='no valid displayed quotes';records.append(rec);continue
            a=np.searchsorted(raw[:,0],entry);b=np.searchsorted(raw[:,0],entry+900000)
            if a>=len(raw) or b>=len(raw) or raw[a,0]-entry>5000 or raw[b,0]-entry-900000>5000:
                rec['reason']='entry or timed exit quote missing within 5 seconds';records.append(rec);continue
            i=lookup[entry,symbol];tick=m[i,5];multiple=m[i,4];distance=max(tick,np.ceil(1.5*x[i,7]*m[i,3]/tick)*tick)
            variants=[]
            for extra in (0,1):
                opened=raw[a,4 if side==1 else 2]+side*extra*tick;stop=opened-side*distance
                executable=raw[a:b+1,2 if side==1 else 4]
                hit=np.flatnonzero(executable<=stop if side==1 else executable>=stop);end=a+int(hit[0]) if len(hit) else b
                exit_price=raw[end,2 if side==1 else 4]-side*extra*tick
                depth=min(raw[a,5 if side==1 else 3],raw[end,3 if side==1 else 5])
                variants.append(dict(extra_ticks=extra,net=float((side*(exit_price-opened)*multiple-10)*lots),
                    entry_delay_ms=float(raw[a,0]-entry),exit_ms=float(raw[end,0]),enough_displayed_depth=bool(depth>=lots),
                    max_quote_gap_ms=float(np.diff(raw[a:end+1,0]).max()) if end>a else 0))
            rec.update(usable=True,variants=variants);records.append(rec)
        usable=[r for r in records if r['usable']]
        results[arm]=dict(total=len(records),usable=len(usable),original_net=sum(r['original_net'] for r in records),
            matched_original_net=sum(r['original_net'] for r in usable),
            displayed_quote_net=sum(r['variants'][0]['net'] for r in usable),
            quote_plus_tick_net=sum(r['variants'][1]['net'] for r in usable),
            insufficient_displayed_depth=sum(not r['variants'][1]['enough_displayed_depth'] for r in usable),
            episodes_with_quote_gap_over_5s=sum(r['variants'][1]['max_quote_gap_ms']>5000 for r in usable),records=records)
    save_json(OUT/'tick-audit.json',dict(selection='all trades from all 3 candidate ledgers, no PnL selection',
        unique_episodes=len(episodes),results=results,
        code_sha256=digest(Path(__file__)),
        tick_sha256={p.name:digest(p) for p in sorted(target.glob('*.json.gz'))},
        ledger_sha256={arm:digest(OUT/f'{arm}-成交.csv') for arm in ledgers},
        limitation='Historical diagnosis, not fresh forward evidence. Fixed original lots and entries; quote stops can alter exit timing; no portfolio rescheduling, queue or latency guarantees.'))
    print(json.dumps({k:{a:b for a,b in v.items() if a!='records'} for k,v in results.items()},ensure_ascii=False),flush=True)

if __name__=='__main__':main()

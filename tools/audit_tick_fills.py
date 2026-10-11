"""Inspect first five frozen historical trades using actual L1 tick serials."""
import csv
from datetime import datetime,timedelta
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

CACHE=ROOT/'.tqsdk/research/fine15m/tick_audit'
OUT=ROOT/'docs/研究/2026-09-26-分钟路径模型'


def main():
    CACHE.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader((ROOT/'docs/研究/2026-09-26-条件净收益模型-低成本品种/成交明细.csv').open(encoding='utf-8-sig')))[:5]
    data=np.load(ROOT/'.tqsdk/research/conditional15m_low_cost/data.npz');symbols=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    c=load_config().tqsdk;results=[]
    for record in rows:
        entry_ms=int(record['entry_ms']);symbol=record['symbol'];path=CACHE/f'{symbol}-{entry_ms}.json.gz'
        if not path.exists():
            cutoff=datetime.fromtimestamp((entry_ms+1800000)/1000,ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
            api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),backtest=TqBacktest(start_dt=cutoff,end_dt=cutoff+timedelta(days=3)),debug=False,disable_print=True)
            try:
                box={}
                async def subscribe():box['frame']=api.get_tick_serial(symbol,data_length=10000)
                task=api.create_task(subscribe());deadline=time.time()+120
                while not task.done() or 'frame' not in box or not api.is_serial_ready(box['frame']):
                    api.wait_update(deadline=min(deadline,time.time()+5))
                    if time.time()>=deadline:raise TimeoutError('historical tick readiness')
                task.result();columns=['datetime','last_price','bid_price1','bid_volume1','ask_price1','ask_volume1','volume','amount','open_interest']
                raw=box['frame'][columns].to_numpy();raw=raw[np.isfinite(raw).all(axis=1)&(raw[:,0]>0)]
                raw[:,0]/=1e6
                raw=raw[(raw[:,0]>=entry_ms-900000)&(raw[:,0]<=entry_ms+1800000)]
                with gzip.open(path,'wt',encoding='utf-8') as f:json.dump(dict(symbol=symbol,entry_ms=entry_ms,columns=columns,rows=raw.tolist()),f)
            finally:api.close()
        raw=np.array(json.loads(gzip.decompress(path.read_bytes()))['rows'])
        side=int(record['direction']);lots=int(record['lots']);si=symbols.index(symbol)
        i=np.flatnonzero((data['m'][:,0]==entry_ms)&(data['m'][:,1]==si))[0]
        m=data['m'][i];x=data['x'][i];tick=m[5];multiple=m[4]
        distance=max(tick,np.ceil(1.5*x[7]*m[3]/tick)*tick)
        qualified=(raw[:,2]>0)&(raw[:,4]>=raw[:,2]);raw=raw[qualified]
        start=np.searchsorted(raw[:,0],entry_ms);end=np.searchsorted(raw[:,0],entry_ms+900000)
        r=dict(symbol=symbol,entry_ms=entry_ms,original_net=float(record['net']),ticks=len(raw),lots=lots)
        if start>=len(raw) or end>=len(raw) or raw[start,0]-entry_ms>5000 or raw[end,0]-(entry_ms+900000)>5000:
            r.update(usable=False,reason='missing quotes within 5 seconds of entry or timed exit');results.append(r);continue
        variants=[]
        for extra in (0,1):
            entry_quote=raw[start,4 if side==1 else 2]
            opened=entry_quote+side*extra*tick;stop=opened-side*distance
            executable=raw[start:end+1,2 if side==1 else 4]
            hit=np.flatnonzero(executable<=stop if side==1 else executable>=stop)
            exit_index=start+int(hit[0]) if len(hit) else end
            price=raw[exit_index,2 if side==1 else 4]-side*extra*tick
            depth_entry=raw[start,5 if side==1 else 3];depth_exit=raw[exit_index,3 if side==1 else 5]
            variants.append(dict(extra_ticks=extra,net=float((side*(price-opened)*multiple-10)*lots),
                entry_quote=float(entry_quote),exit_quote=float(raw[exit_index,2 if side==1 else 4]),
                entry_delay_ms=float(raw[start,0]-entry_ms),exit_ms=float(raw[exit_index,0]),
                enough_displayed_depth=bool(min(depth_entry,depth_exit)>=lots),stop_trigger='executable opposing L1 quote'))
        r.update(usable=True,variants=variants);results.append(r)
        print('tick audit',symbol,entry_ms,r,flush=True)
    (OUT/'tick-fill-audit.json').write_text(json.dumps(dict(selection='first five trades in chronological order; no profit selection',
        source='actual get_tick_serial; no synthetic quote',results=results,
        limitations='L1 displayed depth is not a fill guarantee; no queue/latency model; five episodes cannot train a model.'),ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

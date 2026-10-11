"""Read-only entitlement/data probe; requests actual tick serial, never quote proxy."""
from datetime import datetime,timedelta
import gzip
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from config import load_config
from tqsdk import TqApi,TqAuth,TqSim,TqBacktest


def main():
    c=load_config().tqsdk;folder=ROOT/'.tqsdk/research/fine15m';folder.mkdir(exist_ok=True)
    symbol='DCE.eb2509';start=datetime(2025,8,18,10,0)
    api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),
        backtest=TqBacktest(start_dt=start,end_dt=start+timedelta(days=2)),debug=False,disable_print=True)
    frames={};status={}
    try:
        async def request():
            frames['minute']=api.get_kline_serial(symbol,60,data_length=10000)
            frames['tick']=api.get_tick_serial(symbol,data_length=1000)
        task=api.create_task(request());deadline=time.time()+60
        while not task.done() or not all(api.is_serial_ready(a) for a in frames.values()):
            if not api.wait_update(deadline=deadline):raise TimeoutError('serial readiness')
        task.result()
        for name,frame in frames.items():
            d=frame.replace([np.inf,-np.inf],np.nan)
            d=d[d.datetime>0]
            records=json.loads(d.to_json(orient='records'))
            with gzip.open(folder/f'probe-{name}.json.gz','wt',encoding='utf-8') as f:
                json.dump(dict(symbol=symbol,backtest_start=str(start),source='actual public serial',rows=records),f)
            status[name]=dict(rows=len(d),columns=list(d.columns),first_ns=int(d.datetime.min()),last_ns=int(d.datetime.max()))
            if name=='tick':
                status[name]['valid_best_quotes']=int(((d.bid_price1>0)&(d.ask_price1>=d.bid_price1)&(d.bid_volume1>=0)&(d.ask_volume1>=0)).sum())
                status[name]['valid_amount']=int(np.isfinite(d.amount).sum())
        status['success']=True
    except Exception as e:
        message=str(e).replace(c.account,'[account]').replace(c.password,'[password]')
        status.update(success=False,error_type=type(e).__name__,error=message)
    finally:api.close()
    (folder/'probe.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(status,ensure_ascii=False),flush=True)


if __name__=='__main__':main()

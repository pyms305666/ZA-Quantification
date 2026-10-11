"""Retain pre-window daily context so the first study date has causal features."""
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
    folder=ROOT/'.tqsdk/research/history';todo=[]
    for path in sorted(folder.glob('*.json.gz')):
        with gzip.open(path,'rt',encoding='utf-8') as f:d=json.load(f)
        start=int(datetime.fromisoformat(d['start']).replace(tzinfo=CN).timestamp()*1000)
        first=d['coverage']['86400']['raw_first']
        if first and first<start and not d['coverage']['86400'].get('warmup_loaded'):todo.append(path)
    print('daily warmup contracts',len(todo),flush=True)
    c=load_config().tqsdk
    for offset in range(0,len(todo),64):
        subset=todo[offset:offset+64];frames={}
        api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),debug=False,disable_print=True)
        try:
            async def subscribe():
                for path in subset:frames[path]=api.get_kline_serial(path.name.removesuffix('.json.gz'),86400,data_length=1500)
            task=api.create_task(subscribe());deadline=time.time()+120
            while not task.done() or not all(api.is_serial_ready(f) for f in frames.values()):
                if not api.wait_update(deadline=deadline):raise TimeoutError('daily warmup timeout')
            task.result()
            for path,frame in frames.items():
                with gzip.open(path,'rt',encoding='utf-8') as f:d=json.load(f)
                start=int(datetime.fromisoformat(d['start']).replace(tzinfo=CN).timestamp()*1000)
                lower=start-120*86400000;rows={b[0]:b for b in d['bars']['86400']}
                for b in frame.to_dict('records'):
                    keys=('datetime','open','high','low','close','volume','close_oi')
                    if not all(math.isfinite(float(b[k])) for k in keys):continue
                    t=int(b['datetime'])//1000000
                    if lower<=t<start:rows[t]=[t]+[float(b[k]) for k in keys[1:]]
                d['bars']['86400']=[rows[t] for t in sorted(rows)]
                d['coverage']['86400']['warmup_loaded']=True
                temporary=path.with_suffix('.gz.tmp')
                with gzip.open(temporary,'wt',encoding='utf-8') as f:json.dump(d,f,ensure_ascii=False,allow_nan=False)
                temporary.replace(path)
            print('daily warmup',offset+len(subset),'/',len(todo),flush=True)
        finally:api.close()


if __name__=='__main__':main()

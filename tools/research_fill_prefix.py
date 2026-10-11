"""Fill truncated 15-minute prefixes with authorized past TqBacktest snapshots."""
from collections import defaultdict
from datetime import date, datetime, timedelta
import gzip
import json
import math
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from market.decision_profiles import CN
from research.data_quality import truncated_prefix


def pending(folder):
    groups=defaultdict(list)
    for path in sorted(folder.glob('*.json.gz')):
        with gzip.open(path,'rt',encoding='utf-8') as f:d=json.load(f)
        daily=d['bars']['86400'];short=d['bars']['900']
        if truncated_prefix(d):
            first=datetime.fromtimestamp(short[0][0]/1000,CN)
            year,month=first.year,first.month+1
            if month==13:year,month=year+1,1
            cutoff=date(year,month,1)+timedelta(days=3)
            groups[cutoff].append(path)
    return groups


def main():
    from config import load_config
    from tqsdk import TqApi,TqAuth,TqSim,TqBacktest
    folder=ROOT/'.tqsdk/research/history';c=load_config().tqsdk
    for iteration in range(12):
        groups=pending(folder)
        print('prefix round',iteration,'gaps',sum(map(len,groups.values())),flush=True)
        if not groups:return
        for cutoff,paths in sorted(groups.items()):
            for offset in range(0,len(paths),48):
                subset=paths[offset:offset+48]
                api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),
                          backtest=TqBacktest(start_dt=cutoff,end_dt=cutoff+timedelta(days=10)),debug=False,disable_print=True)
                frames={}
                try:
                    async def subscribe():
                        for path in subset:frames[path]=api.get_kline_serial(path.name.removesuffix('.json.gz'),900,data_length=8000)
                    task=api.create_task(subscribe());deadline=time.time()+120
                    while not task.done() or not all(api.is_serial_ready(f) for f in frames.values()):
                        if not api.wait_update(deadline=deadline):raise TimeoutError('prefix readiness timeout')
                    task.result()
                    for path,frame in frames.items():
                        with gzip.open(path,'rt',encoding='utf-8') as f:d=json.load(f)
                        old_first=d['bars']['900'][0][0]
                        lower=int(datetime.fromisoformat(d['start']).replace(tzinfo=CN).timestamp()*1000)-3*86400000
                        additions={}
                        for row in frame.to_dict('records'):
                            keys=('datetime','open','high','low','close','volume','close_oi')
                            if not all(math.isfinite(float(row[k])) for k in keys):continue
                            t=int(row['datetime'])//1000000
                            if lower<=t<old_first:additions[t]=[t]+[float(row[k]) for k in keys[1:]]
                        if not additions:raise RuntimeError('no prefix progress: '+d['meta']['instrument_id'])
                        additions.update({b[0]:b for b in d['bars']['900']})
                        d['bars']['900']=[additions[t] for t in sorted(additions)]
                        d['coverage']['900'].setdefault('prefix_snapshots',[]).append(str(cutoff))
                        temporary=path.with_suffix('.gz.tmp')
                        with gzip.open(temporary,'wt',encoding='utf-8') as f:json.dump(d,f,ensure_ascii=False,allow_nan=False)
                        temporary.replace(path)
                    print('prefix snapshot',cutoff,len(subset),'contracts complete',flush=True)
                finally:api.close()
    raise RuntimeError('prefix gaps remain after 12 rounds')


if __name__=='__main__':main()

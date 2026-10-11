"""Exercise the observer's real run loop with isolated fake clocks and market API."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import tools.forward_decision_accuracy as service
from research.forward_runtime import Journal


def main():
    signal=int(pd.Timestamp('2026-09-28 10:30',tz='Asia/Shanghai').timestamp()*1000)
    real_base=service.BASE;checks=[]
    for case in ('normal','clock_lead','stale','missing_endpoint','late_commit'):
        with tempfile.TemporaryDirectory(prefix='decision-observer-verify-') as tmp:
            folder=Path(tmp);clock=SimpleNamespace(now=signal-1000,step=0)
            shutil.copy2(real_base/'manifest.json',folder/'manifest.json')
            shutil.copy2(real_base/'calibration.joblib',folder/'calibration.joblib')
            parent=folder/'selector.json'
            parent.write_text(json.dumps(dict(wall_ms=clock.now,state='collecting',selected_symbols={'TEST':'TEST.X'})))
            q=SimpleNamespace(datetime='',last_price=100.,price_tick=1.,volume_multiple=10.,expire_datetime=signal/1000+86400*30,
                trading_time={'day':[['09:00:00','11:30:00']],'night':[]})
            def quote_at(ms,price):
                q.datetime=datetime.fromtimestamp(ms/1000,service.CN).isoformat(sep=' ').split('+')[0];q.last_price=price
            quote_at(signal-1000,100)
            class FakeApi:
                def __init__(self,*args,**kwargs):pass
                def get_quote(self,symbol):return q
                def get_kline_serial(self,symbol,p,data_length):
                    rows=[]
                    for i in range(200):
                        close=50+(i/199)**2*50
                        rows.append(dict(datetime=(signal-(200-i)*p*1000)*1000000,open=close-.1,high=close+.2,low=close-.2,
                            close=close,volume=800 if i==199 else 100,close_oi=1000+i))
                    return pd.DataFrame(rows)
                def is_serial_ready(self,frame):return True
                def wait_update(self,deadline):
                    clock.step+=1
                    if clock.step==1:
                        clock.now=signal+1500;quote_at(signal-60000 if case=='stale' else clock.now+500 if case=='clock_lead' else clock.now,100)
                    elif clock.step==2:
                        clock.now=signal+(906000 if case=='missing_endpoint' else 900100);quote_at(clock.now+500 if case=='clock_lead' else clock.now,105)
                    elif clock.step==3:
                        clock.now=signal+1800100;quote_at(clock.now+500 if case=='clock_lead' else clock.now,105)
                    else:raise KeyboardInterrupt()
                def close(self):pass
            original=Journal.event
            def event(instance,kind,payload,*args,**kwargs):
                result=original(instance,kind,payload,*args,**kwargs)
                if case=='late_commit' and kind=='prediction':clock.now=signal+6000
                return result
            import tqsdk
            with (patch.object(service,'BASE',folder),patch.object(service,'PARENT',parent),patch.object(service.time,'time',lambda:clock.now/1000),
                patch.object(service,'load_config',lambda:SimpleNamespace(tqsdk=SimpleNamespace(account='fake',password='fake'))),
                patch.object(tqsdk,'TqApi',FakeApi),patch.object(tqsdk,'TqAuth',lambda *a:None),patch.object(tqsdk,'TqSim',lambda:None),
                patch.object(Journal,'event',event)):
                try:service.run(0)
                except KeyboardInterrupt:pass
            journal=Journal(folder/'events.sqlite3');journal.verify()
            counts=dict(journal.db.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
            stats=journal.read('stats',{})
            if case in ('normal','clock_lead'):
                assert counts.get('prediction_committed')==1 and counts.get('outcome')==2
                assert stats['outcomes']['original_15']['signals']==1 and stats['outcomes']['original_15']['correct']==1
                assert stats['outcomes']['original_15']['cost_hits']==1
            elif case=='stale':assert counts.get('prediction',0)==0
            elif case=='missing_endpoint':assert counts.get('missing_endpoint')==1 and counts.get('outcome')==1 and stats['invalid_endpoints']==1
            else:assert counts.get('late_prediction')==1 and counts.get('outcome',0)==0 and counts.get('prediction_committed',0)==0
            journal.db.close();checks.append(case)
    output=ROOT/'docs/研究/2026-09-27-决策因子与成本状态'
    output.mkdir(exist_ok=True)
    (output/'观察程序核验.json').write_text(json.dumps(dict(passed=True,cases=checks,
        scope='Real observer loop with fake API and clock, real SQLite and frozen candidate; all synthetic data isolated in temporary directories.',
        limitation='No real-market prediction or fill is claimed.'),ensure_ascii=False,indent=2),encoding='utf-8')
    print('Observer loop fault checks passed:',checks)


if __name__=='__main__':main()

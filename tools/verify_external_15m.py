"""Check frozen predictions, fingerprints and accounting for external research."""
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
from threadpoolctl import threadpool_limits
import tools.validate_external_15m as b


def main():
    result=json.loads((b.OUT/'results.json').read_text(encoding='utf-8'))
    checks=[]
    for path,expected in result['code_sha256'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==expected,path
    checks.append('recorded code fingerprints match')
    for split in ('validation','test'):
        run=result['inference'][split]
        assert hashlib.sha256((b.CACHE/f'{split}.npz').read_bytes()).hexdigest()==run['dataset_sha256']
        assert hashlib.sha256((b.CACHE/f'kronos-{split}.npz').read_bytes()).hexdigest()==run['forecast_sha256']
        for name,expected in run['artifact_sha256'].items():
            assert hashlib.sha256((b.CACHE/name/'model.safetensors').read_bytes()).hexdigest()==expected
    checks.append('input, forecast and weight fingerprints match')
    test=b.load('test')
    lookup={(int(row[0]),int(row[1])):i for i,row in enumerate(test[1])}
    for name,r in result['results'].items():
        c=r['selection']['choice'];saved=np.load(b.CACHE/f'{name}-frozen-scores.npz')
        actual=b.summary(b.execute(test,saved['mean'],saved['win'],c))
        assert actual==r['historical'],name
        if name.startswith('event_filter'):
            folder=b.CACHE/'full_events' if name.endswith('_full') else b.CACHE
            artifact=joblib.load(folder/f"event-{c['primary']}-{c['recipe']}.joblib")
            predicted=b.event_predictions(artifact['reg'],artifact['clf'],test[0][:256],test[3][:256,c['primary']])
            np.testing.assert_allclose(predicted[0],saved['mean'][:256],rtol=0,atol=1e-12)
            np.testing.assert_allclose(predicted[1],saved['win'][:256],rtol=0,atol=1e-12)
        rows=list(csv.DictReader((b.OUT/f'{name}-成交.csv').open(encoding='utf-8-sig')))
        assert len(rows)==actual['trades']
        balance=50000.;locked=-1
        for row in rows:
            entry=int(row['entry_ms']);exit_time=int(row['exit_ms']);lots=int(row['lots'])
            assert entry>=locked and 0<exit_time-entry<=1800000
            assert (entry+28800000)//86400000==(exit_time+28800000)//86400000
            assert 1<=lots<=10
            assert abs(float(row['prior_balance'])-balance)<1e-7
            symbols=json.loads((b.CACHE/'metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
            i=lookup[(entry,symbols.index(row['symbol']))];m=test[1][i];x=test[0][i]
            sign=int(row['direction']);tick=m[5];multiple=m[4]
            distance=max(tick,np.ceil(1.5*x[7]*m[3]/tick)*tick)
            risk=distance*multiple+10+2*tick*multiple
            assert lots==min(10,int(min(900,balance*.018)/risk),int(balance/max(m[7]*multiple*.15,1)))
            opened=m[7]+sign*tick;stop=opened-sign*distance
            target=opened+sign*np.ceil(b.RECIPES[c['recipe']][1]*distance/tick)*tick
            n=b.RECIPES[c['recipe']][0];price=m[10 if n==1 else 14];end=entry+n*900000
            for j in range(n):
                o,h,l,_=m[7+4*j:11+4*j]
                gap=(o<=stop) if sign==1 else (o>=stop)
                bad=(l<=stop) if sign==1 else (h>=stop)
                good=(h>=target) if sign==1 else (l<=target)
                if gap or bad:
                    price=o if gap else stop;end=entry+(j+1)*900000;break
                if good:
                    price=target;end=entry+(j+1)*900000;break
            expected=(sign*(price-sign*tick-opened)*multiple-10)*lots
            assert end==exit_time and abs(expected-float(row['net']))<1e-7
            balance+=float(row['net']);assert abs(float(row['balance'])-balance)<1e-7
            locked=exit_time
        assert abs(balance-50000-actual['net'])<1e-7
        checks.append(f'{name}: exact frozen replay, independent per-trade fill/fee/lot arithmetic, ledger totals, no overlapping positions, duration/day/lot bounds')
    selected=json.loads((b.OUT/'selection.json').read_text(encoding='utf-8'))
    assert len(selected['comparisons'])==45
    assert all(c['validation_approved']==b.choose([a for a in selected['comparisons'] if a['model']==name])[1]
               for name,c in selected['selections'].items())
    checks.append('45 comparisons retained; approval independently recomputed')
    b.save_json(b.OUT/'核验记录.json',dict(passed=True,checks=checks,
        limitations='Checks reproducibility/accounting; does not establish actual tick fills or profitability.'))
    print(json.dumps(checks,ensure_ascii=False),flush=True)


if __name__=='__main__':
    with threadpool_limits(limits=4):main()

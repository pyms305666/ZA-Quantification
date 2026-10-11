"""Independent path examples, causal features, and frozen model replay checks."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits
from research.minute_intraday import features,outcomes,replay
from tools.research_15m_profit import save_json,summary
import tools.research_minute_15m as b


def main(examples_only=False):
    checks=[]
    x=np.zeros((3,37));x[:,7]=10/1.5/100
    m=np.zeros((3,16));m[:,3]=100;m[:,4]=10;m[:,5]=1
    future=np.tile([100,104,98,103],(3,30,1)).astype(float)
    future[0,-1]=[103,108,101,107]
    future[1,5]=[100,104,85,90]
    future[2,5]=[80,90,75,85]
    pnl,ends,_,risk=outcomes(x,m,future,2)
    np.testing.assert_allclose(pnl[:,0],[40,-120,-230],rtol=0,atol=1e-8)
    np.testing.assert_allclose(ends[:,0],[1800000,360000,360000],rtol=0,atol=0)
    np.testing.assert_allclose(risk,[130,130,130],rtol=0,atol=1e-8)
    checks.append('independent terminal/stop/intraminute-gap PnL, stop times and risk budget examples')
    past=np.tile([100,104,98,103,10,100],(3,15,1)).astype(float)
    before=features(past,np.ones(3)*5)
    future[:,:,0:4]*=2
    np.testing.assert_array_equal(before,features(past,np.ones(3)*5))
    assert np.isfinite(before).all()
    checks.append('minute features accept only completed past window; future-path changes cannot alter inputs')
    if examples_only:
        print(json.dumps(checks),flush=True);return
    result=json.loads((b.OUT/'results.json').read_text(encoding='utf-8'))
    for name,expected in result['code_sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected
    assert hashlib.sha256((b.CACHE/'model-data.npz').read_bytes()).hexdigest()==result['audit']['data_sha256']
    for name,expected in result['model_sha256'].items():
        assert hashlib.sha256((b.CACHE/f'{name}.joblib').read_bytes()).hexdigest()==expected
    checks.append('data/model/source fingerprints match')
    dataset=np.load(b.CACHE/'model-data.npz');full_m=dataset['m'];past_data=dataset['past'];full_x=dataset['x']
    symbols=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    for i in np.linspace(0,len(full_m)-1,24).astype(int):
        when=full_m[i,0];symbol=symbols[int(full_m[i,1])]
        month=pd.Timestamp(when,unit='ms',tz='UTC').tz_convert('Asia/Shanghai').strftime('%Y-%m')
        raw=np.array(json.loads(gzip.decompress((b.CACHE/f'minutes/{symbol}-{month}.json.gz').read_bytes()))['bars'])
        window=raw[(raw[:,0]>=when-900000)&(raw[:,0]<when)]
        np.testing.assert_array_equal(window[:,0],when+np.arange(-15,0)*60000)
        np.testing.assert_array_equal(window[:,1:],past_data[i])
        reconstructed=features(window[None,:,1:],np.array([full_x[i,7]*full_m[i,3]]))[0]
        np.testing.assert_allclose(reconstructed,full_x[i,28:],rtol=0,atol=1e-12)
    checks.append('24 source windows confirm every input minute is closed before signal and stored features reproduce')
    test=b.load_parts()[-1]
    for name,r in result['results'].items():
        c=r['selection']['choice'];h=c['horizon'];model=joblib.load(b.CACHE/f'{name}-{h}.joblib')
        saved=np.load(b.CACHE/f'{name}-{h}-test.npz');mean,prob=saved['mean'],saved['prob']
        predicted=b.predictions(model,test[0][:256])
        np.testing.assert_allclose(predicted[0],mean[:256],rtol=0,atol=1e-12)
        np.testing.assert_allclose(predicted[1],prob[:256],rtol=0,atol=1e-12)
        actual=replay(*test,mean,prob,h,c['edge'],c['floor'])
        assert summary(actual)==r['historical']
        balance=50000;locked=-1
        for row in actual['ledger']:
            assert row[0]>=locked and 0<row[1]-row[0]<=h*900000 and 1<=row[5]<=10
            assert (row[0]+28800000)//86400000==(row[1]+28800000)//86400000
            assert abs(row[8]-balance)<1e-7
            balance+=row[6];assert abs(row[7]-balance)<1e-7;locked=row[1]
        checks.append(f'{name}: saved scores reproduce, exact replay, cash/position/duration/day bounds')
    save_json(b.OUT/'核验记录.json',dict(passed=True,checks=checks,
        limitation='Minute touch still does not establish real tick fills or profitability.'))
    print(json.dumps(checks),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--examples-only',action='store_true');args=parser.parse_args()
    with threadpool_limits(limits=4):main(args.examples_only)

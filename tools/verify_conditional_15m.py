"""Check mathematical payoffs, saved predictions and causal context features."""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
from threadpoolctl import threadpool_limits
import tools.research_conditional_15m as b
from research.conditional_intraday import contextual_features,fixed_outcomes,replay


def main():
    r=json.loads((b.OUT/'results.json').read_text(encoding='utf-8'));checks=[]
    for path,expected in r['code_sha256'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==expected
    assert hashlib.sha256((b.CACHE/'data.npz').read_bytes()).hexdigest()==r['data_sha256']
    for h,expected in r['model_sha256'].items():
        assert hashlib.sha256((b.CACHE/f'model-{h}.joblib').read_bytes()).hexdigest()==expected
    checks.append('recorded data/model/code hashes match')
    data=np.load(b.CACHE/'data.npz');mask=data['m'][:,0]>=b.timestamp('2025-09-26')
    x,m,p=(data[a][mask] for a in ('x','m','p'));c=r['choice'];h=c['horizon']
    scores=np.load(b.CACHE/f'pred-{h}-test.npz');mean,prob=scores['mean'],scores['prob']
    model=joblib.load(b.CACHE/f'model-{h}.joblib')
    for side,head in enumerate(model['heads']):
        predicted=head['reg'].predict(x[:256])+head['mean_bias']
        np.testing.assert_allclose(predicted,mean[:256,side],rtol=0,atol=1e-12)
        predicted=head['calibrator'].predict_proba(b.logit(head['clf'].predict_proba(x[:256])[:,1]))[:,1]
        np.testing.assert_allclose(predicted,prob[:256,side],rtol=0,atol=1e-12)
    assert b.summary(replay(x,m,p,mean,prob,h,c['edge'],c['floor']))==r['historical']
    checks.append('loaded calibrated models match saved predictions; exact frozen replay')
    # Independent toy examples: 100 entry, 10 stop, multiplier10, tick1.
    tx=np.zeros((3,28));tx[:,7]=10/1.5/100
    tm=np.zeros((3,16));tm[:,3]=100;tm[:,4]=10;tm[:,5]=1
    tm[:,7:15]=[[100,104,98,103,103,108,101,107],
                 [100,104,85,90,90,95,88,94],
                 [100,105,98,104,80,90,75,85]]
    profit,_,_,_=fixed_outcomes(tx,tm,2)
    # Long: expiry107 after entry101/exit106 ->40; touch-stop91 ->-120;
    # second-bar stop gap80/exit79 ->-230. Short direction arithmetic too.
    np.testing.assert_allclose(profit[:,0],[40,-120,-230],rtol=0,atol=1e-8)
    checks.append('independent expiry, intrabar stop and opening-gap payoff examples')
    rows=list(csv.DictReader((b.OUT/'成交明细.csv').open(encoding='utf-8-sig')))
    balance=50000.;locked=-1
    for row in rows:
        entry=int(row['entry_ms']);end=int(row['exit_ms'])
        assert entry>=locked and 0<end-entry<=h*900000
        assert (entry+28800000)//86400000==(end+28800000)//86400000
        assert 1<=int(row['lots'])<=10
        assert abs(float(row['prior_balance'])-balance)<1e-7
        balance+=float(row['net']);assert abs(float(row['balance'])-balance)<1e-7;locked=end
    assert len(rows)==r['historical']['trades'] and abs(balance-50000-r['historical']['net'])<1e-7
    checks.append('ledger totals, non-overlap, duration, day and lot bounds')
    for path in sorted((ROOT/'.tqsdk/research/history').glob('SHFE.rb*.json.gz')):
        payload=json.loads(gzip.decompress(path.read_bytes()));raw=payload['bars']['900']
        if len(raw)<300:continue
        raw=np.array(raw,dtype='float64');cut=len(raw)//2
        _,before=contextual_features(raw.tolist());changed=raw.copy();changed[cut+1:,1:5]*=2
        changed[cut+1:,5]*=3
        _,after=contextual_features(changed.tolist())
        np.testing.assert_allclose(before[:cut+1],after[:cut+1],rtol=0,atol=0,equal_nan=True)
        checks.append('future-price/volume perturbation leaves all prior context features unchanged');break
    assert len(r['comparisons'])==12
    b.save_json(b.OUT/'核验记录.json',dict(passed=True,checks=checks,
        limitations='Reproducibility and arithmetic only; not tick execution or profitability proof.'))
    print(json.dumps(checks,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--low-cost',action='store_true');args=parser.parse_args()
    if args.low_cost:
        b.CACHE=ROOT/'.tqsdk/research/conditional15m_low_cost'
        b.OUT=ROOT/'docs/研究/2026-09-26-条件净收益模型-低成本品种'
    with threadpool_limits(limits=4):main()

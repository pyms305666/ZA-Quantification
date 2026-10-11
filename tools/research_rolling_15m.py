"""Predeclared chronological rolling experiment, with no client or order changes."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits
from research.minute_intraday import outcomes,replay
from tools.research_conditional_15m import SETTINGS,timestamp
from tools.research_15m_profit import save_json,summary

CACHE=ROOT/'.tqsdk/research/rolling15m'
OUT=ROOT/'docs/研究/2026-09-27-滚动训练与误差余量'
SOURCE=ROOT/'.tqsdk/research/fine15m/model-data.npz'
ARMS=('static','rolling_global','rolling_bins','rolling_margin')

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def declare():
    CACHE.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    cfg=dict(study='monthly rolling refit and calibration error margin',
        declared_at_utc=pd.Timestamp.now(tz='UTC').isoformat(),
        evaluation_start='2025-10-01',evaluation_end_exclusive='2026-09-25',
        history_previously_exposed=True,training_months=24,calibration_months=1,
        features=37,horizons=[1,2],arms=list(ARMS),candidate_count=8,
        training_parameters=SETTINGS,recency_half_life_days=365,
        calibration_bins_training_quantiles=[.5,.9],minimum_calibration_rows=100,
        minimum_calibration_days=10,margin_multiplier=1.645,
        margin_is_heuristic_not_guaranteed_individual_confidence=True,
        edge=0,profit_probability_filter=False,fee_roundtrip_per_lot=10,slippage_ticks_per_side=1,
        account=50000,maximum_lots=10,risk_cash=900,risk_equity_fraction=.018,
        margin_rate=.15,stop_atr=1.5,cost_stop_filter=.35,
        capital_continuous_across_months=True,refuse_candidate_selection_after_results=True,
        research_screen=dict(min_trades=100,net_positive=True,min_profit_factor=1.2,
            max_drawdown=.1,min_positive_calendar_months=8),
        source_sha256=digest(SOURCE),
        frozen_model_sha256={str(h):digest(ROOT/f'.tqsdk/research/fine15m/minute-{h}.joblib') for h in (1,2)})
    path=OUT/'protocol.json'
    if path.exists():raise RuntimeError('Protocol exists; do not overwrite declared rules')
    save_json(path,cfg);print('Declared 8 arms before fitting',flush=True)

def load():
    z=np.load(SOURCE);return tuple(z[k] for k in ('x','m','p','future'))

def partitions(m):
    for month in pd.date_range('2025-10-01','2026-09-01',freq='MS',tz='Asia/Shanghai'):
        cal_start=month-pd.DateOffset(months=1);train_start=cal_start-pd.DateOffset(months=24)
        a,b,c,e=[int(t.timestamp()*1000) for t in (train_start,cal_start,month,month+pd.DateOffset(months=1))]
        e=min(e,timestamp('2026-09-25'));t=m[:,0]
        yield month.strftime('%Y-%m'),np.flatnonzero((t>=a)&(t+1800000<b)),np.flatnonzero((t>=b)&(t+1800000<c)),np.flatnonzero((t>=c)&(t+1800000<e)),(a,b,c,e)

def cluster_calibration(residual,days):
    """Row-weighted bias and day-cluster sandwich SE, with finite-cluster correction."""
    unique,index=np.unique(days,return_inverse=True);n=len(residual);g=len(unique)
    if n<100 or g<10:return dict(valid=False,n=n,days=g,bias=0.,se=None)
    bias=float(residual.mean());sums=np.bincount(index,weights=residual-bias)
    se=float(np.sqrt(g/(g-1)*np.sum(sums*sums))/n)
    return dict(valid=True,n=n,days=g,bias=bias,se=se)

def calibrate(pred_train,pred_cal,y_cal,days):
    edges=np.unique(np.quantile(pred_train,[.5,.9]));labels=np.searchsorted(edges,pred_cal,side='right')
    residual=y_cal-pred_cal;global_stats=cluster_calibration(residual,days);bins=[]
    for k in range(len(edges)+1):
        s=cluster_calibration(residual[labels==k],days[labels==k]);s['fallback_global']=not s['valid']
        if not s['valid']:s=dict(global_stats,fallback_global=True)
        bins.append(s)
    return dict(edges=edges.tolist(),global_stats=global_stats,bins=bins)

def adjusted(raw,cal):
    global_s=cal['global_stats'];g=raw+global_s['bias'] if global_s['valid'] else np.full_like(raw,-np.inf)
    labels=np.searchsorted(cal['edges'],raw,side='right');b=np.full_like(raw,-np.inf);lower=b.copy()
    for k,s in enumerate(cal['bins']):
        if s['valid']:
            b[labels==k]=raw[labels==k]+s['bias'];lower[labels==k]=b[labels==k]-1.645*s['se']
    return g,b,lower

def fit():
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    assert digest(SOURCE)==cfg['source_sha256']
    data=load();x,m,p,future=data
    signature=dict(protocol=digest(OUT/'protocol.json'),code=digest(Path(__file__)),source=digest(SOURCE))
    for horizon in (1,2):
        y,_,_,risk=outcomes(x,m,future,horizon);target=y/risk[:,None]
        frozen_path=ROOT/f'.tqsdk/research/fine15m/minute-{horizon}.joblib'
        assert digest(frozen_path)==cfg['frozen_model_sha256'][str(horizon)]
        frozen=joblib.load(frozen_path)
        for month,tr,ca,te,bounds in partitions(m):
            path=CACHE/f'{month}-{horizon}.joblib'
            if path.exists():
                saved=joblib.load(path)
                if saved['signature']!=signature:raise RuntimeError('Resume fingerprint mismatch')
                print('reuse',month,horizon,flush=True);continue
            assert len(tr)>1000 and len(ca)>=100 and len(te)>0
            weights=np.exp(-np.log(2)*(bounds[1]-m[tr,0])/86400000/365)
            days=((m[ca,0]+28800000)//86400000).astype(int)
            heads=[];scores={name:np.zeros((len(te),2)) for name in ARMS}
            for side in (0,1):
                model=HistGradientBoostingRegressor(**SETTINGS)
                model.fit(x[tr],target[tr,side],sample_weight=weights)
                cal=calibrate(model.predict(x[tr]),model.predict(x[ca]),target[ca,side],days)
                raw=model.predict(x[te]);a,b,c=adjusted(raw,cal)
                scores['rolling_global'][:,side]=a;scores['rolling_bins'][:,side]=b;scores['rolling_margin'][:,side]=c
                old=frozen['heads'][side];scores['static'][:,side]=old['reg'].predict(x[te])+old['bias']
                heads.append(dict(model=model,calibration=cal))
            joblib.dump(dict(signature=signature,heads=heads,month=month,horizon=horizon,
                train_indices=tr,calibration_indices=ca,evaluation_indices=te,bounds=bounds,scores=scores),path)
            print('fit',month,'hold',horizon*15,'rows',len(tr),len(ca),len(te),flush=True)

def evaluate():
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'));data=load();results=[]
    symbol_names=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    for h in (1,2):
        files=[CACHE/f'{month}-{h}.joblib' for month,*_ in partitions(data[1])]
        folds=[joblib.load(f) for f in files];ix=np.concatenate([f['evaluation_indices'] for f in folds]);d=tuple(a[ix] for a in data)
        for arm in ARMS:
            score=np.concatenate([f['scores'][arm] for f in folds]);s=replay(*d,score,np.ones_like(score),h,0,0)
            months=[]
            for month,_,_,_,bounds in partitions(data[1]):
                rows=[row for row in s['ledger'] if bounds[2]<=row[0]<bounds[3]]
                months.append(dict(month=month,trades=len(rows),net=sum(row[6] for row in rows),
                    win_rate=float(np.mean([row[6]>0 for row in rows])) if rows else None))
            positive=sum(q['net']>0 for q in months);stats=summary(s)
            approved=bool(s['trades']>=100 and s['net']>0 and (s['profit_factor'] or 0)>=1.2 and s['max_drawdown']<=.1 and positive>=8)
            costs={f'fee{fee}_slip{slip}':summary(replay(*d,score,np.ones_like(score),h,0,0,fee,slip)) for fee,slip in [(0,0),(10,0),(10,2)]}
            entry_lookup={(int(row[0]),int(row[1])):i for i,row in enumerate(d[1])}
            fees=sum(row[5]*10 for row in s['ledger']);slip=sum(2*row[5]*d[1][entry_lookup[row[0],row[2]],4]*d[1][entry_lookup[row[0],row[2]],5] for row in s['ledger'])
            with (OUT/f'{arm}-{h}-成交.csv').open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
                for row in s['ledger']:
                    row=row.copy();row[2]=symbol_names[row[2]];writer.writerow(row)
            record=dict(arm=arm,horizon=h,historical=stats,positive_months=positive,months=months,
                research_screen_passed=approved,cost_stress=costs,
                fixed_ledger_costs=dict(gross=s['net']+fees+slip,fee=fees,slippage=slip,net=s['net']))
            results.append(record);print('evaluated',arm,h,stats,flush=True)
    save_json(OUT/'results.json',dict(protocol=cfg,results=results,no_winner_selected=True,
        source_sha256=digest(SOURCE),code_sha256=digest(Path(__file__)),
        execution_code_sha256=digest(ROOT/'research/minute_intraday.py'),
        folds_sha256={p.name:digest(p) for p in sorted(CACHE.glob('*.joblib'))}))

def verify():
    result=json.loads((OUT/'results.json').read_text(encoding='utf-8'));data=load();x,m,p,future=data
    assert digest(SOURCE)==result['source_sha256'] and digest(Path(__file__))==result['code_sha256']
    assert digest(ROOT/'research/minute_intraday.py')==result['execution_code_sha256']
    for name,sha in result['folds_sha256'].items():assert digest(CACHE/name)==sha
    days=np.repeat(np.arange(10),10);residual=np.repeat(np.arange(10,dtype=float),10)
    example=cluster_calibration(residual,days)
    np.testing.assert_allclose(example['bias'],4.5)
    np.testing.assert_allclose(example['se'],np.std(np.arange(10),ddof=1)/np.sqrt(10))
    assert not cluster_calibration(residual[:90],days[:90])['valid']
    for h in (1,2):
        folds=[]
        for month,tr,ca,te,bounds in partitions(m):
            f=joblib.load(CACHE/f'{month}-{h}.joblib');folds.append(f)
            for key,expected in [('train_indices',tr),('calibration_indices',ca),('evaluation_indices',te)]:np.testing.assert_array_equal(f[key],expected)
            assert m[tr,0].max()+1800000<bounds[1] and m[ca,0].max()+1800000<bounds[2]
            y,_,_,risk=outcomes(x[ca],m[ca],future[ca],h);caldays=((m[ca,0]+28800000)//86400000).astype(int)
            for side,head in enumerate(f['heads']):
                c=calibrate(head['model'].predict(x[tr]),head['model'].predict(x[ca]),y[:,side]/risk,caldays)
                assert c==head['calibration']
                scores=adjusted(head['model'].predict(x[te]),c)
                for arm,expected in zip(ARMS[1:],scores):np.testing.assert_allclose(f['scores'][arm][:,side],expected,rtol=0,atol=1e-12)
                assert np.all(scores[2]<=scores[1])
        ix=np.concatenate([f['evaluation_indices'] for f in folds]);assert len(ix)==len(np.unique(ix))
        d=tuple(a[ix] for a in data)
        for arm in ARMS:
            score=np.concatenate([f['scores'][arm] for f in folds]);s=replay(*d,score,np.ones_like(score),h,0,0)
            expected=next(q for q in result['results'] if q['arm']==arm and q['horizon']==h)
            assert summary(s)==expected['historical']
            assert abs(sum(q['net'] for q in expected['months'])-s['net'])<1e-7
            cash=50000;locked=-1
            for row in s['ledger']:
                assert row[0]>=locked and 0<row[1]-row[0]<=h*900000 and 1<=row[5]<=10
                assert (row[0]+28800000)//86400000==(row[1]+28800000)//86400000
                assert abs(row[8]-cash)<1e-7;cash+=row[6];assert abs(row[7]-cash)<1e-7;locked=row[1]
    save_json(OUT/'核验记录.json',dict(passed=True,checks=[
        'data/code/24 fold fingerprints','cluster-SE independent arithmetic example and sparse-day rejection',
        'all fold training/calibration labels end before following interval',
        'all calibration and rolling scores reproduce; margin never raises score',
        'all 8 global-account replays, monthly sums, cash/position/day bounds']))
    print('All rolling checks passed',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['declare','fit','evaluate','verify']);args=parser.parse_args()
    with threadpool_limits(limits=4):globals()[args.mode]()

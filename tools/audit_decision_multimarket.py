"""Direct, past-only ultra evaluator audit on the existing eight-product archive."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from config import RiskConfig
from market.evaluator import evaluate, _trend_factor, _momentum_factor, _volume_oi_factor, _risk_factor
from market.model import Instrument
from tools.audit_decision_accuracy import metrics, bootstrap

OUT=ROOT/'docs/研究/2026-09-27-决策准确度多品种'
CACHE=ROOT/'.tqsdk/research/decision_accuracy'
MINUTES=ROOT/'.tqsdk/research/fine15m/minutes'
SOURCE=ROOT/'.tqsdk/research/fine15m/model-data.npz'
CN=timezone(timedelta(hours=8))
FIELDS=('datetime','open','high','low','close','volume','open_interest')


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def bars(array):return [dict(zip(FIELDS,row)) for row in array.tolist()]


def build():
    OUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    source=np.load(SOURCE);m=source['m'];future=source['future']
    meta=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']
    protocol=dict(source_sha256=sha(SOURCE),model='unchanged market.evaluator ultra',
        sources={p:sha(ROOT/p) for p in ['market/evaluator.py','market/decision_profiles.py','market/indicators.py','tools/audit_decision_multimarket.py','tools/audit_decision_accuracy.py']},
        eligible='Existing frozen 8-product opportunity set; excludes incomplete future labels. Not all-market coverage.',
        input='200 completed 1m/5m/15m bars; require all past 67 native 15m bars match full minute aggregates. 5m uses exact contiguous clock buckets.',
        fit_before='2025-07-01',validation_before='2025-10-01',test_end='2026-09-24',
        models=['original','fixed logistic C=1 on 8 original factor scores'],
        calibration='Training excludes flat 15m labels; validation threshold targets original coverage; no test selection or parameter search.',
        horizons=[15,30],fees=10,adverse_ticks_per_side=1,
        labels='signal last completed close to future endpoint close; flat not correct; no stop or portfolio simulation',
        no_deployment=True)
    if (OUT/'protocol.json').exists() and json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))!=protocol:
        raise RuntimeError('Frozen protocol differs; create a new version')
    save(OUT/'protocol.json',protocol)
    rows=[];skips=Counter();digests={};started=time.monotonic()
    for number,si in enumerate(np.unique(m[:,1]).astype(int),1):
        symbol=meta['symbols'][si];path=ROOT/f'.tqsdk/research/history/{symbol}.json.gz'
        payload=json.loads(gzip.decompress(path.read_bytes()));digests[str(path.relative_to(ROOT))]=sha(path)
        raw=np.array(payload['bars']['900'],dtype=float);raw=raw[np.argsort(raw[:,0])]
        parts=[]
        for minute_path in sorted(MINUTES.glob(f'{symbol}-*.json.gz')):
            digests[str(minute_path.relative_to(ROOT))]=sha(minute_path)
            part=np.array(json.loads(gzip.decompress(minute_path.read_bytes()))['bars'],dtype=float)
            if len(part):parts.append(part)
        ix=np.flatnonzero(m[:,1]==si)
        if not parts:skips['missing_symbol_minutes']+=len(ix);continue
        minute=np.concatenate(parts);minute=minute[np.argsort(minute[:,0],kind='stable')]
        duplicate=minute[1:,0]==minute[:-1,0]
        if duplicate.any() and not np.array_equal(minute[1:][duplicate],minute[:-1][duplicate]):
            raise RuntimeError('Conflicting minute snapshots')
        minute=minute[np.r_[True,np.diff(minute[:,0])!=0]]
        # A native bar is covered only when every minute and aggregate OHLCV agrees.
        need=raw[:,0,None]+np.arange(15)*60000
        positions=np.searchsorted(minute[:,0],need);bounded=np.minimum(positions,len(minute)-1)
        seg=minute[bounded]
        matched=(positions<len(minute)).all(axis=1)&(seg[:,:,0]==need).all(axis=1)
        aggregate=np.column_stack([seg[:,0,1],seg[:,:,2].max(axis=1),seg[:,:,3].min(axis=1),seg[:,-1,4],seg[:,:,5].sum(axis=1)])
        matched&=np.isclose(aggregate,raw[:,1:6],rtol=0,atol=1e-7).all(axis=1)
        mismatch_prefix=np.r_[0,np.cumsum(~matched)]
        # Build native clock-aligned 5m bars only from five complete minute rows.
        starts=np.flatnonzero(minute[:,0]%300000==0)
        starts=starts[starts+4<len(minute)]
        seg5=minute[starts[:,None]+np.arange(5)]
        good=(seg5[:,:,0]==seg5[:,0,0,None]+np.arange(5)*60000).all(axis=1)
        seg5=seg5[good]
        five=np.column_stack([seg5[:,0,0],seg5[:,0,1],seg5[:,:,2].max(axis=1),seg5[:,:,3].min(axis=1),seg5[:,-1,4],seg5[:,:,5].sum(axis=1),seg5[:,-1,6]])
        minute_bars=bars(minute);five_bars=bars(five);raw_bars=bars(raw)
        md=payload['meta'];product=md['exchange_id']+'.'+md['product_id']
        instrument=Instrument(symbol=symbol,exchange=md['exchange_id'],instrument_id=symbol.split('.')[1],
                              price_tick=md['price_tick'],volume_multiple=int(md['volume_multiple']),expire_datetime=md['expire_datetime'])
        for i in ix:
            t=int(m[i,0]);end=np.searchsorted(raw[:,0],t-900000,side='right')
            if end<67 or raw[end-1,0]!=t-900000 or mismatch_prefix[end]!=mismatch_prefix[end-67]:
                skips['incomplete_past_67_native_bars']+=1;continue
            e1=np.searchsorted(minute[:,0],t-60000,side='right');e5=np.searchsorted(five[:,0],t-300000,side='right')
            if e1<200 or e5<200:
                skips['insufficient_completed_input']+=1;continue
            if (future[i,:,4]<=0).any():skips['zero_future_volume']+=1;continue
            inputs={60:minute_bars[e1-200:e1],300:five_bars[e5-200:e5],900:raw_bars[max(0,end-200):end]}
            quote=dict(last=raw[end-1,4],timestamp=t,expire_datetime=md['expire_datetime'])
            r=evaluate(instrument,quote,inputs,RiskConfig(),'ultra',t)
            if not r['data_ok']:skips['evaluator_data_not_ok']+=1;continue
            ex=inputs[60];co=inputs[300];tr=inputs[900]
            vq=dict(last=ex[-1]['close'],pre_close=ex[-2]['close'],open_interest=ex[-1]['open_interest'],pre_open_interest=ex[-2]['open_interest'])
            features=[*_trend_factor(tr,co)[:2],*_momentum_factor(ex,co)[:2],*_volume_oi_factor(ex,ex,vq)[:2],*_risk_factor(ex,tr,quote)[:2]]
            if abs(sum(features[::2])-r['score_long'])>.051 or abs(sum(features[1::2])-r['score_short'])>.051:
                raise RuntimeError('Application factor reconstruction mismatch')
            dt=datetime.fromtimestamp(t/1000,CN)
            rows.append(dict(symbol=symbol,product=product,time=t,day=str(dt.date()),month=dt.strftime('%Y-%m'),
                session='night' if dt.hour>=18 or dt.hour<6 else 'day',
                side={'LONG':1,'SHORT':-1,'FLAT':0}[r['direction_en']],score=r['score'],factors=features,
                delta15=float(future[i,14,3]-quote['last']),delta30=float(future[i,29,3]-quote['last']),
                multiple=int(md['volume_multiple']),cost=float(10+2*md['price_tick']*md['volume_multiple'])))
        print('symbol',number,symbol,'rows',len(rows),'elapsed',round(time.monotonic()-started),flush=True)
    rows.sort(key=lambda r:(r['time'],r['product']))
    with gzip.open(CACHE/'observations.json.gz','wt',encoding='utf-8') as stream:json.dump(rows,stream,allow_nan=False)
    save(OUT/'data-audit.json',dict(source_opportunities=len(m),kept=len(rows),skips=dict(skips),files_sha256=digests,
        observation_sha256=sha(CACHE/'observations.json.gz'),range=[rows[0]['day'],rows[-1]['day']],products=sorted({r['product'] for r in rows})))


def analyze():
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    import joblib
    with gzip.open(CACHE/'observations.json.gz','rt',encoding='utf-8') as stream:rows=json.load(stream)
    train=[r for r in rows if r['day']<'2025-07-01' and r['delta15']!=0]
    val=[r for r in rows if '2025-07-01'<=r['day']<'2025-10-01']
    test=[r for r in rows if r['day']>='2025-10-01']
    model=make_pipeline(StandardScaler(),LogisticRegression(C=1,max_iter=1000,random_state=20260927))
    model.fit([r['factors'] for r in train],[int(r['delta15']>0) for r in train])
    vp=model.predict_proba([r['factors'] for r in val])[:,1]
    target=sum(bool(r['side']) for r in val)/len(val)
    threshold=float(np.quantile(np.maximum(vp,1-vp),1-target)) if target else 1.01
    joblib.dump(dict(model=model,threshold=threshold),CACHE/'factor-calibration.joblib')
    probabilities=model.predict_proba([r['factors'] for r in test])[:,1]
    for r,p in zip(test,probabilities):
        r['calibrated_side']=(1 if p>=.5 else -1) if max(p,1-p)>=threshold else 0
        r['p_up_nonflat']=float(p)
    result=dict(split=dict(train_nonflat=len(train),validation=len(val),test=len(test)),validation_target_coverage=target,
                threshold=threshold,model_sha256=sha(CACHE/'factor-calibration.joblib'),no_deployment=True,
                all_original={str(h):metrics(rows,h) for h in (15,30)},test={},groups={})
    for key in ('side','calibrated_side'):
        result['test'][key]={str(h):dict(**metrics(test,h,key),day_bootstrap_ci=bootstrap(test,h,key)) for h in (15,30)}
    for field in ('product','month','session'):
        result['groups'][field]={}
        for value in sorted({r[field] for r in test}):
            subset=[r for r in test if r[field]==value]
            result['groups'][field][value]={key:{str(h):metrics(subset,h,key) for h in (15,30)} for key in ('side','calibrated_side')}
    save(OUT/'results.json',result)
    with gzip.open(CACHE/'test-predictions.json.gz','wt',encoding='utf-8') as stream:json.dump(test,stream,allow_nan=False)
    print(json.dumps({k:v for k,v in result.items() if k!='groups'},ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['build','analyze']);args=parser.parse_args()
    build() if args.action=='build' else analyze()

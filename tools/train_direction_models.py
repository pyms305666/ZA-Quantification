"""Chronological research models for 15 minutes and 1/10/40 trading days."""
import gzip
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits
import joblib
from research.direction_model import FEATURES, START, examples, split_indices
from research.data_quality import truncated_prefix

HORIZONS={'15m':(900,1),'1d':(86400,1),'10d':(86400,10),'40d':(86400,40)}
QUOTAS={'train':2000,'validation':600,'test':1000}


def metrics(values, probabilities, threshold):
    ret=values[:,len(FEATURES)]; cost=values[:,len(FEATURES)+1]
    truth=np.where(ret>0,1,np.where(ret<0,0,-1))
    pred=(probabilities>=.5).astype(int)
    active=np.maximum(probabilities,1-probabilities)>=threshold
    nonflat=truth>=0
    selected=active&nonflat
    accuracy=float(np.mean(pred[active]==truth[active])) if active.any() else None
    recalls=[float(np.mean(pred[active&(truth==side)]==side)) for side in (0,1) if (active&(truth==side)).any()]
    direction_returns=np.where(pred==1,ret,-ret)
    result=dict(samples=len(values),flat_samples=int((~nonflat).sum()),active=int(active.sum()),
        coverage=float(active.mean()),accuracy=accuracy,balanced_accuracy=float(np.mean(recalls)) if len(recalls)==2 else None,
        up_accuracy=float(np.mean(truth[active&(pred==1)]==1)) if (active&(pred==1)).any() else None,
        down_accuracy=float(np.mean(truth[active&(pred==0)]==0)) if (active&(pred==0)).any() else None,
        cost_effective_accuracy=float(np.mean(direction_returns[active]>cost[active])) if active.any() else None,
        always_up_matched=float(np.mean(truth[active]==1)) if active.any() else None,
        always_down_matched=float(np.mean(truth[active]==0)) if active.any() else None,
        momentum_matched=float(np.mean((values[active,0]>=0)==truth[active])) if active.any() else None,
        auc=float(roc_auc_score(truth[nonflat],probabilities[nonflat])) if len(np.unique(truth[nonflat]))==2 else None)
    # Approximate uncertainty via calendar-quarter block bootstrap. This
    # preserves market-wide correlations and some overlapping-label dependence.
    times=pd.to_datetime(values[:,-2],unit='ms',utc=True).tz_convert('Asia/Shanghai')
    groups=times.year*4+(times.month-1)//3
    correct=(pred==truth)&active
    counts=np.array([[correct[groups==g].sum(),active[groups==g].sum()] for g in np.unique(groups)],float)
    rng=np.random.default_rng(20260926)
    boot=[]
    for _ in range(400):
        sampled=counts[rng.integers(0,len(counts),len(counts))].sum(axis=0)
        if sampled[1]:boot.append(sampled[0]/sampled[1])
    result['accuracy_quarter_block_interval']=[float(x) for x in np.quantile(boot,[.025,.975])] if boot else None
    result['quarter_blocks']=len(counts)
    benchmark_predictions=[np.ones(len(values),int),np.zeros(len(values),int),(values[:,0]>=0).astype(int)]
    if active.any():
        best=max(benchmark_predictions,key=lambda p:np.mean(p[active]==truth[active]))
        improvement=((pred==truth).astype(int)-(best==truth).astype(int))*active
        paired=np.array([[improvement[groups==g].sum(),active[groups==g].sum()] for g in np.unique(groups)],float)
        gains=[]
        for _ in range(400):
            sampled=paired[rng.integers(0,len(paired),len(paired))].sum(axis=0)
            if sampled[1]:gains.append(sampled[0]/sampled[1])
        result['gain_quarter_block_interval']=[float(x) for x in np.quantile(gains,[.025,.975])] if gains else None
    else:result['gain_quarter_block_interval']=None
    return result


def main():
    folder=ROOT/'.tqsdk/research'
    output=ROOT/'docs/研究/2026-09-26-全商品方向模型';output.mkdir(parents=True,exist_ok=True)
    with gzip.open(folder/'universe.json.gz','rt',encoding='utf-8') as f:universe=json.load(f)
    calendar_path=folder/'trading_calendar.csv'
    calendar=pd.read_csv(calendar_path)
    dates=pd.to_datetime(calendar.loc[calendar['trading'],'date']).dt.tz_localize('Asia/Shanghai')
    trading_days=[int(t.timestamp()*1000) for t in dates]
    paths=sorted((folder/'history').glob('*.json.gz'))
    if len(paths)!=universe['selected_count']:
        raise RuntimeError(f"history incomplete: {len(paths)} / {universe['selected_count']}")
    pools={}; counts={}; coverage=[]; rng=np.random.default_rng(20260926)
    fingerprint=hashlib.sha256(); started=time.monotonic()
    for index,path in enumerate(paths):
        raw=path.read_bytes();fingerprint.update(hashlib.sha256(raw).digest())
        d=json.loads(gzip.decompress(raw));meta=d['meta'];product=meta['exchange_id']+'.'+meta['product_id']
        daily=d['bars']['86400']; intraday=d['bars']['900']
        gap=truncated_prefix(d)
        if gap:raise RuntimeError('unfilled intraday prefix: '+meta['instrument_id'])
        coverage.append(dict(symbol=meta['instrument_id'],product=product,daily=len(daily),intraday=len(intraday),
                             daily_warmup=sum(b[0]<START for b in daily),
                             intraday_prefix_gap=gap))
        for horizon,(period,step) in HORIZONS.items():
            values=examples(d['bars'][str(period)],period,step,meta,trading_days=trading_days)
            if values is None or not len(values):continue
            for split,mask in split_indices(values).items():
                subset=values[mask]
                if not len(subset):continue
                key=(horizon,split,product);counts[key]=counts.get(key,0)+len(subset)
                # Uniform random-priority reservoir within each product and
                # partition prevents many contract months dominating training.
                priority=rng.random(len(subset))
                candidates=np.column_stack([priority,subset])
                if key in pools:candidates=np.concatenate([pools[key],candidates])
                quota=QUOTAS[split]
                if len(candidates)>quota:candidates=candidates[np.argpartition(candidates[:,0],quota-1)[:quota]]
                pools[key]=candidates
        if index%100==0:print('features',index,'/',len(paths),'seconds',round(time.monotonic()-started),flush=True)
    results={};cache=folder/'models';cache.mkdir(parents=True,exist_ok=True)
    for horizon in HORIZONS:
        partitions={}
        for split in QUOTAS:
            parts=[v[:,1:] for (h,s,p),v in pools.items() if h==horizon and s==split]
            partitions[split]=np.concatenate(parts) if parts else np.empty((0,len(FEATURES)+4))
        training=partitions['train'];training=training[training[:,len(FEATURES)]!=0]
        if min(len(partitions['validation']),len(partitions['test']),len(training))<100:
            results[horizon]=dict(status='insufficient_samples');continue
        X=training[:,:len(FEATURES)];y=(training[:,len(FEATURES)]>0).astype(int)
        candidates={'logistic':make_pipeline(StandardScaler(),LogisticRegression(C=.1,class_weight='balanced',max_iter=300)),
            'hist_gradient':HistGradientBoostingClassifier(max_iter=120,max_leaf_nodes=15,max_depth=4,
                min_samples_leaf=100,l2_regularization=5,learning_rate=.05,early_stopping=False,
                class_weight='balanced',random_state=20260926)}
        comparisons=[];models={}
        for name,model in candidates.items():
            with threadpool_limits(limits=4):model.fit(X,y)
            models[name]=model
            probability=model.predict_proba(partitions['validation'][:,:len(FEATURES)])[:,1]
            for threshold in (.5,.55,.60,.65,.70):
                stats=metrics(partitions['validation'],probability,threshold)
                comparisons.append(dict(model=name,threshold=threshold,validation=stats))
            print('fit',horizon,name,'train',len(training),flush=True)
        eligible=[c for c in comparisons if c['validation']['coverage']>=.20 and c['validation']['balanced_accuracy'] is not None]
        def validation_gain(candidate):
            s=candidate['validation']
            benchmark=max(s['always_up_matched'],s['always_down_matched'],s['momentum_matched'])
            return s['accuracy']-benchmark
        winner=max(eligible,key=lambda c:(validation_gain(c),c['validation']['balanced_accuracy']))
        model=models[winner['model']];test=partitions['test']
        probability=model.predict_proba(test[:,:len(FEATURES)])[:,1]
        test_metrics=metrics(test,probability,winner['threshold'])
        per_product={}
        for (h,s,product),values in pools.items():
            if h==horizon and s=='test':
                values=values[:,1:]
                per_product[product]=metrics(values,model.predict_proba(values[:,:len(FEATURES)])[:,1],winner['threshold'])
        results[horizon]=dict(status='research_only',chosen_model=winner['model'],threshold=winner['threshold'],
            validation_accuracy_gain_over_matched_baselines=validation_gain(winner),
            train_products=sorted(p for (h,s,p),v in pools.items() if h==horizon and s=='train' and (v[:,len(FEATURES)+1]!=0).any()),
            test_products_absent_in_train=sorted(set(per_product)-{p for (h,s,p),v in pools.items()
                if h==horizon and s=='train' and (v[:,len(FEATURES)+1]!=0).any()}),
            train_samples=len(training),validation_samples=len(partitions['validation']),test=test_metrics,
            validation_comparisons=comparisons,per_product=per_product)
        joblib.dump(dict(model=model,features=FEATURES,horizon=horizon,threshold=winner['threshold']),cache/(horizon+'.joblib'))
        results[horizon]['artifact_sha256']=hashlib.sha256((cache/(horizon+'.joblib')).read_bytes()).hexdigest()
        if winner['model']=='logistic':
            results[horizon]['logistic_parameters']=dict(mean=model.named_steps['standardscaler'].mean_.tolist(),
                scale=model.named_steps['standardscaler'].scale_.tolist(),
                coefficient=model.named_steps['logisticregression'].coef_[0].tolist(),
                intercept=float(model.named_steps['logisticregression'].intercept_[0]))
        np.savez_compressed(cache/(horizon+'-test.npz'),values=test,probability=probability)
        print('test',horizon,test_metrics,flush=True)
    audit=dict(contracts=len(paths),products=len(universe['products']),data_sha256=fingerprint.hexdigest(),
        trading_calendar_sha256=hashlib.sha256(calendar_path.read_bytes()).hexdigest(),
        versions={p:version(p) for p in ('tqsdk','numpy','pandas','scikit-learn')},
        code_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                     ('research/direction_model.py','research/data_quality.py','tools/train_direction_models.py')},
        coverage=coverage,feature_names=FEATURES,quotas=QUOTAS,
        candidate_counts={':'.join(k):v for k,v in counts.items()},results=results)
    (output/'results.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print('models saved',flush=True)


if __name__=='__main__':
    with threadpool_limits(limits=4):main()

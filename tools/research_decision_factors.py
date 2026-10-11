"""Predeclared ablations and cost-aware direction models for the actual evaluator."""
import gzip
import hashlib
import json
from pathlib import Path
import sys

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.audit_decision_accuracy import metrics

OUT=ROOT/'docs/研究/2026-09-27-决策因子与成本状态'
CACHE=ROOT/'.tqsdk/research/decision_factors'
SOURCE=ROOT/'.tqsdk/research/decision_accuracy/observations.json.gz'
FACTOR_NAMES=['趋势多','趋势空','动量多','动量空','量仓多','量仓空','风险多','风险空']


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def assess(rows,side):
    for r,s in zip(rows,side):r['candidate_side']=int(s)
    return {str(h):metrics(rows,h,'candidate_side') for h in (15,30)}


def read():
    with gzip.open(SOURCE,'rt',encoding='utf-8') as stream:return json.load(stream)


def cutoff(rows):
    return [np.array([r['day']< '2025-07-01' for r in rows]),
            np.array(['2025-07-01'<=r['day']<'2025-10-01' for r in rows]),
            np.array([r['day']>='2025-10-01' for r in rows])]


def cost_side(prob,classes,threshold):
    up=prob[:,list(classes).index(1)];down=prob[:,list(classes).index(-1)];flat=prob[:,list(classes).index(0)]
    chosen=np.where(up>=down,1,-1)
    return np.where((np.maximum(up,down)>=threshold)&(np.maximum(up,down)>flat),chosen,0)


def main():
    OUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    protocol=dict(source_sha256=sha(SOURCE),fine_sha256=sha(ROOT/'.tqsdk/research/fine15m/model-data.npz'),
        sources={p:sha(ROOT/p) for p in ['tools/research_decision_factors.py','tools/audit_decision_accuracy.py']},
        train_end='2025-07-01',validation_end='2025-10-01',test_end='2026-09-24',
        candidates=['all_factors','without_trend','without_momentum','without_volume','without_risk','cost_factors','cost_context'],
        classifier='StandardScaler + LogisticRegression C=1 max_iter=1000, no tuning',
        binary_target='nonflat 15-minute up/down, validation threshold matches original coverage',
        cost_target='3 classes: move up/down enough to cover 10 yuan + 2 ticks, otherwise noise; do not discard flat',
        context='8 factor scores + 35 past-only x features (drop ordinal product and quarter IDs) + 8 product one-hot fields',
        thresholds=[.5,.55,.6,.65],
        validation_gate=dict(minimum_signals=500,minimum_coverage=.15,accuracy_above_original=True,mean_net_one_lot_at_least=0,cost_hit_above_original=True),
        selection='Highest 15m accuracy among validation gate passers; tie chooses higher coverage; save selection before any test metrics.',
        posterior_history='All history already examined; this is hypothesis development, not new independent evidence',
        deployment=False)
    path=OUT/'protocol.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8'))!=protocol:raise RuntimeError('Frozen protocol differs')
    save(path,protocol)
    rows=read();train,val,test=cutoff(rows);factors=np.array([r['factors'] for r in rows]);delta=np.array([r['delta15'] for r in rows])
    train_nonflat=train&(delta!=0);labels=(delta>0).astype(int)
    products=sorted({r['product'] for r in rows});fine=np.load(ROOT/'.tqsdk/research/fine15m/model-data.npz')
    universe=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    lookup={(universe[int(s)],int(t)):i for i,(t,s) in enumerate(fine['m'][:,:2])}
    indices=np.array([lookup[(r['symbol'],r['time'])] for r in rows]);context=fine['x'][indices][:,[i for i in range(37) if i not in (21,27)]]
    codes=np.array([products.index(r['product']) for r in rows]);full=np.column_stack([factors,context,np.eye(len(products))[codes]])
    price_delta=delta*np.array([r['multiple'] for r in rows]);cost=np.array([r['cost'] for r in rows]);cost_labels=np.where(price_delta>cost,1,np.where(price_delta<-cost,-1,0))
    base_val=metrics([rows[i] for i in np.flatnonzero(val)],15)
    target=base_val['coverage'];models={};predictions={};validation={};coefs={}
    for name,columns in [('all_factors',list(range(8))),('without_trend',list(range(2,8))),
        ('without_momentum',[0,1,4,5,6,7]),('without_volume',[0,1,2,3,6,7]),('without_risk',list(range(6)))]:
        model=make_pipeline(StandardScaler(),LogisticRegression(C=1,max_iter=1000,random_state=20260927))
        model.fit(factors[train_nonflat][:,columns],labels[train_nonflat])
        prob=model.predict_proba(factors[:,columns])[:,1]
        threshold=float(np.quantile(np.maximum(prob[val],1-prob[val]),1-target))
        side=np.where(np.maximum(prob,1-prob)>=threshold,np.where(prob>=.5,1,-1),0)
        models[name]=dict(model=model,columns=columns,threshold=threshold)
        predictions[name]=side
        validation[name]=assess([rows[i] for i in np.flatnonzero(val)],side[val])
        coefs[name]={FACTOR_NAMES[c]:float(v) for c,v in zip(columns,model.named_steps['logisticregression'].coef_[0])}
    for name,features in [('cost_factors',factors),('cost_context',full)]:
        model=make_pipeline(StandardScaler(),LogisticRegression(C=1,max_iter=1000,random_state=20260927))
        model.fit(features[train],cost_labels[train]);prob=model.predict_proba(features)
        models[name]=dict(model=model,context=name=='cost_context')
        for threshold in protocol['thresholds']:
            key=f'{name}_{threshold:.2f}'
            side=cost_side(prob,model.classes_,threshold);predictions[key]=side
            validation[key]=assess([rows[i] for i in np.flatnonzero(val)],side[val])
        print('fit',name,flush=True)
    passed=[]
    for name,value in validation.items():
        m=value['15']
        if (m['signals']>=500 and m['coverage']>=.15 and m['accuracy']>base_val['accuracy'] and
            m['mean_net_one_lot']>=0 and m['cost_hurdle_hit']>base_val['cost_hurdle_hit']):passed.append(name)
    selected=max(passed,key=lambda n:(validation[n]['15']['accuracy'],validation[n]['15']['coverage'],n)) if passed else None
    selection=dict(original_validation=base_val,validation=validation,passed=passed,selected=selected,deployment=False)
    save(OUT/'validation-selection.json',selection)
    joblib.dump(dict(models=models,selected=selected,products=products),CACHE/'models.joblib')
    # Only after the validation decision is committed, disclose fixed test results.
    test_rows=[rows[i] for i in np.flatnonzero(test)]
    test_results={name:assess(test_rows,side[test]) for name,side in predictions.items()}
    result=dict(validation_selection=selection,test_original={str(h):metrics(test_rows,h) for h in (15,30)},
                test_candidates=test_results,standardized_coefficients=coefs,model_sha256=sha(CACHE/'models.joblib'),
                source_counts=dict(train=int(train.sum()),train_nonflat=int(train_nonflat.sum()),validation=int(val.sum()),test=int(test.sum())),
                diagnostics={})
    # Structural observations, train/validation only; test cannot choose filters.
    trainval=np.flatnonzero(train|val)
    for group,name in enumerate(['趋势','动量','量仓','风险']):
        difference=factors[:,2*group]-factors[:,2*group+1]
        result['diagnostics'][name]=dict(equal_fraction=float(np.mean(difference[trainval]==0)),
            positive_fraction=float(np.mean(difference[trainval]>0)),negative_fraction=float(np.mean(difference[trainval]<0)))
    differences=factors[:,:6:2]-factors[:,1:6:2]
    result['factor_gap_correlations']=np.corrcoef(differences[trainval].T).tolist()
    states={}
    training_indices=np.flatnonzero(train)
    # All cutpoints are training quantiles, not selected from outcome accuracy.
    for col,name in [(7,'15分钟ATR比例'),(18,'成本止损比'),(28,'分钟路径效率')]:
        cuts=np.quantile(fine['x'][indices[training_indices],col],[1/3,2/3]);v=fine['x'][indices,col]
        buckets=np.searchsorted(cuts,v,side='right');states[name]=dict(training_cutpoints=cuts.tolist(),validation={},test={})
        for split,mask in [('validation',val),('test',test)]:
            for bucket in range(3):
                ix=np.flatnonzero(mask&(buckets==bucket));rr=[rows[i] for i in ix]
                states[name][split][str(bucket)]=dict(original=metrics(rr,15),calibrated=assess(rr,predictions['all_factors'][ix])['15'])
    result['states']=states
    save(OUT/'results.json',result)
    np.savez_compressed(CACHE/'predictions.npz',**{k:v for k,v in predictions.items()},time=np.array([r['time'] for r in rows]),test=test)
    print(json.dumps(dict(selected=selected,validation_passed=passed,test_original=result['test_original'],test_candidates=test_results),ensure_ascii=False),flush=True)


if __name__=='__main__':main()

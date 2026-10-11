"""Second predeclared candidate: explicit costs and product identity.

The first failed candidate is preserved. This is another historical experiment,
not a pristine test and not permission to change thresholds after results.
"""
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier,HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits
import tools.research_15m_profit as base
from research.direction_model import FEATURES
from research.intraday_profit import RECIPES,outcomes,replay

OUT=ROOT/'docs/研究/2026-09-26-15分钟成本特征模型'
CACHE=base.CACHE/'cost_features'
NAMES=FEATURES+['cost_over_stop','tick_over_atr','margin_over_initial_cash','product_id']


def augment(data):
    x,m,p=data
    atr=x[:,7]*m[:,3]
    distance=np.maximum(m[:,5],np.ceil(1.5*atr/m[:,5])*m[:,5])
    costs=(10+2*m[:,5]*m[:,4])/(distance*m[:,4])
    extra=np.column_stack([costs,m[:,5]/atr,m[:,3]*m[:,4]*.15/50000,p])
    return np.column_stack([x,extra]),m,p


def fit(train,val,test,recipe):
    pnl,_,_,risk=outcomes(train[0],train[1],recipe)
    heads=[];vm=[];vw=[];tm=[];tw=[]
    settings=dict(max_iter=100,max_depth=3,max_leaf_nodes=8,min_samples_leaf=200,
                  l2_regularization=10,learning_rate=.05,early_stopping=False,
                  random_state=base.SEED,categorical_features=[21])
    for side in (0,1):
        reg=HistGradientBoostingRegressor(**settings)
        clf=HistGradientBoostingClassifier(**settings)
        reg.fit(train[0],np.clip(pnl[:,side]/risk,-4,4))
        clf.fit(train[0],pnl[:,side]>0)
        vm.append(reg.predict(val[0]));tm.append(reg.predict(test[0]))
        vw.append(clf.predict_proba(val[0])[:,1]);tw.append(clf.predict_proba(test[0])[:,1])
        heads.append((reg,clf))
    joblib.dump(dict(heads=heads,features=NAMES,recipe=recipe),CACHE/f'model-{recipe}.joblib')
    return tuple(np.column_stack(v) for v in (vm,vw,tm,tw))


def main():
    OUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    meta=json.loads((base.CACHE/'metadata.json').read_text(encoding='utf-8'))
    protocol=json.loads((base.OUT/'protocol.json').read_text(encoding='utf-8'))
    protocol.update(stage='explicit_cost_features',feature_names=NAMES,
        reason='First validation produced too few edges; gross cost and product identity were not explicit features.',
        candidate_count=18,model_parameters=dict(max_iter=100,max_depth=3,max_leaf_nodes=8,
            min_samples_leaf=200,l2_regularization=10,learning_rate=.05,early_stopping=False),
        prior_historical_evaluation_exposed=True)
    base.save_json(OUT/'protocol.json',protocol)
    print('second protocol saved',flush=True)
    train=augment(base.load('train',meta));val=augment(base.load('validation',meta));test=augment(base.load('test',meta))
    fmask=val[1][:,0]+1800000<base.HALF;smask=val[1][:,0]>=base.HALF
    fdata=tuple(v[fmask] for v in val);sdata=tuple(v[smask] for v in val)
    comparisons=[];predictions={};learned={};diagnostics=[]
    for recipe in range(3):
        vm,vw,tm,tw=fit(train,val,test,recipe)
        allowed,slots=base.filters(*fdata,vm[fmask],vw[fmask],recipe)
        learned[recipe]=(allowed,slots);predictions[recipe]=(tm,tw)
        side=vm[fmask].argmax(axis=1);ix=np.arange(fmask.sum())
        active=(vm[fmask][ix,side]>0)&(vw[fmask][ix,side]>=.5)
        diagnostics.append(dict(recipe=recipe,filter_samples=len(ix),both_positive_and_half_score=int(active.sum()),
                                allowed_products=[meta['products'][i] for i in allowed],slots=slots))
        for edge in protocol['edge_grid']:
            for floor in protocol['win_score_grid']:
                stats=replay(sdata[0],sdata[1],vm[smask],vw[smask],recipe,edge,floor,sdata[2],allowed,slots)
                record=dict(recipe=recipe,edge=edge,win_floor=floor,allowed_products=[meta['products'][i] for i in allowed],
                            slots=slots,validation=base.summary(stats))
                comparisons.append(record)
                print('cost-validation',recipe,edge,floor,base.summary(stats),flush=True)
    good=[c for c in comparisons if c['validation']['trades']>=100 and c['validation']['net']>0
          and c['validation']['max_drawdown']<=.1 and c['validation']['win_rate']>=.5]
    winner=max(good,key=lambda c:c['validation']['net']) if good else None
    choice=winner or max(comparisons,key=lambda c:(c['validation']['net'],c['validation']['trades']))
    base.save_json(OUT/'selection.json',dict(approved=bool(winner),selected=choice,comparisons=comparisons))
    recipe=choice['recipe'];mean,win=predictions[recipe];allowed,slots=learned[recipe]
    stats=replay(test[0],test[1],mean,win,recipe,choice['edge'],choice['win_floor'],test[2],allowed,slots)
    stress={str(slip):base.summary(replay(test[0],test[1],mean,win,recipe,choice['edge'],choice['win_floor'],
              test[2],allowed,slots,slippage=slip)) for slip in (0,1,2)}
    old=joblib.load(ROOT/'.tqsdk/research/models/15m.joblib')['model']
    probability=old.predict_proba(test[0][:,:18])[:,1]
    old_mean=np.column_stack([probability,1-probability])
    baseline=replay(test[0],test[1],old_mean,np.ones_like(old_mean),recipe,0,0,test[2],range(80),range(24))
    result=dict(protocol=protocol,metadata=meta,training_samples=len(train[0]),validation_samples=len(val[0]),
        historical_samples=len(test[0]),approved_by_validation=bool(winner),selection=choice,
        historical=base.summary(stats),slippage_stress=stress,old_direction_baseline=base.summary(baseline),
        filter_diagnostics=diagnostics,
        code_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ['research/intraday_profit.py','tools/research_15m_profit.py','tools/research_15m_cost_features.py']},
        model_sha256=hashlib.sha256((CACHE/f'model-{recipe}.joblib').read_bytes()).hexdigest())
    base.save_json(OUT/'results.json',result)
    np.savez_compressed(CACHE/'chosen-evaluation.npz',x=test[0],m=test[1],products=test[2],mean=mean,win=win)
    with (OUT/'成交明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
        for row in stats['ledger']:
            row=row.copy();row[2]=meta['symbols'][row[2]];row[3]=meta['products'][row[3]];w.writerow(row)
    base.OUT=OUT;base.write_report(result)
    path=OUT/'研究报告.md';text=path.read_text(encoding='utf-8')
    text=text.replace('# 15分钟净收益模型研究','# 15分钟成本特征模型研究')
    text=text.replace('## 模型与交易规则','## 本轮新增特征\n\n在前一轮失败结果上，补入当时可知的双边成本/止损空间、最小跳动/ATR、名义保证金/初始资金、品种分类特征。三组规则和18组阈值网格保持预先固定；原失败结果单独保留。新增品种没有旧训练样本时，品种分类只能视作未知类别，不能保证外推。\n\n## 模型与交易规则')
    text=text.replace('.tqsdk/research/profit15m中','.tqsdk/research/profit15m/cost_features中')
    text=text.replace('python tools/research_15m_profit.py','python tools/research_15m_cost_features.py（需先运行上一轮生成事件缓存）')
    path.write_text(text,encoding='utf-8')
    print('cost-feature finished',base.summary(stats),'approved',bool(winner),flush=True)


if __name__=='__main__':
    with threadpool_limits(limits=4):main()

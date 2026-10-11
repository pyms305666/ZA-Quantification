"""Frozen paired comparison: identical minute fills, baseline vs minute features."""
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
from sklearn.ensemble import HistGradientBoostingRegressor,HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from threadpoolctl import threadpool_limits
from research.minute_intraday import FEATURE_NAMES,outcomes,replay
from tools.research_conditional_15m import SETTINGS,timestamp,logit
from tools.research_15m_profit import save_json,summary

CACHE=ROOT/'.tqsdk/research/fine15m'
OUT=ROOT/'docs/研究/2026-09-26-分钟路径模型'


def protocol():
    OUT.mkdir(parents=True,exist_ok=True)
    source=json.loads((ROOT/'docs/研究/2026-09-26-条件净收益模型-低成本品种/protocol.json').read_text(encoding='utf-8'))
    result=dict(source,study='paired baseline/minute features with 1-minute execution',
        new_features=FEATURE_NAMES,candidate_count=36,paired_features=[28,37],
        preserve_source_model_settings=True,source_bars_must_match=True,
        actual_tick_probe_only=True,tick_features_not_yet_in_model=True,
        volume_sign_feature_is_proxy_not_aggressor_flow=True)
    result['profit_probability_grid']=[0,.5,.55]
    result['approval']=dict(min_trades=100,net_positive=True,min_profit_factor=1.2,max_drawdown=.1)
    result['additional_mean_only_comparison']=dict(added_before_minute_model_fit=True,original_24_cases_retained=True,
        reason='Positive expected net payoff does not mathematically require win probability >=50%; add 12 mean-only cases.',
        win_rate_always_reported=True)
    save_json(OUT/'protocol.json',result)
    return result


def load_parts():
    d=np.load(CACHE/'model-data.npz');m=d['m'];a,b,c=[timestamp(s) for s in ['2025-07-01','2025-08-01','2025-09-26']]
    masks=[m[:,0]+1800000<a,(m[:,0]>=a)&(m[:,0]+1800000<b),
        (m[:,0]>=b)&(m[:,0]+1800000<c),m[:,0]>=c]
    return [tuple(d[key][mask] for key in ('x','m','p','future')) for mask in masks]


def predictions(model,x):
    means=[];probs=[]
    x=x[:,:model['dimensions']]
    for head in model['heads']:
        means.append(head['reg'].predict(x)+head['bias'])
        probs.append(head['cal'].predict_proba(logit(head['clf'].predict_proba(x)[:,1]))[:,1])
    return np.column_stack(means),np.column_stack(probs)


def main(resume=False):
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    parts=load_parts();train,cal,val,test=parts
    weights=np.exp(-np.log(2)*(timestamp('2025-07-01')-train[1][:,0])/86400000/365)
    comparisons=[];scores={};diagnostics=[]
    for name,dimensions in [('baseline',28),('minute',37)]:
        for horizon in (1,2):
            y,_,_,risk=outcomes(train[0],train[1],train[3],horizon)
            cy,_,_,crisk=outcomes(cal[0],cal[1],cal[3],horizon)
            vy,_,_,_=outcomes(val[0],val[1],val[3],horizon)
            heads=[]
            saved_model=CACHE/f'{name}-{horizon}.joblib'
            if resume and saved_model.exists():
                heads=joblib.load(saved_model)['heads']
            for side in (() if heads else (0,1)):
                reg=HistGradientBoostingRegressor(**SETTINGS);clf=HistGradientBoostingClassifier(**SETTINGS)
                reg.fit(train[0][:,:dimensions],y[:,side]/risk,sample_weight=weights)
                clf.fit(train[0][:,:dimensions],y[:,side]>0,sample_weight=weights)
                calibrator=LogisticRegression(C=1,max_iter=1000)
                calibrator.fit(logit(clf.predict_proba(cal[0][:,:dimensions])[:,1]),cy[:,side]>0)
                bias=float(np.mean(cy[:,side]/crisk-reg.predict(cal[0][:,:dimensions])))
                heads.append(dict(reg=reg,clf=clf,cal=calibrator,bias=bias))
                print('minute fit',name,horizon,side,flush=True)
            model=dict(heads=heads,dimensions=dimensions,horizon=horizon,protocol=cfg)
            joblib.dump(model,CACHE/f'{name}-{horizon}.joblib')
            for split,d in [('validation',val),('test',test)]:
                score=predictions(model,d[0]);scores[name,horizon,split]=score
                np.savez_compressed(CACHE/f'{name}-{horizon}-{split}.npz',mean=score[0],prob=score[1])
            mean,prob=scores[name,horizon,'validation']
            diagnostics.append(dict(model=name,horizon=horizon,
                validation_brier=[float(brier_score_loss(vy[:,s]>0,prob[:,s])) for s in (0,1)],
                max_probability=prob.max(axis=0).tolist(),max_expected_R=mean.max(axis=0).tolist()))
            for edge in cfg['edge_grid']:
                for floor in cfg['profit_probability_grid']:
                    stats=summary(replay(*val,mean,prob,horizon,edge,floor))
                    comparisons.append(dict(model=name,horizon=horizon,edge=edge,floor=floor,validation=stats))
            print('minute validation',name,horizon,comparisons[-9:],flush=True)
    selections={}
    for name in ('baseline','minute'):
        cases=[c for c in comparisons if c['model']==name]
        approved=[c for c in cases if c['validation']['trades']>=100 and c['validation']['net']>0
            and (c['validation']['profit_factor'] or 0)>=1.2 and c['validation']['max_drawdown']<=.1]
        diagnostic=[c for c in cases if c['validation']['trades']>=20] or cases
        selections[name]=dict(approved=bool(approved),choice=max(approved or diagnostic,key=lambda c:c['validation']['net']))
    save_json(OUT/'selection.json',dict(selections=selections,comparisons=comparisons))
    result={}
    symbols=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']
    for name,selection in selections.items():
        c=selection['choice'];mean,prob=scores[name,c['horizon'],'test']
        stats=replay(*test,mean,prob,c['horizon'],c['edge'],c['floor'])
        stress={f'fee{fee}_slip{slip}':summary(replay(*test,mean,prob,c['horizon'],c['edge'],c['floor'],fee,slip))
            for fee,slip in [(0,0),(10,0),(10,1),(10,2)]}
        with (OUT/f'{name}-成交.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
            for row in stats['ledger']:
                row=row.copy();row[2]=symbols['symbols'][row[2]];row[3]=symbols['products'][row[3]];writer.writerow(row)
        result[name]=dict(selection=selection,historical=summary(stats),cost_stress=stress)
        print('minute historical',name,summary(stats),flush=True)
    # Re-evaluate the previous frozen policy with minute paths on matched rows.
    previous=ROOT/'.tqsdk/research/conditional15m_low_cost'
    old=np.load(previous/'data.npz');old_m=old['m'];ix=np.flatnonzero(old_m[:,0]>=timestamp('2025-09-26'))
    lookup={(int(old_m[i,0]),int(old_m[i,1])):j for j,i in enumerate(ix)}
    choice=json.loads((ROOT/'docs/研究/2026-09-26-条件净收益模型-低成本品种/results.json').read_text(encoding='utf-8'))['choice']
    saved=np.load(previous/f"pred-{choice['horizon']}-test.npz")
    match=np.array([lookup[int(row[0]),int(row[1])] for row in test[1]])
    old_stats=summary(replay(*test,saved['mean'][match],saved['prob'][match],choice['horizon'],choice['edge'],choice['floor']))
    payload=dict(protocol=cfg,counts=dict(zip(('train','calibration','validation','historical'),[len(d[0]) for d in parts])),
        results=result,diagnostics=diagnostics,previous_frozen_policy_minute_replay=old_stats,
        audit=json.loads((OUT/'data-audit.json').read_text(encoding='utf-8')),
        code_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
            ['research/minute_intraday.py','tools/prepare_minute_model.py','tools/research_minute_15m.py']},
        model_sha256={f'{n}-{h}':hashlib.sha256((CACHE/f'{n}-{h}.joblib').read_bytes()).hexdigest()
            for n in ('baseline','minute') for h in (1,2)})
    save_json(OUT/'results.json',payload)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--declare',action='store_true');parser.add_argument('--resume',action='store_true');args=parser.parse_args()
    with threadpool_limits(limits=4):
        if args.declare:protocol()
        else:main(args.resume)

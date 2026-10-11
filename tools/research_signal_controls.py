"""One-lot attribution, stratified feasible random controls and fixed Ridge baseline."""
import argparse
import csv
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder,StandardScaler
from threadpoolctl import threadpool_limits
from research.minute_intraday import outcomes,replay
from tools.research_rolling_15m import SOURCE,load,digest,partitions,calibrate,adjusted
from tools.research_15m_profit import save_json,summary

OUT=ROOT/'docs/研究/2026-09-27-信号优势与线性对照'
CACHE=ROOT/'.tqsdk/research/signal_controls'
GATED=ROOT/'.tqsdk/research/gated15m'
GATED_OUT=ROOT/'docs/研究/2026-09-27-有条件替换模型'
ARMS=['reference','frozen_anchor','conditional','agreement','ridge_margin']

def declare():
    OUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    if (OUT/'protocol.json').exists():raise RuntimeError('Protocol already exists; do not overwrite')
    cfg=dict(declared_at_utc=pd.Timestamp.now(tz='UTC').isoformat(),arms=ARMS,
        date_start='2025-10-01',date_end='2026-09-24',history_previously_exposed=True,
        fixed_horizon_minutes=15,ridge_alpha=100.,ridge_parameter_search=False,
        ridge_numeric_scaling='unweighted training-only StandardScaler',categorical_columns=[21,27],
        ridge_training='same 24-month train, 1-month calibration, 365-day half-life as rolling tree',
        ridge_calibration='same training-score bins and day-cluster 1.645 SE margin',
        one_lot_respects_original_risk_and_margin=True,original_ledger_divide_by_lots_separately_reported=True,
        random_replicates=1000,random_max_attempts=100000,random_seed=20260927,
        random_strata=['month','product','15-minute clock slot','model-preferred direction'],
        random_pool='all one-lot initially affordable low-cost opportunities, including retained observations',
        random_draw='uniform without replacement within strata; reject overlapping positions or cash/risk infeasibility',
        random_exact_trade_count=True,random_results_are_descriptive_not_independent_proof=True,
        filtered_signal_statistics='per opportunity, not simultaneous portfolio returns',
        deployment_authorization='existing conditional authorization retained; no software replacement on this study alone',
        source_sha256=digest(SOURCE),gated_results_sha256=digest(GATED_OUT/'results.json'),
        dependency_sha256={p:digest(ROOT/p) for p in ['research/minute_intraday.py','tools/research_rolling_15m.py']})
    save_json(OUT/'protocol.json',cfg);print('Declared five fixed policies, one Ridge alpha and 1000 matched controls',flush=True)

def signature():
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    assert digest(SOURCE)==cfg['source_sha256'] and digest(GATED_OUT/'results.json')==cfg['gated_results_sha256']
    for path,sha in cfg['dependency_sha256'].items():assert digest(ROOT/path)==sha
    return dict(protocol=digest(OUT/'protocol.json'),code=digest(Path(__file__)),source=digest(SOURCE))

def fit():
    sig=signature();data=load();x,m,p,future=data;y,_,_,risk=outcomes(x,m,future,1);target=y/risk[:,None]
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    for month,tr,ca,te,bounds in partitions(m):
        path=CACHE/f'ridge-{month}.joblib'
        if path.exists():assert joblib.load(path)['signature']==sig;continue
        numeric=[i for i in range(x.shape[1]) if i not in (21,27)]
        prep=ColumnTransformer([('numeric',StandardScaler(),numeric),('category',OneHotEncoder(handle_unknown='ignore',sparse_output=False),[21,27])])
        model=Pipeline([('prep',prep),('reg',Ridge(alpha=cfg['ridge_alpha']))])
        weights=np.exp(-np.log(2)*(bounds[1]-m[tr,0])/86400000/365)
        model.fit(x[tr],target[tr],reg__sample_weight=weights)
        train_prediction=model.predict(x[tr]);cal_prediction=model.predict(x[ca]);raw=model.predict(x[te])
        days=((m[ca,0]+28800000)//86400000).astype(int);cal=[];scores=[]
        for side in (0,1):
            c=calibrate(train_prediction[:,side],cal_prediction[:,side],target[ca,side],days);cal.append(c)
            scores.append(adjusted(raw[:,side],c)[2])
        joblib.dump(dict(signature=sig,model=model,calibration=cal,score=np.column_stack(scores),
            train_indices=tr,calibration_indices=ca,evaluation_indices=te,bounds=bounds),path)
        print('ridge fit',month,len(tr),len(ca),len(te),flush=True)

def context():
    data=load();indices=[];scores={a:[] for a in ARMS};expected=json.loads((GATED_OUT/'results.json').read_text(encoding='utf-8'))
    for month,_,_,te,_ in partitions(data[1]):
        path=GATED/f'{month}-scores.npz';assert digest(path)==expected['score_sha256'][path.name]
        previous=np.load(path);np.testing.assert_array_equal(previous['indices'],te)
        ridge=joblib.load(CACHE/f'ridge-{month}.joblib');assert ridge['signature']==signature()
        np.testing.assert_array_equal(ridge['evaluation_indices'],te)
        indices.append(te)
        for arm in ARMS:scores[arm].append(ridge['score'] if arm=='ridge_margin' else previous[arm])
    ix=np.concatenate(indices);d=tuple(a[ix] for a in data);scores={k:np.concatenate(v) for k,v in scores.items()}
    pnl,ends,worst,risk=outcomes(d[0],d[1],d[3],1);m=d[1]
    distance=np.maximum(m[:,5],np.ceil(1.5*d[0][:,7]*m[:,3]/m[:,5])*m[:,5])
    low_cost=(10+2*m[:,5]*m[:,4])/(distance*m[:,4])<=.35
    margin=d[3][:,0,0]*m[:,4]*.15
    return d,scores,pnl,ends,worst,risk,low_cost,margin

def selected_history(d,pnl,ends,worst,risk,margin,indices,sides,strict=False):
    cash=50000.;peak=cash;dd=0.;locked=-1;ledger=[]
    for i in indices:
        side=int(sides[i])
        if d[1][i,0]<locked or risk[i]>min(900,cash*.018) or margin[i]>cash:
            if strict:return None
            continue
        before=cash;dd=max(dd,(peak-cash-worst[i,side])/peak);cash+=pnl[i,side]
        peak=max(peak,cash);dd=max(dd,(peak-cash)/peak);locked=ends[i,side]
        ledger.append([int(i),int(d[1][i,0]),int(locked),side,float(pnl[i,side]),cash,before])
    profits=np.array([r[4] for r in ledger]);loss=-profits[profits<0].sum()
    return dict(trades=len(ledger),net=float(cash-50000),max_drawdown=float(dd),win_rate=float(np.mean(profits>0)) if len(profits) else None,
        profit_factor=float(profits[profits>0].sum()/loss) if loss else None,ledger=ledger)

def run_one_lot(c,score):
    d,_,pnl,ends,worst,risk,low_cost,margin=c
    side=score.argmax(axis=1);value=score[np.arange(len(score)),side]
    eligible=(value>=0)&low_cost;order=np.lexsort((-value,d[1][:,0]));order=order[eligible[order]]
    s=selected_history(d,pnl,ends,worst,risk,margin,order,side)
    s.update(coverage=float(eligible.mean()),eligible=int(eligible.sum()));return s,side,eligible

def group_stats(values):
    return dict(opportunities=len(values),mean_net_per_lot=float(values.mean()) if len(values) else None,
        net_win_rate=float(np.mean(values>0)) if len(values) else None)

def matched_controls(c,score,base_score,observed,seed):
    d,_,pnl,ends,worst,risk,low_cost,margin=c;m=d[1];n=len(m);side=base_score.argmax(axis=1)
    finite=np.isfinite(base_score).any(axis=1)
    affordable=finite&low_cost&(risk<=900)&(margin<=50000)
    month=pd.to_datetime(m[:,0],unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m').to_numpy()
    slot=((m[:,0]+28800000)%86400000//900000).astype(int)
    keys=[(month[i],int(d[2][i]),int(slot[i]),int(side[i])) for i in range(n)]
    pools={};counts={}
    for i in np.flatnonzero(affordable):pools.setdefault(keys[i],[]).append(int(i))
    for row in observed['ledger']:
        i=row[0];assert row[3]==side[i];counts[keys[i]]=counts.get(keys[i],0)+1
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'));rng=np.random.default_rng(seed)
    cells=[(np.array(pools[k],dtype=int),count) for k,count in sorted(counts.items())]
    assert all(len(pool)>=count for pool,count in cells)
    if not cells:return dict(replicates=0,attempts=0,reason='no executed trades; no random comparison'),np.empty((0,0),int),np.empty(0)
    samples=[];nets=[];attempts=0
    while len(samples)<cfg['random_replicates'] and attempts<cfg['random_max_attempts']:
        attempts+=1;chosen=np.concatenate([rng.choice(pool,size=count,replace=False) for pool,count in cells])
        chosen=chosen[np.argsort(m[chosen,0],kind='stable')]
        # All starts lie on the 15-minute grid, with exits no later than the next grid point.
        if len(np.unique(m[chosen,0]))<len(chosen):continue
        result=selected_history(d,pnl,ends,worst,risk,margin,chosen,side,strict=True)
        if result is None:continue
        assert result['trades']==observed['trades']
        samples.append(chosen);nets.append(result['net'])
    v=np.array(nets);info=dict(replicates=len(v),attempts=attempts,strata=len(cells),
        strata_with_no_alternative=sum(len(pool)==count for pool,count in cells),
        observations_in_no_alternative_strata=sum(count for pool,count in cells if len(pool)==count),
        random_median_net=float(np.median(v)) if len(v) else None,
        random_5_95_net=np.quantile(v,[.05,.95]).tolist() if len(v) else None,
        fraction_random_at_least_observed=float((1+np.sum(v>=observed['net']))/(len(v)+1)) if len(v) else None,
        observed_net=observed['net'],matching='month/product/clock-quarter/model-side, exact count and feasible single account')
    return info,np.array(samples,dtype=int),v

def run():
    sig=signature();c=context();d,scores,pnl,ends,worst,risk,low_cost,margin=c;records=[]
    names=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    for j,arm in enumerate(ARMS):
        score=scores[arm];one,side,eligible=run_one_lot(c,score)
        base=scores['conditional'] if arm=='agreement' else score;base_side=base.argmax(axis=1)
        affordable=low_cost&(risk<=900)&(margin<=50000)&np.isfinite(base).any(axis=1)
        values=pnl[np.arange(len(pnl)),base_side]
        retained=affordable&eligible;removed=affordable&~eligible
        signal=dict(retained=group_stats(values[retained]),filtered=group_stats(values[removed]),all=group_stats(values[affordable]))
        dynamic=replay(*d,score,np.ones_like(score),1,0,0)
        same_ledger_one_lot=sum(r[6]/r[5] for r in dynamic['ledger'])
        diagnostic=np.array([r[4] for r in one['ledger']]);top3=float(np.sort(diagnostic)[-3:].sum()) if len(diagnostic) else 0.
        info,draws,nets=matched_controls(c,score,base,one,cfg['random_seed']+j)
        np.savez_compressed(CACHE/f'{arm}-random.npz',indices=draws,nets=nets)
        with (OUT/f'{arm}-一手成交.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['data_index','entry_ms','exit_ms','symbol','direction','net','balance','prior_balance'])
            for i,start,end,s,net,balance,before in one['ledger']:writer.writerow([i,start,end,names[int(d[1][i,1])],1 if s==0 else -1,net,balance,before])
        rec=dict(arm=arm,dynamic=summary(dynamic),original_trades_rescaled_to_one_lot_net=float(same_ledger_one_lot),
            one_lot=summary(one),signal_groups=signal,matched_random=info,
            net_without_top3=float(one['net']-top3),top3_profit=top3)
        records.append(rec);print('control',arm,summary(one),info,flush=True)
    save_json(OUT/'results.json',dict(signature=sig,protocol=cfg,results=records,
        files_sha256={p.name:digest(p) for p in CACHE.glob('*') if p.is_file()},
        deployment_allowed=False,reason='Small exposed historical research is not sufficient for software replacement; prior requirements remain.'))

def verify():
    r=json.loads((OUT/'results.json').read_text(encoding='utf-8'));assert signature()==r['signature']
    for name,sha in r['files_sha256'].items():assert digest(CACHE/name)==sha
    data=load();x,m,_,future=data;y,_,_,risk=outcomes(x,m,future,1)
    for month,tr,ca,te,bounds in partitions(m):
        f=joblib.load(CACHE/f'ridge-{month}.joblib')
        for key,expected in [('train_indices',tr),('calibration_indices',ca),('evaluation_indices',te)]:np.testing.assert_array_equal(f[key],expected)
        assert m[tr,0].max()+1800000<bounds[1] and m[ca,0].max()+1800000<bounds[2]
        scaler=f['model'].named_steps['prep'].named_transformers_['numeric'];columns=[i for i in range(37) if i not in (21,27)]
        np.testing.assert_allclose(scaler.mean_,x[tr][:,columns].mean(axis=0),rtol=1e-12,atol=1e-12)
        raw=f['model'].predict(x[te]);train=f['model'].predict(x[tr]);cal=f['model'].predict(x[ca]);days=((m[ca,0]+28800000)//86400000).astype(int)
        for side in (0,1):
            expected=calibrate(train[:,side],cal[:,side],y[ca,side]/risk[ca],days);assert expected==f['calibration'][side]
            np.testing.assert_allclose(adjusted(raw[:,side],expected)[2],f['score'][:,side],rtol=0,atol=1e-12)
    c=context();d,scores,pnl,ends,worst,risk,low_cost,margin=c;m=d[1]
    assert np.all(m[:,0]%900000==0)
    months=pd.to_datetime(m[:,0],unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m').to_numpy()
    slot=((m[:,0]+28800000)%86400000//900000).astype(int)
    for record in r['results']:
        arm=record['arm'];one,side,eligible=run_one_lot(c,scores[arm]);assert summary(one)==record['one_lot']
        base=scores['conditional'] if arm=='agreement' else scores[arm];base_side=base.argmax(axis=1)
        def histogram(ix):
            result={}
            for i in ix:
                key=(months[i],int(d[2][i]),int(slot[i]),int(base_side[i]));result[key]=result.get(key,0)+1
            return result
        observed=histogram([a[0] for a in one['ledger']]);z=np.load(CACHE/f'{arm}-random.npz')
        for sample,net in zip(z['indices'],z['nets']):
            assert len(sample)==one['trades'] and len(np.unique(sample))==len(sample)
            assert histogram(sample)==observed
            assert np.all(low_cost[sample]) and np.all(risk[sample]<=900) and np.all(margin[sample]<=50000)
            cash=50000.;locked=-1
            for i in sample:
                s=base_side[i];assert m[i,0]>=locked and risk[i]<=min(900,cash*.018) and margin[i]<=cash
                cash+=pnl[i,s];locked=ends[i,s]
            assert abs(cash-50000-net)<1e-7
        for i,start,end,s,net,balance,before in one['ledger']:
            assert 0<end-start<=900000 and (start+28800000)//86400000==(end+28800000)//86400000
            assert net==pnl[i,s] and balance==before+net
    save_json(OUT/'核验记录.json',dict(passed=True,checks=['all data/code/model/control fingerprints',
        'all Ridge scalers/encoders fit in past; label boundaries and saved predictions/calibration reproduce',
        'all one-lot ledgers replay, cash/holding-period checks',
        'every random replicate has exact month/product/time/direction counts, no repeated or overlapping positions, original risk/margin feasibility and reconstructed cash'],
        limitation='Matched historical controls are descriptive and do not remove prior research selection bias.'))
    print('All one-lot, Ridge and matched-control checks passed',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['declare','fit','run','verify']);args=parser.parse_args()
    with threadpool_limits(limits=4):globals()[args.mode]()

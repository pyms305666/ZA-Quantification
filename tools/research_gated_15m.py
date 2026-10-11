"""Chronological model replacement gates; research only, no trading/runtime writes."""
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
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits
from research.minute_intraday import outcomes,replay
from tools.research_15m_profit import save_json,summary
from tools.research_rolling_15m import load,digest,calibrate,adjusted,SETTINGS,SOURCE

CACHE=ROOT/'.tqsdk/research/gated15m'
OUT=ROOT/'docs/研究/2026-09-27-有条件替换模型'
ROLLING=ROOT/'.tqsdk/research/rolling15m'
PRIOR=ROOT/'docs/研究/2026-09-27-滚动训练与误差余量'
ARMS=['reference','frozen_anchor','conditional','agreement']

def stamps(month):
    start=pd.Timestamp(month+'-01',tz='Asia/Shanghai')
    return [int(t.timestamp()*1000) for t in (start-pd.DateOffset(months=25),start-pd.DateOffset(months=1),start,start+pd.DateOffset(months=1))]

def month_indices(m,month):
    a,b,c,e=stamps(month);e=min(e,int(pd.Timestamp('2026-09-25',tz='Asia/Shanghai').timestamp()*1000));t=m[:,0]
    return np.flatnonzero((t>=c)&(t+1800000<e))

def declare():
    OUT.mkdir(parents=True,exist_ok=True);(CACHE/'models').mkdir(parents=True,exist_ok=True)
    (OUT/'decisions').mkdir(exist_ok=True)
    if (OUT/'protocol.json').exists():raise RuntimeError('Refusing to overwrite declared protocol')
    previous=json.loads((PRIOR/'results.json').read_text(encoding='utf-8'))
    ref=next(r for r in previous['results'] if r['arm']=='rolling_margin' and r['horizon']==1)
    source_files=['research/minute_intraday.py','tools/research_rolling_15m.py','tools/research_conditional_15m.py']
    runtime=[p for folder in ('market','mobile-app/app/src/main/python/market') for p in (ROOT/folder).glob('*.py')]
    cfg=dict(declared_at_utc=pd.Timestamp.now(tz='UTC').isoformat(),arms=ARMS,
        historical_start='2025-10-01',historical_end='2026-09-24',history_previously_exposed=True,
        horizon_minutes=15,training_months=24,calibration_months=1,gate_validation_months=2,
        initial_anchor='2025-08',minimum_gate_trades=20,minimum_gate_profit_factor=1.2,
        gate_both_months_positive=True,gate_net_not_below_incumbent=True,
        gate_drawdown_not_above_incumbent_when_incumbent_trades_at_least=20,
        maximum_drawdown=ref['historical']['max_drawdown'],
        historical_screen=dict(min_trades=100,net_positive=True,min_profit_factor=1.2,
            min_positive_months=8,net_not_below_reference=True,drawdown_not_above_reference=True,
            stress_two_tick_net_nonnegative=True),
        deployment_requires=dict(fresh_forward_trading_days=60,fresh_forward_trades=100,
            forward_net_positive=True,forward_profit_factor_min=1.2,
            forward_drawdown_cap=ref['historical']['max_drawdown'],
            timestamped_predictions_before_outcomes=True,real_quote_execution_audit=True,
            desktop_and_mobile_integration_tests=True),
        fees_and_risk_unchanged=True,source_sha256=digest(SOURCE),
        dependency_sha256={p:digest(ROOT/p) for p in source_files},
        runtime_sha256={p.relative_to(ROOT).as_posix():digest(p) for p in runtime},
        prior_results_sha256=digest(PRIOR/'results.json'))
    save_json(OUT/'protocol.json',cfg);print('Declared 4 policies and deployment requirements',flush=True)

def signature():
    cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    assert digest(SOURCE)==cfg['source_sha256']
    for p,sha in cfg['dependency_sha256'].items():assert digest(ROOT/p)==sha
    return dict(protocol=digest(OUT/'protocol.json'),code=digest(Path(__file__)),source=digest(SOURCE))

def get_model(key,data,sig):
    path=CACHE/'models'/f'{key}.joblib'
    if path.exists():
        saved=joblib.load(path);assert saved['signature']==sig;return saved
    x,m,p,future=data;a,b,c,_=stamps(key);t=m[:,0]
    tr=np.flatnonzero((t>=a)&(t+1800000<b));ca=np.flatnonzero((t>=b)&(t+1800000<c))
    prior_path=ROLLING/f'{key}-1.joblib'
    if prior_path.exists():
        old=json.loads((PRIOR/'results.json').read_text(encoding='utf-8'))
        assert digest(prior_path)==old['folds_sha256'][prior_path.name]
        f=joblib.load(prior_path)
        np.testing.assert_array_equal(tr,f['train_indices']);np.testing.assert_array_equal(ca,f['calibration_indices'])
        heads=f['heads'];source=f'reused {prior_path.name}'
    else:
        y,_,_,risk=outcomes(x,m,future,1);target=y/risk[:,None];heads=[]
        weights=np.exp(-np.log(2)*(b-m[tr,0])/86400000/365)
        days=((m[ca,0]+28800000)//86400000).astype(int)
        for side in (0,1):
            reg=HistGradientBoostingRegressor(**SETTINGS);reg.fit(x[tr],target[tr,side],sample_weight=weights)
            cal=calibrate(reg.predict(x[tr]),reg.predict(x[ca]),target[ca,side],days)
            heads.append(dict(model=reg,calibration=cal))
        source='new fit, no validation-window labels used'
    saved=dict(key=key,signature=sig,heads=heads,train_indices=tr,calibration_indices=ca,bounds=[a,b,c],source=source)
    joblib.dump(saved,path);print('model ready',key,source,flush=True);return saved

def score(model,x):
    return np.column_stack([adjusted(h['model'].predict(x),h['calibration'])[2] for h in model['heads']])

def agree(inc,recent):
    same=inc.argmax(axis=1)==recent.argmax(axis=1);side=inc.argmax(axis=1)
    result=np.full_like(inc,-np.inf);i=np.arange(len(inc));valid=same&(inc[i,side]>=0)&(recent[i,side]>=0)
    result[i[valid],side[valid]]=np.minimum(inc[i[valid],side[valid]],recent[i[valid],side[valid]])
    return result

def replay_score(data,scores,fee=10,slip=1):return replay(*data,scores,np.ones_like(scores),1,0,0,fee,slip)

def gate(candidate,incumbent,months,cfg):
    rules=dict(enough_trades=candidate['trades']>=cfg['minimum_gate_trades'],
        net_positive=candidate['net']>0,profit_factor=(candidate['profit_factor'] or 0)>=cfg['minimum_gate_profit_factor'],
        drawdown_cap=candidate['max_drawdown']<=cfg['maximum_drawdown'],
        not_worse_net=candidate['net']>=incumbent['net'],
        not_worse_drawdown=(incumbent['trades']<20 or candidate['max_drawdown']<=incumbent['max_drawdown']),
        both_months_positive=len(months)==2 and all(q>0 for q in months))
    return bool(all(rules.values())),rules

def run():
    sig=signature();cfg=json.loads((OUT/'protocol.json').read_text(encoding='utf-8'));data=load();x,m,_,_=data
    anchor=get_model('2025-08',data,sig);incumbent=anchor;all_ix=[];scores={a:[] for a in ARMS};decisions=[]
    for time in pd.date_range('2025-10-01','2026-09-01',freq='MS',tz='Asia/Shanghai'):
        month=time.strftime('%Y-%m');key=(time-pd.DateOffset(months=2)).strftime('%Y-%m');candidate=get_model(key,data,sig)
        lo=stamps(key)[2];hi=stamps(month)[2];va=np.flatnonzero((m[:,0]>=lo)&(m[:,0]+1800000<hi));vd=tuple(a[va] for a in data)
        assert candidate['bounds'][2]<=lo and incumbent['bounds'][2]<=lo
        cs=score(candidate,x[va]);old=score(incumbent,x[va]);c=replay_score(vd,cs);i=replay_score(vd,old)
        mid=stamps((time-pd.DateOffset(months=1)).strftime('%Y-%m'))[2]
        monthly=[sum(row[6] for row in c['ledger'] if start<=row[0]<end) for start,end in [(lo,mid),(mid,hi)]]
        accepted,rules=gate(c,i,monthly,cfg);before=incumbent['key']
        if accepted:incumbent=candidate
        record=dict(month=month,validation_start_ms=lo,validation_end_ms=hi,candidate=key,
            incumbent_before=before,selected=incumbent['key'],gate_passed=accepted,
            actually_changed=before!=incumbent['key'],rules=rules,
            candidate_validation=summary(c),incumbent_validation=summary(i),candidate_validation_monthly_net=monthly)
        # Persist selection before touching this month's realized strategy outcomes.
        save_json(OUT/'decisions'/f'{month}.json',record);decisions.append(record)
        te=month_indices(m,month);recent=get_model(month,data,sig);now=score(incumbent,x[te]);latest=score(recent,x[te])
        fold=dict(reference=latest,frozen_anchor=score(anchor,x[te]),conditional=now,agreement=agree(now,latest))
        np.savez_compressed(CACHE/f'{month}-scores.npz',indices=te,**fold)
        all_ix.append(te)
        for arm in ARMS:scores[arm].append(fold[arm])
        print('gate',month,'accepted',accepted,'selected',incumbent['key'],'failed',[k for k,v in rules.items() if not v],flush=True)
    ix=np.concatenate(all_ix);evaluation=tuple(a[ix] for a in data);results=[]
    names=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    for arm in ARMS:
        prediction=np.concatenate(scores[arm]);s=replay_score(evaluation,prediction);months=[]
        for record in decisions:
            lo=stamps(record['month'])[2];hi=stamps(record['month'])[3];rows=[r for r in s['ledger'] if lo<=r[0]<hi]
            months.append(dict(month=record['month'],net=sum(r[6] for r in rows),trades=len(rows)))
        stress=summary(replay_score(evaluation,prediction,10,2))
        with (OUT/f'{arm}-成交.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['entry_ms','exit_ms','symbol','product','direction','lots','net','balance','prior_balance'])
            for row in s['ledger']:
                row=row.copy();row[2]=names[row[2]];writer.writerow(row)
        results.append(dict(arm=arm,historical=summary(s),months=months,positive_months=sum(q['net']>0 for q in months),stress_two_ticks=stress))
        print('result',arm,summary(s),flush=True)
    reference=results[0]['historical']
    prior=json.loads((PRIOR/'results.json').read_text(encoding='utf-8'))
    expected=next(q for q in prior['results'] if q['arm']=='rolling_margin' and q['horizon']==1)['historical'];assert reference==expected
    for r in results:
        s=r['historical'];r['historical_checks']=dict(enough_trades=s['trades']>=100,net_positive=s['net']>0,
            profit_factor=(s['profit_factor'] or 0)>=1.2,drawdown_cap=s['max_drawdown']<=reference['max_drawdown'],
            net_not_below_reference=s['net']>=reference['net'],positive_months=r['positive_months']>=8,
            stress_two_ticks_nonnegative=r['stress_two_ticks']['net']>=0)
        r['historical_passed']=all(r['historical_checks'].values())
    save_json(OUT/'results.json',dict(protocol=cfg,signature=sig,results=results,decisions=decisions,
        model_sha256={p.name:digest(p) for p in sorted((CACHE/'models').glob('*.joblib'))},
        score_sha256={p.name:digest(p) for p in sorted(CACHE.glob('*-scores.npz'))}))
    # Historical evidence alone is never sufficient for deployment.
    save_json(OUT/'deployment-decision.json',dict(replace_runtime_algorithm=False,
        historically_qualified_candidates=[r['arm'] for r in results if r['historical_passed'] and r['arm'] in ('conditional','agreement')],
        forward_evidence=dict(verified_trading_days=0,verified_trades=0,available=False),
        reasons=['Fresh timestamped forward evidence has not been collected for this newly declared policy.',
            'Desktop/mobile integration and executable-quote validation have not been established.'],
        conditional_user_authorization_recorded=True))

def verify():
    result=json.loads((OUT/'results.json').read_text(encoding='utf-8'));cfg=result['protocol'];assert signature()==result['signature']
    data=load();x,m,_,_=data;sig=result['signature']
    for name,sha in result['model_sha256'].items():assert digest(CACHE/'models'/name)==sha
    for name,sha in result['score_sha256'].items():assert digest(CACHE/name)==sha
    # Independent synthetic gate and consensus examples.
    good=dict(trades=30,net=100,profit_factor=1.5,max_drawdown=.01)
    assert gate(good,good,[50,50],cfg)[0]
    for change in [dict(trades=19),dict(net=-1),dict(profit_factor=1.1),dict(max_drawdown=.5)]:
        assert not gate(dict(good,**change),good,[50,50],cfg)[0]
    assert not gate(good,good,[0,100],cfg)[0]
    a=np.array([[.1,-.1],[.1,.2],[-.2,-.1]]);b=np.array([[.2,-.2],[.3,.1],[.1,.2]])
    expected=np.array([[.1,-np.inf],[-np.inf,-np.inf],[-np.inf,-np.inf]])
    np.testing.assert_array_equal(agree(a,b),expected)
    anchor=get_model('2025-08',data,sig);incumbent=anchor;all_ix=[];scores={a:[] for a in ARMS}
    for r in result['decisions']:
        candidate=get_model(r['candidate'],data,sig);lo=r['validation_start_ms'];hi=r['validation_end_ms']
        for model in (candidate,incumbent):
            tr,ca=model['train_indices'],model['calibration_indices'];a,b,c=model['bounds']
            assert m[tr,0].max()+1800000<b and m[ca,0].max()+1800000<c and c<=lo
        va=np.flatnonzero((m[:,0]>=lo)&(m[:,0]+1800000<hi));vd=tuple(a[va] for a in data)
        c=replay_score(vd,score(candidate,x[va]));i=replay_score(vd,score(incumbent,x[va]))
        mid=int((pd.Timestamp(r['month']+'-01',tz='Asia/Shanghai')-pd.DateOffset(months=1)).timestamp()*1000)
        months=[sum(row[6] for row in c['ledger'] if a<=row[0]<b) for a,b in [(lo,mid),(mid,hi)]]
        assert summary(c)==r['candidate_validation'] and summary(i)==r['incumbent_validation']
        accepted,rules=gate(c,i,months,cfg);assert accepted==r['gate_passed'] and rules==r['rules']
        if accepted:incumbent=candidate
        assert incumbent['key']==r['selected']
        z=np.load(CACHE/f"{r['month']}-scores.npz");te=month_indices(m,r['month']);np.testing.assert_array_equal(te,z['indices'])
        current=get_model(r['month'],data,sig);assert current['bounds'][2]<=m[te,0].min()
        now=score(incumbent,x[te]);recent=score(current,x[te])
        values=dict(reference=recent,frozen_anchor=score(anchor,x[te]),conditional=now,agreement=agree(now,recent))
        all_ix.append(te)
        for arm,value in values.items():np.testing.assert_allclose(value,z[arm],rtol=0,atol=1e-12);scores[arm].append(value)
    ix=np.concatenate(all_ix);assert len(ix)==len(np.unique(ix));d=tuple(a[ix] for a in data)
    for r in result['results']:
        s=replay_score(d,np.concatenate(scores[r['arm']]));assert summary(s)==r['historical']
        assert sum(q['net'] for q in r['months'])==s['net'];cash=50000;locked=-1
        for row in s['ledger']:
            assert row[0]>=locked and 0<row[1]-row[0]<=900000 and 1<=row[5]<=10
            assert (row[0]+28800000)//86400000==(row[1]+28800000)//86400000
            assert row[8]==cash;cash+=row[6];assert row[7]==cash;locked=row[1]
    for path,sha in cfg['runtime_sha256'].items():assert digest(ROOT/path)==sha
    deployment=json.loads((OUT/'deployment-decision.json').read_text(encoding='utf-8'))
    assert deployment['replace_runtime_algorithm'] is False and not deployment['forward_evidence']['available']
    save_json(OUT/'核验记录.json',dict(passed=True,checks=['source/model/score/protocol fingerprints',
        'gate acceptance/rejection and disagreement synthetic examples',
        'all training/calibration labels precede validation; validation precedes use',
        'all monthly decisions and predictions reproduce without evaluation outcome inputs',
        'all four shared-account replays, cash/position/calendar constraints',
        'market runtime source fingerprints unchanged; missing forward evidence blocks deployment']))
    print('All conditional replacement checks passed',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['declare','run','verify']);args=parser.parse_args()
    with threadpool_limits(limits=4):globals()[args.mode]()

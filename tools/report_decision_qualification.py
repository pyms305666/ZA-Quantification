"""Evaluate frozen, prospective promotion gates for the actual decision system."""
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/研究/2026-09-28-开盘观察'
DB=ROOT/'.tqsdk/research/decision_forward/events.sqlite3'
CN=timezone(timedelta(hours=8))


def read_events():
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN')
        return [(seq,k,json.loads(p)) for seq,k,p in db.execute('SELECT seq,kind,payload FROM events ORDER BY seq')]


def main():
    protocol_path=OUT/'接入判据.json'
    protocol=json.loads(protocol_path.read_text(encoding='utf-8'))
    events=read_events();predictions={};accepted=set();records=[];now_ms=int(time.time()*1000)
    for seq,kind,v in events:
        if kind=='prediction':predictions[seq]=v
        if kind=='prediction_committed':accepted.add(v['prediction_seq'])
        if kind=='outcome' and v['horizon']==15:
            ref=predictions.get(v['prediction_seq'])
            if ref and ref['signal_ms']>protocol['qualification_begins_after_ms']:
                records.append(v)
    # Each outcome contains the same observed price for both frozen arms.
    unique={(v['prediction_seq'],v['horizon']) for v in records}
    if len(unique)!=len(records):raise RuntimeError('Duplicate paired endpoint')
    expected=sum(ref in predictions and predictions[ref]['signal_ms']>protocol['qualification_begins_after_ms']
                 and predictions[ref]['signal_ms']+900000+5000<now_ms for ref in accepted)
    missing=expected-len(records)
    if missing<0:raise RuntimeError('More endpoints than matured committed predictions')
    perday=defaultdict(list)
    for v in records:perday[datetime.fromtimestamp(v['due']/1000,CN).date().isoformat()].append(v)
    qualified_days=sum(len(vs)>=protocol['minimum_paired_15m_endpoints_per_day'] for vs in perday.values())
    outcome={}
    for arm in ('original','calibrated'):
        active=[v for v in records if v['sides'][arm]]
        profit=[v['sides'][arm]*v['delta']*v['multiple']-v['cost'] for v in active]
        outcome[arm]=dict(observed=len(records),signals=len(active),long=sum(v['sides'][arm]==1 for v in active),
            short=sum(v['sides'][arm]==-1 for v in active),coverage=len(active)/len(records) if records else None,
            accuracy=sum(v['sides'][arm]*v['delta']>0 for v in active)/len(active) if active else None,
            cost_hurdle_rate=sum(p>0 for p in profit)/len(profit) if profit else None,
            net_per_observed_opportunity=sum(profit)/len(records) if records else None,
            net_per_signal=sum(profit)/len(profit) if profit else None)
    c=outcome['calibrated'];o=outcome['original']
    byproduct=Counter(v['product'] for v in records if v['sides']['calibrated'])
    bymonth=defaultdict(float)
    for v in records:
        side=v['sides']['calibrated'];month=datetime.fromtimestamp(v['due']/1000,CN).strftime('%Y-%m')
        if side:bymonth[month]+=side*v['delta']*v['multiple']-v['cost']
    # Resample whole days; do not claim iid tick or contract observations.
    confidence=dict(accuracy_lift_lower=None,net_per_opportunity_lower=None)
    if len(perday)>=2 and c['signals']>0 and o['signals']>0:
        import numpy as np
        daily=[]
        for day,vs in sorted(perday.items()):
            row=[0.,0.,0.,0.,0.]
            for v in vs:
                row[0]+=v['sides']['calibrated']!=0
                row[1]+=v['sides']['calibrated']*v['delta']>0
                row[2]+=v['sides']['original']!=0
                row[3]+=v['sides']['original']*v['delta']>0
                if v['sides']['calibrated']:row[4]+=v['sides']['calibrated']*v['delta']*v['multiple']-v['cost']
            daily.append(row+[len(vs)])
        a=np.asarray(daily);rng=np.random.default_rng(20260928)
        draws=a[rng.integers(len(a),size=(2000,len(a)))].sum(axis=1)
        valid=(draws[:,0]>0)&(draws[:,2]>0)
        if valid.any():confidence['accuracy_lift_lower']=float(np.quantile(draws[valid,1]/draws[valid,0]-draws[valid,3]/draws[valid,2],.025))
        confidence['net_per_opportunity_lower']=float(np.quantile(draws[:,4]/draws[:,5],.025))
    checks=dict(
        observed_days=qualified_days>=protocol['minimum_days'],
        candidate_signals=c['signals']>=protocol['minimum_candidate_signals'],
        both_directions=min(c['long'],c['short'])>=protocol['minimum_each_direction'],
        coverage=c['coverage'] is not None and o['coverage'] is not None and c['coverage']>=protocol['minimum_coverage'] and c['coverage']>=o['coverage']-protocol['max_coverage_drop'],
        accuracy_lift=c['accuracy'] is not None and o['accuracy'] is not None and c['accuracy']>=o['accuracy']+protocol['minimum_accuracy_lift'],
        accuracy_day_ci=confidence['accuracy_lift_lower'] is not None and confidence['accuracy_lift_lower']>0,
        cost_hurdle=c['cost_hurdle_rate'] is not None and o['cost_hurdle_rate'] is not None and c['cost_hurdle_rate']>o['cost_hurdle_rate'],
        terminal_net=c['net_per_observed_opportunity'] is not None and o['net_per_observed_opportunity'] is not None and c['net_per_observed_opportunity']>0 and c['net_per_observed_opportunity']>=o['net_per_observed_opportunity'],
        terminal_net_day_ci=confidence['net_per_opportunity_lower'] is not None and confidence['net_per_opportunity_lower']>0,
        breadth=sum(v>=protocol['minimum_signals_per_product'] for v in byproduct.values())>=protocol['minimum_products'],
        months=sum(v>0 for v in bymonth.values())>=protocol['minimum_positive_months'],
        endpoint_completeness=bool(records) and (len(records)/(len(records)+missing)>=protocol['minimum_endpoint_completeness']),
        execution_review=False,
        client_integration_review=False,
    )
    report=dict(asof=datetime.now(CN).isoformat(),qualification_begins_after_ms=protocol['qualification_begins_after_ms'],
        observed_days=qualified_days,valid_15m_endpoints=len(records),missing_15m_endpoints=missing,
        arms=outcome,candidate_signals_by_product=dict(byproduct),candidate_monthly_terminal_net=dict(bymonth),
        day_block_95pct_lower=confidence,checks=checks,promote=False,
        note='Numeric endpoint diagnostics cannot substitute for execution and client integration review. No change to production algorithm.')
    (OUT/'接入门槛状态.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(dict(observed_days=qualified_days,endpoints=len(records),arms=outcome,checks=checks,promote=False),ensure_ascii=False))


if __name__=='__main__':main()

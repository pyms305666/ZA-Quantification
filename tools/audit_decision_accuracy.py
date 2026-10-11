"""Audit the actual shipped evaluator, separate direction accuracy from trading PnL."""
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
from collections import Counter

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import RiskConfig
from research.backtest import History, local_time
from market.evaluator import evaluate, _trend_factor, _momentum_factor, _volume_oi_factor, _risk_factor
from market.decision_profiles import PROFILES

OUT = ROOT / 'docs/研究/2026-09-27-现有决策准确度'
DATA = ROOT / '.tqsdk/backtests/rb2610-20250926-20260924.json.gz'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(rows, horizon, side_key='side'):
    active = [r for r in rows if r[side_key]]
    def rate(predicate):
        return sum(predicate(r) for r in active)/len(active) if active else None
    return dict(opportunities=len(rows), signals=len(active), coverage=len(active)/len(rows) if rows else None,
                accuracy=rate(lambda r:r[side_key]*r[f'delta{horizon}'] > 0),
                nonflat_accuracy=(sum(r[side_key]*r[f'delta{horizon}']>0 for r in active)/sum(r[f'delta{horizon}']!=0 for r in active)
                                  if any(r[f'delta{horizon}']!=0 for r in active) else None),
                flat_outcomes=sum(r[f'delta{horizon}']==0 for r in active),
                cost_hurdle_hit=rate(lambda r:r[side_key]*r[f'delta{horizon}']*r['multiple'] > r['cost']),
                mean_net_one_lot=(float(np.mean([r[side_key]*r[f'delta{horizon}']*r['multiple']-r['cost'] for r in active])) if active else None),
                always_long_same_signals=rate(lambda r:r[f'delta{horizon}']>0),
                always_short_same_signals=rate(lambda r:r[f'delta{horizon}']<0))


def bootstrap(rows, horizon, side_key='side'):
    # Resample whole trading days, preserving intraday dependence and overlapping labels.
    days = sorted({r['day'] for r in rows})
    if not days:
        return None
    a = np.array([[sum(r[side_key]*r[f'delta{horizon}']>0 for r in rows if r['day']==d and r[side_key]),
                   sum(bool(r[side_key]) for r in rows if r['day']==d)] for d in days])
    rng = np.random.default_rng(20260927)
    draws = a[rng.integers(len(days), size=(2000, len(days)))].sum(axis=1)
    valid = draws[:,1]>0
    return np.quantile(draws[valid,0]/draws[valid,1],[.025,.975]).tolist() if valid.any() else None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    protocol = dict(data_sha256=digest(DATA), source_sha256={p:digest(ROOT/p) for p in
                    ['market/evaluator.py','market/decision_profiles.py','market/indicators.py', 'tools/audit_decision_accuracy.py']},
                    modes=['ultra','short'], sampling='15-minute bar ends; all next 30 contiguous positive-volume minutes required',
                    horizons=[15,30], endpoint='last known minute close at signal to minute close at horizon',
                    flat_is_correct=False, costs='10 yuan roundtrip + 1 adverse tick per side, no spread/depth model',
                    calibration='one fixed StandardScaler+LogisticRegression C=1 on 8 original factor scores; 60/20/20 chronological trading days; fit nonflat 15m labels only; validation confidence threshold targets original validation coverage; no parameter search',
                    selection='one rb2610 dataset already studied previously; diagnostic, not new independent evidence',
                    deploy=False)
    protocol_file=OUT/'protocol.json'
    if protocol_file.exists() and json.loads(protocol_file.read_text(encoding='utf-8')) != protocol:
        raise RuntimeError('Existing protocol differs; use a new experiment directory')
    protocol_file.write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf-8')
    with gzip.open(DATA,'rt',encoding='utf-8') as stream:
        history=History(json.load(stream))
    minute={int(b['datetime']):b for b in history.bars[60]}
    rows=[];skips=Counter();last=time.monotonic()
    for index, bar in enumerate(history.bars[900]):
        t=int(bar['datetime'])+900000
        prior=minute.get(t-60000)
        future=[minute.get(t+i*60000) for i in range(30)]
        if prior is None or prior['volume']<=0 or any(b is None or b['volume']<=0 for b in future):
            skips['missing_or_zero_volume_30m_path']+=1
            continue
        if local_time(t).date()!=local_time(t+1800000).date():
            skips['overnight']+=1
            continue
        day=str(history.trading_day(t))
        quote=dict(last=prior['close'],timestamp=t,trading_time=history.meta['trading_time'],expire_datetime=history.meta['expire_datetime'])
        for mode in ('ultra','short'):
            bars=history.completed(mode,t)
            result=evaluate(history.instrument,quote,bars,RiskConfig(),mode,t)
            if not result['data_ok']:
                skips[f'{mode}_warmup']+=1
                continue
            p=PROFILES[mode];ex=bars[p['execution']];co=bars[p['confirm']];tr=bars[p['trend']]
            vq=dict(last=ex[-1]['close'],pre_close=ex[-2]['close'],open_interest=ex[-1].get('open_interest'),pre_open_interest=ex[-2].get('open_interest'))
            factors=[*_trend_factor(tr,co)[:2],*_momentum_factor(ex,co)[:2],*_volume_oi_factor(ex,ex,vq)[:2],*_risk_factor(ex,tr,quote)[:2]]
            # Factor reconstruction must be exactly the scores used by the application.
            if abs(sum(factors[::2])-result['score_long'])>.051 or abs(sum(factors[1::2])-result['score_short'])>.051:
                raise RuntimeError('Factor reconstruction mismatch')
            rows.append(dict(mode=mode,time=t,day=day,month=day[:7],session='night' if local_time(t).hour>=18 else 'day',
                             side={'LONG':1,'SHORT':-1,'FLAT':0}[result['direction_en']],score=result['score'],
                             score_long=result['score_long'],score_short=result['score_short'],score_band=f"{int(result['score']//10)*10}-{int(result['score']//10)*10+9}",
                             delta15=future[14]['close']-prior['close'],delta30=future[29]['close']-prior['close'],
                             cost=10+2*history.instrument.price_tick*history.instrument.volume_multiple,multiple=history.instrument.volume_multiple,
                             factors=factors,contracts=result['contracts']))
        if time.monotonic()-last>15:
            print('evaluated',index+1,'/',len(history.bars[900]),flush=True);last=time.monotonic()
    summary=dict(protocol=protocol,skips=dict(skips),modes={})
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    for mode in ('ultra','short'):
        selected=[r for r in rows if r['mode']==mode]
        days=sorted({r['day'] for r in selected});cut1=days[int(len(days)*.6)];cut2=days[int(len(days)*.8)]
        train=[r for r in selected if r['day']<cut1 and r['delta15']!=0]
        validation=[r for r in selected if cut1<=r['day']<cut2]
        test=[r for r in selected if r['day']>=cut2]
        model=make_pipeline(StandardScaler(),LogisticRegression(C=1,max_iter=1000,random_state=20260927))
        model.fit([r['factors'] for r in train],[int(r['delta15']>0) for r in train])
        vp=model.predict_proba([r['factors'] for r in validation])[:,1]
        target=sum(bool(r['side']) for r in validation)/len(validation)
        threshold=float(np.quantile(np.maximum(vp,1-vp),1-target)) if target else 1.01
        tp=model.predict_proba([r['factors'] for r in test])[:,1]
        for r,p in zip(test,tp):
            r['calibrated_side']=(1 if p>=.5 else -1) if max(p,1-p)>=threshold else 0
            r['p_up']=float(p)
        groups={}
        for field in ('month','session','score_band','side'):
            groups[field]={str(value):{str(h):metrics([r for r in selected if r[field]==value],h) for h in (15,30)} for value in sorted({r[field] for r in selected})}
        summary['modes'][mode]=dict(all={str(h):dict(**metrics(selected,h),day_bootstrap_ci=bootstrap(selected,h)) for h in (15,30)},
            groups=groups,split=dict(validation_start=cut1,test_start=cut2,training_nonflat=len(train),validation=len(validation),test=len(test)),
            calibration_threshold=threshold,validation_target_coverage=target,
            test_original={str(h):dict(**metrics(test,h),day_bootstrap_ci=bootstrap(test,h)) for h in (15,30)},
            test_calibrated={str(h):dict(**metrics(test,h,'calibrated_side'),day_bootstrap_ci=bootstrap(test,h,'calibrated_side')) for h in (15,30)})
    columns=list(dict.fromkeys(k for r in rows for k in r))
    with (OUT/'逐次评估.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,columns);writer.writeheader();writer.writerows(rows)
    (OUT/'results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    for mode,result in summary['modes'].items():
        print(mode,json.dumps({k:v for k,v in result.items() if k!='groups'},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()

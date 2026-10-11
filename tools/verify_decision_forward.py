"""Independently replay saved real predictions and check subsequent market labels."""
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import joblib
from config import RiskConfig
from market.evaluator import evaluate,_trend_factor,_momentum_factor,_volume_oi_factor,_risk_factor
from market.model import Instrument
from market.decision_profiles import prepare
from research.forward_runtime import canonical

BASE=ROOT/'.tqsdk/research/decision_forward'
OUT=ROOT/'docs/研究/2026-09-28-开盘观察'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    errors=[];counts=Counter();predictions={};commits={};outcomes=[];prior='0'*64
    manifest=json.loads((BASE/'manifest.json').read_text(encoding='utf-8'))
    for source,digest in manifest['source_sha256'].items():
        if sha(ROOT/source)!=digest:errors.append(f'source changed: {source}')
    if sha(BASE/'calibration.joblib')!=manifest['model_sha256']:errors.append('candidate model changed')
    model=joblib.load(BASE/'calibration.joblib')
    with sqlite3.connect((BASE/'events.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN');events=db.execute('SELECT * FROM events ORDER BY seq').fetchall()
    for n,(seq,wall,kind,body,previous,digest) in enumerate(events,1):
        expected=hashlib.sha256(canonical([seq,wall,kind,body,previous]).encode()).hexdigest()
        if n!=seq or previous!=prior or digest!=expected:errors.append(f'ledger hash chain: {seq}')
        prior=digest;counts[kind]+=1;v=json.loads(body)
        if kind=='prediction':predictions[seq]=(wall,v)
        elif kind=='prediction_committed':
            commits[v['prediction_seq']]=v['committed_ms']
            if (v['prediction_seq'] not in predictions or
                    v['committed_ms']<predictions[v['prediction_seq']][0] or
                    v['committed_ms']>wall or
                    v['committed_ms']>predictions[v['prediction_seq']][1]['signal_ms']+5000):
                errors.append(f'prediction commit timing: {seq}')
        elif kind=='outcome':outcomes.append((seq,v))
    for seq,(wall,v) in predictions.items():
        artifact=BASE/v['input_file']
        if not artifact.is_file() or sha(artifact)!=v['input_sha256']:
            errors.append(f'prediction input hash: {seq}');continue
        with gzip.open(artifact,'rt',encoding='utf-8') as stream:inputs=json.load(stream)
        signal=v['signal_ms'];symbol=v['symbol'];q=inputs['quote']
        if not signal<=wall<=signal+5000 or not -5000<=wall-q['timestamp']<=5000:
            errors.append(f'quote/prediction time: {seq}')
        frames={int(p):bars for p,bars in inputs['inputs'].items()}
        for period,bars in frames.items():
            if len(bars)!=200 or bars[-1]['datetime']+period*1000!=signal or any(b['datetime']+period*1000>signal for b in bars):
                errors.append(f'nonclosed or incomplete input: {seq}/{period}')
        instrument=Instrument(symbol=symbol,exchange=symbol.split('.')[0],instrument_id=symbol.split('.')[1],
                              price_tick=q['price_tick'],volume_multiple=int(q['volume_multiple']),expire_datetime=q['expire_datetime'])
        result=evaluate(instrument,q,frames,RiskConfig(),'ultra',signal)
        if result!=inputs['result'] or result['score']!=v['score']:
            errors.append(f'evaluator mismatch: {seq}')
        _,prepared,_,_=prepare('ultra',frames,q,instrument,signal)
        ex,co,tr=prepared[60],prepared[300],prepared[900]
        vq=dict(last=ex[-1]['close'],pre_close=ex[-2]['close'],open_interest=ex[-1]['open_interest'],pre_open_interest=ex[-2]['open_interest'])
        factors=[*_trend_factor(tr,co)[:2],*_momentum_factor(ex,co)[:2],*_volume_oi_factor(ex,ex,vq)[:2],*_risk_factor(ex,tr,q)[:2]]
        p=float(model['model'].predict_proba([factors])[0,1])
        sides=dict(original={'LONG':1,'SHORT':-1,'FLAT':0}[result['direction_en']],
                   calibrated=(1 if p>=.5 else -1) if max(p,1-p)>=model['threshold'] else 0)
        if factors!=v['factors'] or abs(p-v['p_up_nonflat'])>1e-12 or sides!=v['sides']:
            errors.append(f'saved candidate mismatch: {seq}')
    paired=Counter()
    for seq,v in outcomes:
        ref=v['prediction_seq'];paired[(ref,v['horizon'])]+=1
        if ref not in commits or paired[(ref,v['horizon'])]>1:
            errors.append(f'outcome pair/duplicate: {seq}');continue
        if not commits[ref]<v['due']<=v['quote_ms']<=v['due']+5000 or abs(v['last']-v['anchor']-v['delta'])>1e-8:
            errors.append(f'outcome chronology/arithmetic: {seq}')
        if v['sides']!=predictions[ref][1]['sides'] or v['symbol']!=predictions[ref][1]['symbol']:
            errors.append(f'outcome source: {seq}')
    result=dict(passed=not errors,ledger_events=len(events),predictions=len(predictions),accepted=len(commits),
                observed_endpoints=len(outcomes),checks=['frozen source/model hashes','SQLite chain','saved input hashes',
                'all three completed input periods','actual evaluator rescoring','calibrated probability and side',
                'forecast before outcome','endpoint price arithmetic and one pair per horizon'],errors=errors,
                limitation='No accuracy claim without enough fresh observed outcomes; exchange-clock timestamps are locally observed, not third-party attested.')
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'实时预测核验.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    if errors:raise SystemExit(1)


if __name__=='__main__':main()

"""Observe the actual ultra decision engine against future quotes; no order API."""
import argparse
from datetime import datetime, timezone, timedelta
import gzip
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import joblib
from config import RiskConfig, load_config
from market.evaluator import evaluate, _trend_factor, _momentum_factor, _volume_oi_factor, _risk_factor
from market.model import Instrument
from market.decision_profiles import prepare, session_info
from research.forward_runtime import Journal
from tools.forward_research import atomic, sha, metadata, frame_rows

BASE=ROOT/'.tqsdk/research/decision_forward'
PARENT=ROOT/'.tqsdk/research/forward15m/status.json'
OUT=ROOT/'docs/研究/2026-09-27-决策准确度多品种'
CN=timezone(timedelta(hours=8))
FIELDS=('datetime','open','high','low','close','volume','open_interest')
SOURCES=['tools/forward_decision_accuracy.py','tools/forward_research.py','research/forward_runtime.py',
         'market/evaluator.py','market/decision_profiles.py','market/indicators.py','market/model.py','config.py']


def freeze():
    BASE.mkdir(parents=True,exist_ok=True)
    if (BASE/'manifest.json').exists():raise RuntimeError('Experiment already frozen')
    shutil.copy2(ROOT/'.tqsdk/research/decision_accuracy/factor-calibration.joblib',BASE/'calibration.joblib')
    value=dict(created_ms=int(time.time()*1000),source_sha256={p:sha(ROOT/p) for p in SOURCES},
        model_sha256=sha(BASE/'calibration.joblib'),mode='ultra',periods=[60,300,900],completed_bars=200,
        selector='Fresh parent collector selected_symbols snapshot; record selection timestamp, may lag current boundary.',
        label='Last quote known at signal versus first fresh exchange quote at/after signal+15/30m, max 5 seconds late; endpoint diagnostic only.',
        max_quote_age_ms=5000,max_prediction_delay_ms=5000,signal_interval_ms=900000,
        require_session_minutes=30,invalid_missing_endpoint=True,no_real_orders=True,no_deployment=True)
    atomic(BASE/'manifest.json',value);atomic(OUT/'前向冻结方案.json',value)


def timestamp(q):
    try:return int(datetime.fromisoformat(q.datetime).replace(tzinfo=CN).timestamp()*1000)
    except (ValueError,TypeError):return 0


def run(seconds):
    import msvcrt
    from tqsdk import TqApi,TqAuth,TqSim
    cfg=json.loads((BASE/'manifest.json').read_text(encoding='utf-8'))
    for p,h in cfg['source_sha256'].items():
        if sha(ROOT/p)!=h:raise RuntimeError('Frozen source differs: '+p)
    if sha(BASE/'calibration.joblib')!=cfg['model_sha256']:raise RuntimeError('Model differs')
    lock=(BASE/'service.lock').open('a+b');lock.seek(0)
    if not lock.read(1):lock.write(b'1');lock.flush()
    lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    (BASE/'STOP').unlink(missing_ok=True)
    model=joblib.load(BASE/'calibration.joblib');journal=Journal(BASE/'events.sqlite3');journal.verify()
    pending=journal.read('pending',[]);stats=journal.read('stats',dict(predictions=0,invalid_endpoints=0,outcomes={}))
    if pending:
        stats['invalid_endpoints']+=len(pending)
        journal.event('restart_invalidates_pending',dict(count=len(pending)),marks={'pending':[],'stats':stats})
        pending=[]
    last_signal=journal.read('last_signal',0);boot=int(time.time()*1000)
    deadline=time.time()+seconds if seconds else float('inf');last_status=0;last_parent=0;selected={};parent_ms=0;api=None
    def publish(state,error=None):
        atomic(BASE/'status.json',dict(state=state,pid=os.getpid(),wall_ms=int(time.time()*1000),started_ms=boot,
            selected_symbols=selected,selector_timestamp=parent_ms,stats=stats,pending_endpoints=len(pending),error=error,
            real_orders=False,algorithm_replaced=False))
    publish('connecting')
    try:
        while time.time()<deadline and not (BASE/'STOP').exists():
            try:
                c=load_config().tqsdk;api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),debug=False,disable_print=True)
                quotes={};frames={};last_parent=0
                while time.time()<deadline and not (BASE/'STOP').exists():
                    now=int(time.time()*1000)
                    if now-last_parent>2000:
                        try:
                            parent=json.loads(PARENT.read_text(encoding='utf-8'));parent_ms=parent['wall_ms']
                            selected=parent.get('selected_symbols',{}) if 0<=now-parent_ms<30000 and parent['state']!='stopped' else {}
                        except (OSError,ValueError,KeyError):selected={}
                        for symbol in set(selected.values())|{p['symbol'] for p in pending}:
                            if symbol not in quotes:
                                quotes[symbol]=api.get_quote(symbol)
                                frames[symbol]={p:api.get_kline_serial(symbol,p,data_length=201) for p in (60,300,900)}
                        last_parent=now
                    api.wait_update(deadline=time.time()+.25);now=int(time.time()*1000)
                    for item in list(pending):
                        q=quotes.get(item['symbol']);qt=timestamp(q) if q is not None else 0
                        valid=q is not None and item['due']<=qt<=item['due']+5000 and -5000<=now-qt<=5000 and math.isfinite(q.last_price) and q.last_price>0
                        if valid:
                            delta=float(q.last_price)-item['anchor'];h=str(item['horizon'])
                            for arm,side in item['sides'].items():
                                s=stats['outcomes'].setdefault(arm+'_'+h,dict(opportunities=0,signals=0,correct=0,flat=0,cost_hits=0))
                                s['opportunities']+=1;s['signals']+=int(side!=0)
                                s['correct']+=int(side!=0 and side*delta>0);s['flat']+=int(side!=0 and delta==0)
                                s['cost_hits']+=int(side!=0 and side*delta*item['multiple']>item['cost'])
                            pending.remove(item)
                            journal.event('outcome',dict(**item,quote_ms=qt,received_ms=now,last=float(q.last_price),delta=delta),marks={'pending':pending,'stats':stats})
                        elif now>item['due']+5000:
                            stats['invalid_endpoints']+=1;pending.remove(item)
                            journal.event('missing_endpoint',item,marks={'pending':pending,'stats':stats})
                    signal=now//900000*900000
                    if signal>last_signal and signal>boot and 1000<=now-signal<=3000:
                        # One attempt per boundary; never backfill a missed real-time decision.
                        last_signal=signal;journal.event('boundary_attempt',dict(signal_ms=signal),marks={'last_signal':signal})
                        for product,symbol in list(selected.items()):
                            q=quotes[symbol];qt=timestamp(q);meta=metadata(q)
                            session=session_info(meta['trading_time'],signal)
                            if not -5000<=now-qt<=5000 or session.get('minutes_to_close',0) is None or (session.get('minutes_to_close') or 0)<30:continue
                            if datetime.fromtimestamp(signal/1000,CN).date()!=datetime.fromtimestamp((signal+1800000)/1000,CN).date():continue
                            if meta['expire_datetime'] and signal+1800000>=meta['expire_datetime']*1000:continue
                            if not all(api.is_serial_ready(f) for f in frames[symbol].values()):continue
                            inputs={p:[dict(zip(FIELDS,row)) for row in frame_rows(f,p,signal)[-200:].tolist()] for p,f in frames[symbol].items()}
                            if any(len(v)<200 for v in inputs.values()):continue
                            if any(inputs[p][-1]['datetime']+p*1000!=signal for p in inputs):continue
                            quote=dict(last=float(q.last_price),timestamp=qt,**meta)
                            instrument=Instrument(symbol=symbol,exchange=symbol.split('.')[0],instrument_id=symbol.split('.')[1],
                                price_tick=meta['price_tick'],volume_multiple=int(meta['volume_multiple']),expire_datetime=meta['expire_datetime'])
                            result=evaluate(instrument,quote,inputs,RiskConfig(),'ultra',signal)
                            if not result['data_ok']:continue
                            _,prepared,_,_=prepare('ultra',inputs,quote,instrument,signal)
                            ex=prepared[60];co=prepared[300];tr=prepared[900]
                            vq=dict(last=ex[-1]['close'],pre_close=ex[-2]['close'],open_interest=ex[-1]['open_interest'],pre_open_interest=ex[-2]['open_interest'])
                            factors=[*_trend_factor(tr,co)[:2],*_momentum_factor(ex,co)[:2],*_volume_oi_factor(ex,ex,vq)[:2],*_risk_factor(ex,tr,quote)[:2]]
                            probability=float(model['model'].predict_proba([factors])[0,1])
                            sides=dict(original={'LONG':1,'SHORT':-1,'FLAT':0}[result['direction_en']],
                                calibrated=(1 if probability>=.5 else -1) if max(probability,1-probability)>=model['threshold'] else 0)
                            artifact=BASE/f'input-{signal}-{symbol}.json.gz'
                            with gzip.open(artifact,'wt',encoding='utf-8') as stream:json.dump(dict(quote=quote,inputs=inputs,result=result),stream,allow_nan=False)
                            done=int(time.time()*1000)
                            seq=journal.event('prediction',dict(signal_ms=signal,symbol=symbol,product=product,selector_timestamp=parent_ms,
                                quote_ms=qt,anchor=quote['last'],sides=sides,score=result['score'],factors=factors,
                                p_up_nonflat=probability,input_file=artifact.name,input_sha256=sha(artifact)),done)
                            committed=int(time.time()*1000)
                            if committed>signal+5000:
                                journal.event('late_prediction',dict(prediction_seq=seq,committed_ms=committed));continue
                            for horizon in (15,30):
                                pending.append(dict(prediction_seq=seq,symbol=symbol,product=product,committed_ms=committed,
                                    due=signal+horizon*60000,horizon=horizon,anchor=quote['last'],sides=sides,
                                    multiple=meta['volume_multiple'],cost=10+2*meta['price_tick']*meta['volume_multiple']))
                            stats['predictions']+=1
                            journal.event('prediction_committed',dict(prediction_seq=seq,committed_ms=committed),marks={'pending':pending,'stats':stats})
                    if now-last_status>=10000:
                        if shutil.disk_usage(BASE).free<2*1024**3:raise RuntimeError('Low disk space')
                        fresh=any(0<=now-timestamp(q)<=5000 for q in quotes.values())
                        publish('observing' if fresh else 'waiting_for_fresh_market');last_status=now
            except Exception as error:
                publish('connection_error',type(error).__name__)
                if pending:
                    stats['invalid_endpoints']+=len(pending);pending=[]
                    journal.event('connection_invalidates_pending',{},marks={'pending':pending,'stats':stats})
                if shutil.disk_usage(BASE).free<2*1024**3:break
                if time.time()<deadline and not (BASE/'STOP').exists():time.sleep(5)
            finally:
                if api is not None:api.close();api=None
    finally:
        publish('stopped');journal.event('service_stop',{});journal.db.close();lock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['freeze','run','status','stop']);parser.add_argument('--seconds',type=int,default=0);args=parser.parse_args()
    if args.action=='freeze':freeze()
    elif args.action=='run':run(args.seconds)
    elif args.action=='stop':(BASE/'STOP').write_text('stop',encoding='utf-8')
    else:print((BASE/'status.json').read_text(encoding='utf-8'))

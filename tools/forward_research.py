"""Frozen forward research service. Market data only; paper trades are local SQLite events."""
import argparse
from datetime import date,datetime,timedelta,timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import joblib
import numpy as np
from threadpoolctl import threadpool_limits
from research.forward_runtime import Journal,PaperBook,live_feature,ARMS
from research.intraday_profit import closed_features
from research.direction_model import FEATURES
from market.decision_profiles import session_info
from tools.research_gated_15m import score as model_score,agree

BASE=ROOT/'.tqsdk/research/forward15m'
OUT=ROOT/'docs/研究/2026-09-27-新行情前向验证'
PRODUCTS=['CZCE.OI','CZCE.AP','CZCE.SF','DCE.eb','DCE.p','DCE.pg','DCE.y','INE.lu']
SOURCES=['research/forward_runtime.py','research/intraday_profit.py','research/conditional_intraday.py',
    'research/minute_intraday.py','research/direction_model.py','tools/research_rolling_15m.py',
    'tools/research_gated_15m.py','tools/forward_research.py','market/decision_profiles.py']

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def atomic(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');os.replace(tmp,path)

def prepare():
    BASE.mkdir(exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    if (BASE/'manifest.json').exists():raise RuntimeError('Frozen manifest exists; do not replace an ongoing experiment')
    previous=json.loads((ROOT/'docs/研究/2026-09-27-有条件替换模型/results.json').read_text(encoding='utf-8'))
    selected=previous['decisions'][-1]['selected'];keys=dict(reference='2026-09',frozen_anchor='2025-08',conditional=selected)
    for role,key in keys.items():
        source=ROOT/f'.tqsdk/research/gated15m/models/{key}.joblib'
        assert sha(source)==previous['model_sha256'][source.name]
        shutil.copy2(source,BASE/f'{role}.joblib')
    universe=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['products']
    cfg=dict(version=1,created_at_utc=datetime.now(timezone.utc).isoformat(),created_ms=int(time.time()*1000),
        purpose='fresh forward evidence, frozen versions; not validation of monthly retraining policy',
        products={p:universe.index(p) for p in PRODUCTS},model_versions=keys,
        model_sha256={role:sha(BASE/f'{role}.joblib') for role in keys},source_sha256={p:sha(ROOT/p) for p in SOURCES},
        update_models_during_experiment=False,arms=list(ARMS),account_per_arm=50000,
        fee_roundtrip_per_lot=10,adverse_ticks_beyond_L1_per_side=1,risk_amount=900,risk_fraction=.018,
        max_lots=10,margin_assumption=.15,max_hold_minutes=15,signal_minutes=15,
        require_remaining_session_minutes=30,signal_commit_max_delay_ms=4000,entry_deadline_after_signal_ms=5000,
        quote_max_age_ms=5000,quote_gap_marks_trade_invalid_ms=5000,require_displayed_depth=True,
        active_selection='highest last fully closed 15-minute OI among all eligible live contracts per product',
        minimum_closed_bars=60,input_15m_window=256,input_minute_window=90,
        qualifying_day_minimum_complete_batches=8,complete_batch_requires_all_session_eligible_products=True,minimum_forward_days=60,minimum_valid_forward_trades=100,
        invalid_trade_excludes_qualification_not_losses=True,unresolved_execution_quality_blocks_qualification=True,
        minimum_profit_factor=1.2,max_drawdown=.06878515740649363,
        timestamp_attestation='local wall clock and exchange timestamps; local hash chain is not third-party notarization',
        no_live_orders=True,no_automatic_software_replacement=True,no_os_startup_task=True)
    atomic(BASE/'manifest.json',cfg);atomic(OUT/'冻结方案.json',cfg)
    print('Frozen models and forward rules prepared',flush=True)

def load_manifest():
    cfg=json.loads((BASE/'manifest.json').read_text(encoding='utf-8'))
    for p,h in cfg['source_sha256'].items():
        if sha(ROOT/p)!=h:raise RuntimeError('Frozen code changed: '+p)
    for role,h in cfg['model_sha256'].items():
        if sha(BASE/f'{role}.joblib')!=h:raise RuntimeError('Frozen model changed: '+role)
    return cfg

def frame_rows(frame,duration,signal):
    values=frame[['datetime','open','high','low','close','volume','close_oi']].to_numpy(copy=True)
    values=values[np.isfinite(values).all(axis=1)&(values[:,0]>0)];values[:,0]=np.floor(values[:,0]/1e6)
    return values[values[:,0]+duration*1000<=signal]

def metadata(quote):
    sessions=quote.trading_time
    return dict(price_tick=float(quote.price_tick),volume_multiple=float(quote.volume_multiple),
        expire_datetime=float(quote.expire_datetime) if math.isfinite(quote.expire_datetime) else 0,
        trading_time={k:[list(pair) for pair in (sessions.get(k,[]) if isinstance(sessions,dict) else getattr(sessions,k,[]))] for k in ('day','night')})

def trading_day(now_ms,calendar):
    now=datetime.fromtimestamp(now_ms/1000,timezone(timedelta(hours=8)));today=now.date()
    if now.hour>=18 or now.hour<8:
        anchor=today if now.hour>=18 else today-timedelta(days=1)
        return next((d for d in sorted(calendar) if d>anchor.isoformat() and calendar[d]),None)
    return today.isoformat() if calendar.get(today.isoformat()) else None

def status():
    state=json.loads((BASE/'status.json').read_text(encoding='utf-8')) if (BASE/'status.json').exists() else dict(state='not_started')
    state['status_age_seconds']=round(time.time()-state.get('wall_ms',0)/1000,1) if state.get('wall_ms') else None
    print(json.dumps(state,ensure_ascii=False,indent=2),flush=True)

def stop():
    BASE.mkdir(exist_ok=True);(BASE/'STOP').write_text('User requested graceful stop\n',encoding='utf-8');print('Stop marker written',flush=True)

def run(seconds):
    import msvcrt
    from config import load_config
    from tqsdk import TqApi,TqAuth,TqSim
    cfg=load_manifest();lock=(BASE/'service.lock').open('a+b');lock.seek(0)
    if lock.read(1)==b'':lock.write(b'1');lock.flush()
    lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:raise RuntimeError('Forward service is already running')
    (BASE/'STOP').unlink(missing_ok=True)
    journal=Journal(BASE/'events.sqlite3');journal.verify();book=PaperBook(journal)
    models={role:joblib.load(BASE/f'{role}.joblib') for role in cfg['model_versions']}
    boot=int(time.time()*1000);deadline=time.time()+seconds if seconds else float('inf')
    stats=journal.read('collector_stats',dict(prediction_batches=0,complete_batches_by_day={},fresh_ticks=0,skipped_batches=0))
    selected={};last_attempt=0;last_status=0;last_refresh=None;api=None;raw_stream=None;raw_date=None
    def publish(state,error=None):
        now=int(time.time()*1000);book.heartbeat(now)
        value=dict(state=state,pid=os.getpid(),wall_ms=now,started_ms=boot,errors=error,
            selected_symbols=selected,stats=stats,qualifying_observed_days=sum(v>=8 for v in stats['complete_batches_by_day'].values()),
            paper_accounts=book.state,real_orders=False,software_replacement_allowed=False)
        atomic(BASE/'status.json',value);journal.save('collector_stats',stats)
    publish('connecting')
    try:
        while time.time()<deadline and not (BASE/'STOP').exists():
            try:
                c=load_config().tqsdk;api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),debug=False,disable_print=True)
                quotes={};bars={};minutes={};ticks={};last_tick={};catalog={};calendar={};ready=False;last_refresh=None
                while time.time()<deadline and not (BASE/'STOP').exists():
                    now=int(time.time()*1000);day=datetime.now().date()
                    if last_refresh!=day:
                        for product in PRODUCTS:
                            ex,p=product.split('.');catalog[product]=list(api.query_quotes(ins_class='FUTURE',exchange_id=ex,product_id=p,expired=False))
                        table=api.get_trading_calendar(day-timedelta(days=7),date(day.year,12,31))
                        calendar={str(row['date'])[:10]:bool(row['trading']) for row in table.to_dict('records')}
                        journal.event('catalog_calendar',dict(catalog=catalog,calendar=calendar))
                        for symbol in [s for ss in catalog.values() for s in ss]:
                            if symbol not in quotes:
                                quotes[symbol]=api.get_quote(symbol);bars[symbol]=api.get_kline_serial(symbol,900,data_length=256)
                                minutes[symbol]=api.get_kline_serial(symbol,60,data_length=90);ticks[symbol]=api.get_tick_serial(symbol,data_length=200)
                        last_refresh=day
                    api.wait_update(deadline=time.time()+.5);now=int(time.time()*1000)
                    if not ready:
                        available=sum(api.is_serial_ready(f) for f in bars.values())
                        if available==len(bars):
                            ready=True;journal.event('warmup_ready',dict(symbols=len(bars)))
                            for product,ss in catalog.items():
                                eligible=[s for s in ss if math.isfinite(quotes[s].open_interest)]
                                if eligible:selected[product]=max(eligible,key=lambda s:quotes[s].open_interest)
                        else:
                            if time.time()-last_status>10:publish(f'warming_{available}_of_{len(bars)}');last_status=time.time()
                            continue
                    wanted=set(selected.values())|{s['position']['symbol'] for s in book.state.values() if s['position']}|{s['pending']['symbol'] for s in book.state.values() if s['pending']}
                    for symbol in wanted:
                        frame=ticks[symbol]
                        if not api.is_serial_ready(frame):continue
                        latest=int(frame.iloc[-1]['id'])
                        if symbol not in last_tick:last_tick[symbol]=latest;continue
                        if latest==last_tick[symbol]:continue
                        new=frame[frame.id>last_tick[symbol]];last_tick[symbol]=latest
                        for row in new[['datetime','last_price','bid_price1','bid_volume1','ask_price1','ask_volume1','volume','amount','open_interest']].to_numpy():
                            if not np.isfinite(row).all():continue
                            row=row.copy();row[0]/=1e6
                            received=int(time.time()*1000)
                            # Exchange timestamps can lead the local receipt clock slightly.
                            # This tick has already arrived; retain only the bounded skew.
                            if row[0]<max(boot,cfg['created_ms']) or not -5000<=received-row[0]<=5000:continue
                            date_key=datetime.now().strftime('%Y-%m-%d')
                            if raw_date!=date_key:
                                if raw_stream:raw_stream.close()
                                raw_date=date_key;raw_stream=gzip.open(BASE/f'ticks-{date_key}.jsonl.gz','at',encoding='utf-8')
                            raw_stream.write(json.dumps(dict(received_ms=received,symbol=symbol,tick=row.tolist()),separators=(',',':'))+'\n')
                            stats['fresh_ticks']+=1;book.tick(symbol,row.tolist(),received)
                    signal=now//900000*900000
                    if signal>last_attempt and signal>journal.read('last_prediction',0) and 1000<=now-signal<=3000:
                        last_attempt=signal;td=trading_day(signal,calendar);candidates=[];failures=[];expected_products=0
                        if td and shutil.disk_usage(BASE).free>2*1024**3:
                            for product,ss in catalog.items():
                                if not ss:failures.append(dict(product=product,reason='empty catalog'));continue
                                scheduled=session_info(metadata(quotes[ss[0]])['trading_time'],signal)
                                if scheduled['status']!='scheduled' or scheduled['minutes_to_close']<30:continue
                                expected_products+=1
                                choices=[]
                                for symbol in ss:
                                    if not api.is_serial_ready(bars[symbol]):continue
                                    a=frame_rows(bars[symbol],900,signal)
                                    if len(a)<60 or a[-1,0]!=signal-900000 or a[-1,5]<100 or a[-1,6]<100:continue
                                    d,f=closed_features(a.tolist());meta=metadata(quotes[symbol]);expiry=meta['expire_datetime']
                                    f['days_to_expiry']=np.clip((expiry*1000-d.time)/86400000,0,1000) if expiry else 0
                                    if np.isfinite(f[FEATURES].iloc[-1]).all():choices.append((a[-1,6],symbol,a,meta))
                                if not choices:failures.append(dict(product=product,reason='no eligible closed contract'));continue
                                _,symbol,a,meta=max(choices,key=lambda q:(q[0],q[1]));selected[product]=symbol
                                try:
                                    session=session_info(meta['trading_time'],signal)
                                    if session['status']!='scheduled' or session['minutes_to_close']<30:raise ValueError('session cannot cover next 30 minutes')
                                    if (signal+28800000)//86400000!=(signal+1800000+28800000)//86400000:raise ValueError('midnight boundary')
                                    if meta['expire_datetime'] and signal+1800000>=meta['expire_datetime']*1000:raise ValueError('expiry boundary')
                                    quote_ms=int(datetime.fromisoformat(quotes[symbol].datetime).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
                                    if not -5000<=now-quote_ms<=5000:raise ValueError('stale real quote')
                                    if not api.is_serial_ready(minutes[symbol]):raise ValueError('minute warmup incomplete')
                                    mins=frame_rows(minutes[symbol],60,signal);feature,spec=live_feature(a,mins,meta,signal,cfg['products'][product])
                                    predictions={role:model_score(model,feature[None,:]) for role,model in models.items()}
                                    predictions['agreement']=agree(predictions['conditional'],predictions['reference'])
                                    artifact=BASE/f'input-{signal}-{symbol}.json.gz'
                                    with gzip.open(artifact,'wt',encoding='utf-8') as f:json.dump(dict(signal_ms=signal,meta=meta,bars=a.tolist(),minutes=mins.tolist(),product_id=cfg['products'][product]),f,allow_nan=False)
                                    candidates.append(dict(symbol=symbol,product=product,feature=feature.tolist(),spec=spec,input_file=artifact.name,input_sha256=sha(artifact),
                                        scores={role:[float(v) if np.isfinite(v) else None for v in values[0]] for role,values in predictions.items()}))
                                except (ValueError,KeyError) as e:failures.append(dict(product=product,symbol=symbol,reason=str(e)))
                            done=int(time.time()*1000)
                            complete=len(candidates)==expected_products and not failures
                            if candidates and book.prediction(signal,candidates,done,allow_entries=complete):
                                stats['prediction_batches']+=1
                                stats['complete_batches_by_day'][td]=stats['complete_batches_by_day'].get(td,0)+1
                            else:stats['skipped_batches']+=1
                        else:failures.append(dict(reason='nontrading date/unknown calendar or low disk'))
                        journal.event('batch_quality',dict(signal_ms=signal,trading_day=td,candidates=len(candidates),failures=failures))
                    if time.time()-last_status>10:
                        if shutil.disk_usage(BASE).free<=2*1024**3:
                            journal.event('low_disk_stop',dict(minimum_free_bytes=2*1024**3));deadline=0
                        publish('collecting' if stats['fresh_ticks'] else 'waiting_for_fresh_market')
                        if raw_stream:raw_stream.flush()
                        last_status=time.time()
            except Exception as e:
                # Do not include account identifiers or server payloads in public status.
                journal.event('connection_error',dict(type=type(e).__name__));publish('reconnecting',type(e).__name__)
                for s in book.state.values():
                    if s['position']:s['position']['invalid']=True
                until=time.time()+10
                while time.time()<until and not (BASE/'STOP').exists():time.sleep(.5)
            finally:
                if api:api.close();api=None
    finally:
        if raw_stream:raw_stream.close()
        publish('stopped');journal.event('service_stop',dict(pid=os.getpid()),state=book.state)
        journal.db.close();lock.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['prepare','run','status','stop']);parser.add_argument('--seconds',type=int,default=0);args=parser.parse_args()
    with threadpool_limits(limits=1):
        if args.mode=='run':run(args.seconds)
        else:globals()[args.mode]()

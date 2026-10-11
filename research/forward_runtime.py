"""Past-only live feature adapter and local paper ledger; no brokerage API."""
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
import numpy as np
from research.direction_model import FEATURES
from research.intraday_profit import closed_features
from research.conditional_intraday import contextual_features
from research.minute_intraday import features as minute_features

ARMS=('reference','frozen_anchor','conditional','agreement')

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def live_feature(bars,minutes,meta,signal_ms,product_id):
    rows=np.asarray(bars,dtype=float);rows=rows[rows[:,0]+900000<=signal_ms]
    if len(rows)<60 or rows[-1,0]!=signal_ms-900000:raise ValueError('missing closed 15-minute history')
    d,x=closed_features(rows.tolist());expiry=meta.get('expire_datetime',0) or 0
    x['days_to_expiry']=np.clip((expiry*1000-d.time)/86400000,0,1000) if expiry else 0
    base=x[FEATURES].iloc[-1].to_numpy(dtype=float);close=rows[-1,4]
    if not np.isfinite(base).all() or rows[-1,5]<100 or rows[-1,6]<100:raise ValueError('ineligible closed bar')
    tick=float(meta['price_tick']);multiple=float(meta['volume_multiple'])
    if tick<=0 or multiple<=0:raise ValueError('invalid contract specifications')
    atr=base[7]*close;distance=max(tick,math.ceil(1.5*atr/tick)*tick)
    extra=np.array([(10+2*tick*multiple)/(distance*multiple),tick/atr,close*multiple*.15/50000,product_id])
    _,ctx=contextual_features(rows.tolist())
    minute=np.asarray(minutes,dtype=float);minute=minute[(minute[:,0]>=signal_ms-900000)&(minute[:,0]+60000<=signal_ms)]
    if len(minute)!=15 or not np.array_equal(minute[:,0],signal_ms+np.arange(-15,0)*60000):raise ValueError('incomplete closed minute path')
    aggregate=np.array([minute[0,1],minute[:,2].max(),minute[:,3].min(),minute[-1,4]])
    if not np.allclose(aggregate,rows[-1,1:5],rtol=0,atol=1e-7) or abs(minute[:,5].sum()-rows[-1,5])>1e-6:raise ValueError('minute/bar mismatch')
    result=np.r_[base,extra,ctx[-1],minute_features(minute[None,:,1:],np.array([atr]))[0]]
    if len(result)!=37 or not np.isfinite(result).all():raise ValueError('nonfinite live feature')
    return result,dict(tick=tick,multiple=multiple,distance=float(distance),close=float(close),
        cost_stop_ratio=float(extra[0]),oi=float(rows[-1,6]))

class Journal:
    def __init__(self,path):
        self.db=sqlite3.connect(path);self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, wall_ms INTEGER,kind TEXT,payload TEXT,previous TEXT,hash TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,payload TEXT)');self.db.commit()
    def read(self,key,default=None):
        row=self.db.execute('SELECT payload FROM state WHERE key=?',(key,)).fetchone();return json.loads(row[0]) if row else default
    def event(self,kind,payload,wall_ms=None,state=None,marks=None):
        now=int(time.time()*1000) if wall_ms is None else int(wall_ms);body=canonical(payload)
        with self.db:
            row=self.db.execute('SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            seq=row[0]+1 if row else 1;previous=row[1] if row else '0'*64
            sha=hashlib.sha256(canonical([seq,now,kind,body,previous]).encode()).hexdigest()
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',(seq,now,kind,body,previous,sha))
            if state is not None:self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',('paper',canonical(state)))
            for key,value in (marks or {}).items():self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,canonical(value)))
        return seq
    def save(self,key,payload):
        with self.db:self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,canonical(payload)))
    def verify(self):
        previous='0'*64;count=0
        for seq,wall,kind,body,prev,sha in self.db.execute('SELECT * FROM events ORDER BY seq'):
            assert seq==count+1 and prev==previous
            assert sha==hashlib.sha256(canonical([seq,wall,kind,body,prev]).encode()).hexdigest()
            previous=sha;count+=1
        return count

class PaperBook:
    def __init__(self,journal,clock=None):
        self.journal=journal;self.clock=clock or (lambda:int(time.time()*1000));self.state=journal.read('paper') or {a:dict(cash=50000.,peak=50000.,max_drawdown=0.,
            pending=None,position=None,trades=0,invalid_trades=0,wins=0,gross_profit=0.,gross_loss=0.) for a in ARMS}
        for arm,s in self.state.items():
            if s['pending']:s['pending']=None
            if s['position']:s['position']['invalid']=True
        self.journal.event('paper_resume',dict(open_positions=sum(s['position'] is not None for s in self.state.values())),state=self.state)

    def prediction(self,signal_ms,candidates,wall_ms,allow_entries=True):
        if not signal_ms<=wall_ms<=signal_ms+4000:return False
        if signal_ms<=self.journal.read('last_prediction',0):return False
        # The synchronous FULL journal commit finishes before any fill is considered.
        seq=self.journal.event('prediction',dict(signal_ms=signal_ms,candidates=candidates,allow_entries=allow_entries),wall_ms,marks={'last_prediction':signal_ms})
        committed_ms=max(wall_ms,self.clock())
        self.journal.event('prediction_committed',dict(prediction_seq=seq,committed_ms=committed_ms),committed_ms)
        if committed_ms>signal_ms+4000 or not allow_entries:return False
        for arm,s in self.state.items():
            if s['position'] or s['pending']:continue
            choices=[]
            for row in candidates:
                values=row['scores'][arm]
                if row['spec']['cost_stop_ratio']>.35:continue
                for index,value in enumerate(values):
                    if value is not None and value>=0:choices.append((value,row['symbol'],1 if index==0 else -1,row))
            if choices:
                value,symbol,side,row=max(choices,key=lambda a:(a[0],a[1]))
                s['pending']=dict(symbol=symbol,side=side,spec=row['spec'],signal_ms=signal_ms,deadline=signal_ms+5000,
                    prediction_seq=seq,prediction_ms=committed_ms)
        self.journal.event('paper_pending',dict(prediction_seq=seq),wall_ms,state=self.state);return True

    def tick(self,symbol,tick,received_ms):
        # [exchange_ms,last,bid,bid_size,ask,ask_size,volume,amount,oi]
        event_ms,last,bid,bsize,ask,asize,*_=tick
        fresh=-5000<=received_ms-event_ms<=5000 and bid>0 and ask>=bid and bsize>0 and asize>0
        for arm,s in self.state.items():
            pending=s['pending']
            if pending and received_ms>pending['deadline']:
                s['pending']=None;self.journal.event('entry_expired',dict(arm=arm,signal_ms=pending['signal_ms']),received_ms,state=self.state);pending=None
            if pending and pending['symbol']==symbol and fresh and received_ms>pending['prediction_ms'] and event_ms>pending['prediction_ms']:
                spec=pending['spec'];side=pending['side'];price=(ask if side==1 else bid)+side*spec['tick']
                risk=spec['distance']*spec['multiple']+10+2*spec['tick']*spec['multiple']
                lots=min(10,int(min(900,s['cash']*.018)/risk),int(s['cash']/(price*spec['multiple']*.15)))
                depth=asize if side==1 else bsize;s['pending']=None
                if lots<1 or depth<lots:
                    self.journal.event('entry_rejected',dict(arm=arm,symbol=symbol,reason='risk/margin/depth',lots=lots,displayed_depth=depth),received_ms,state=self.state)
                else:
                    s['position']=dict(symbol=symbol,side=side,lots=lots,entry=float(price),stop=float(price-side*spec['distance']),
                        entry_ms=float(event_ms),entry_received_ms=received_ms,due_ms=pending['signal_ms']+900000,
                        spec=spec,prediction_seq=pending['prediction_seq'],last_event_ms=event_ms,invalid=False)
                    self.journal.event('paper_entry',dict(arm=arm,**s['position']),received_ms,state=self.state)
            pos=s['position']
            if not pos or pos['symbol']!=symbol:continue
            if not fresh:
                pos['invalid']=True;continue
            if event_ms<pos['last_event_ms']:continue
            if event_ms-pos['last_event_ms']>5000:pos['invalid']=True
            pos['last_event_ms']=event_ms;side=pos['side'];spec=pos['spec'];opposing=bid if side==1 else ask
            marked=(side*(opposing-side*spec['tick']-pos['entry'])*spec['multiple']-10)*pos['lots']
            equity=s['cash']+marked;s['peak']=max(s['peak'],equity);s['max_drawdown']=max(s['max_drawdown'],(s['peak']-equity)/s['peak'])
            hit=opposing<=pos['stop'] if side==1 else opposing>=pos['stop'];due=event_ms>=pos['due_ms']
            if not hit and not due:continue
            depth=bsize if side==1 else asize
            if depth<pos['lots']:
                if not pos.get('depth_wait'):
                    pos['depth_wait']=True;self.journal.event('exit_depth_wait',dict(arm=arm,symbol=symbol,depth=depth),received_ms)
                pos['invalid']=True;continue
            if event_ms>pos['due_ms']+5000:pos['invalid']=True
            before=s['cash'];s['cash']+=marked;s['trades']+=1;s['wins']+=int(marked>0)
            s['gross_profit']+=max(marked,0);s['gross_loss']+=max(-marked,0);s['invalid_trades']+=int(pos['invalid'])
            record=dict(arm=arm,**pos,exit_ms=float(event_ms),exit_received_ms=received_ms,net=float(marked),
                before=before,after=s['cash'],reason='stop' if hit else 'time')
            s['position']=None;self.journal.event('paper_exit',record,received_ms,state=self.state)

    def heartbeat(self,wall_ms):
        for arm,s in self.state.items():
            if s['pending'] and wall_ms>s['pending']['deadline']:
                self.journal.event('entry_expired',dict(arm=arm,signal_ms=s['pending']['signal_ms']),wall_ms)
                s['pending']=None
            if s['position'] and wall_ms>s['position']['due_ms']+5000:s['position']['invalid']=True
        self.journal.save('paper',self.state)

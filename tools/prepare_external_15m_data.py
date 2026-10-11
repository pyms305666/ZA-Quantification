"""Choose representative products using training data only and attach past context."""
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from tools.research_15m_profit import load,CACHE as OLD,HALF,save_json
from research.intraday_profit import closed_features

CACHE=ROOT/'.tqsdk/research/external15m'
OUT=ROOT/'docs/研究/2026-09-26-外部模型验证'
LOOKBACK=128


def main():
    OUT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    meta=json.loads((OLD/'metadata.json').read_text(encoding='utf-8'))
    train=load('train',meta)
    ranking=[]
    for pi,product in enumerate(meta['products']):
        z=train[2]==pi;x=train[0][z];m=train[1][z]
        if len(x)<2000:continue
        atr=x[:,7]*m[:,3];distance=np.maximum(m[:,5],np.ceil(1.5*atr/m[:,5])*m[:,5])
        cost=(10+2*m[:,5]*m[:,4])/(distance*m[:,4])
        margin=m[:,3]*m[:,4]*.15
        if np.median(margin)>20000 or np.median(cost)>.35:continue
        ranking.append(dict(product=product,id=pi,train_samples=len(x),
            median_cost_R=float(np.median(cost)),median_margin=float(np.median(margin)),
            median_OI=float(np.median(m[:,2]))))
    ranking.sort(key=lambda r:(-r['median_OI'],r['product']))
    selected=ranking[:8]
    if len(selected)<4:raise RuntimeError('not enough training-qualified products')
    protocol=dict(selected_products=selected,eligible_ranking=ranking,lookback=LOOKBACK,
        selection='training only: >=2000 sampled rows; median margin<=20000; median cost/stop<=0.35; top8 current OI',
        train_period='2021-09-26 to 2024-09-25',validation_period='2025-08-01 to 2025-09-25',
        historical_period='2025-09-26 to 2026-09-24; previously viewed',
        kronos_model='Kronos-small',context=LOOKBACK,sample_count=4,temperature=.6,top_p=.9,
        kronos_edge_grid=[0,.1,.2],recipes=[[1,1.5],[2,1.5],[2,2]],
        meta_primary=['MA4_MA16_crossover','Bollinger16_2sd_reversion'],meta_probability_grid=[.5,.55,.6],
        meta_expected_net_R_floor=0,meta_random_forest=dict(n_estimators=200,max_depth=4,min_samples_leaf=30,seed=20260926),
        approval=dict(min_validation_trades=100,min_win_rate=.5,max_drawdown=.1,net_positive=True),
        fee=10,slippage_ticks=1,initial_cash=50000,position_limit=1,
        data_downloader_entitlement=False,backtest_entitlement=True)
    save_json(OUT/'protocol.json',protocol)
    selected_ids=[r['id'] for r in selected]
    partitions={}
    for split in ('train','validation','test'):
        x,m,p=load(split,meta);mask=np.isin(p,selected_ids)
        if split=='validation':mask &= m[:,0]>=HALF
        partitions[split]=[x[mask],m[mask],p[mask]]
    symbols=sorted(set(int(si) for a in partitions.values() for si in a[1][:,1]))
    contexts={split:np.zeros((len(a[0]),LOOKBACK,6),dtype='float32') for split,a in partitions.items() if split!='train'}
    stamps={split:np.zeros((len(a[0]),LOOKBACK,5),dtype='float32') for split,a in partitions.items() if split!='train'}
    valid={split:np.zeros(len(a[0]),bool) for split,a in partitions.items()}
    primary={split:np.zeros((len(a[0]),2),dtype='int8') for split,a in partitions.items()}
    for count,si in enumerate(symbols):
        name=meta['symbols'][si]
        raw=(ROOT/'.tqsdk/research/history'/f'{name}.json.gz').read_bytes()
        payload=json.loads(gzip.decompress(raw));d,_=closed_features(payload['bars']['900'])
        c=d.close;fast=c.rolling(4).mean();slow=c.rolling(16).mean()
        cross=np.where((fast>slow)&(fast.shift()<=slow.shift()),1,
                       np.where((fast<slow)&(fast.shift()>=slow.shift()),-1,0))
        sd=c.rolling(16).std();revert=np.where(c<slow-2*sd,1,np.where(c>slow+2*sd,-1,0))
        # Do not create an event from a crossover across a session break.
        cross=np.where(d.time.diff()==900000,cross,0)
        ts=pd.to_datetime(d.time,unit='ms',utc=True).dt.tz_convert('Asia/Shanghai')
        time_array=np.column_stack([ts.dt.minute,ts.dt.hour,ts.dt.weekday,ts.dt.day,ts.dt.month]).astype('float32')
        raw_x=d[['open','high','low','close','volume']].to_numpy()
        # Same proxy as the official predictor when amount is not provided.
        amount=d.volume*d[['open','high','low','close']].mean(axis=1)
        raw_x=np.column_stack([raw_x,amount]).astype('float32')
        for split,(x,m,p) in partitions.items():
            ix=np.flatnonzero(m[:,1]==si)
            if not len(ix):continue
            positions=np.searchsorted(d.time.to_numpy(),m[ix,0]-900000)
            for destination,pos in zip(ix,positions):
                if pos>=len(d) or d.time.iloc[pos]!=m[destination,0]-900000 or pos<LOOKBACK-1:continue
                window=raw_x[pos-LOOKBACK+1:pos+1]
                if not np.isfinite(window).all() or (window[:,:4]<=0).any():continue
                valid[split][destination]=True
                primary[split][destination]=[cross[pos],revert[pos]]
                if split!='train':
                    contexts[split][destination]=window
                    stamps[split][destination]=time_array[pos-LOOKBACK+1:pos+1]
        if count%20==0:print('context',count,'/',len(symbols),flush=True)
    counts={}
    for split,(x,m,p) in partitions.items():
        ok=valid[split];counts[split]=dict(before=len(ok),kept=int(ok.sum()),missing_context=int((~ok).sum()))
        data=dict(x=x[ok],m=m[ok],products=p[ok],primary=primary[split][ok])
        if split!='train':data.update(context=contexts[split][ok],stamps=stamps[split][ok])
        np.savez_compressed(CACHE/f'{split}.npz',**data)
        print('saved',split,counts[split],flush=True)
    save_json(CACHE/'metadata.json',dict(universe=meta,protocol=protocol,counts=counts,
        cache_sha256={split:hashlib.sha256((CACHE/f'{split}.npz').read_bytes()).hexdigest() for split in partitions}))


if __name__=='__main__':main()

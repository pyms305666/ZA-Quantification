"""Closed-bar features, conservative bracket outcomes and single-position replay.

Only historical OHLC modelling; no order submission or tick fill claims.
"""
import numpy as np
import pandas as pd
from research.direction_model import FEATURES

RECIPES = [(1, 1.5), (2, 1.5), (2, 2.0)]
META = ['time', 'symbol', 'oi', 'close', 'multiple', 'tick', 'expiry',
        'o1', 'h1', 'l1', 'c1', 'o2', 'h2', 'l2', 'c2', 'valid_future']


def closed_features(rows):
    d = pd.DataFrame(rows, columns=['time','open','high','low','close','volume','oi'])
    d = d.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    c = d.close.where((d.low > 0) & (d.high >= d[['open','close','low']].max(axis=1))
                     & (d.low <= d[['open','close','high']].min(axis=1)))
    tr = pd.concat([d.high-d.low,(d.high-c.shift()).abs(),(d.low-c.shift()).abs()],axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    x = pd.DataFrame(index=d.index)
    for n in (1,4,16,48): x[f'r{n}'] = c/c.shift(n)-1
    for n in (4,16,48): x[f'trend{n}'] = c/c.rolling(n).mean()-1
    x['atr14'] = atr/c
    x['volatility16'] = c.pct_change(fill_method=None).rolling(16).std()
    x['volume_relative'] = np.log1p(d.volume)-np.log1p(d.volume.rolling(16).mean())
    x['oi_change4'] = np.log1p(d.oi)-np.log1p(d.oi.shift(4))
    x['range_relative'] = (d.high-d.low)/atr.replace(0,np.nan)
    x['body_relative'] = (c-d.open)/atr.replace(0,np.nan)
    x['gap_relative'] = (d.open-c.shift())/atr.replace(0,np.nan)
    lo=d.low.rolling(16).min(); hi=d.high.rolling(16).max()
    x['location16'] = (c-lo)/(hi-lo).replace(0,np.nan)
    minutes=((d.time//60000)+480)%1440
    x['clock_sin']=np.sin(2*np.pi*minutes/1440)
    x['clock_cos']=np.cos(2*np.pi*minutes/1440)
    return d,x


def contract_examples(rows, instrument, symbol):
    if len(rows)<60: return None
    d,x=closed_features(rows)
    expiry=instrument.get('expire_datetime') or 0
    x['days_to_expiry']=np.clip((expiry*1000-d.time)/86400000,0,1000) if expiry else 0
    known=d.time+900000
    good=np.isfinite(x[FEATURES]).all(axis=1)&(d.volume>=100)&(d.oi>=100)&(d.close>0)
    future=(d.time.shift(-2)-d.time==1800000)&(d.time.shift(-1)-d.time==900000)
    # Conservative no crossing civil midnight; no crossing any observed break.
    future &= ((known+28800000)//86400000 == (known+1800000+28800000)//86400000)
    if expiry: future &= (known+1800000 < expiry*1000)
    m=pd.DataFrame({'time':known,'symbol':symbol,'oi':d.oi,'close':d.close,
                    'multiple':instrument['volume_multiple'],'tick':instrument['price_tick'],
                    'expiry':expiry})
    for n in (1,2):
        for a,b in [('o','open'),('h','high'),('l','low'),('c','close')]:
            m[a+str(n)]=d[b].shift(-n)
    future &= np.isfinite(m[[k for k in META if k not in ('valid_future',)]]).all(axis=1)
    for n in (1,2):
        future &= (m['l'+str(n)]>0)&(m['h'+str(n)]>=m[['o'+str(n),'c'+str(n),'l'+str(n)]].max(axis=1))
        future &= m['l'+str(n)]<=m[['o'+str(n),'c'+str(n),'h'+str(n)]].min(axis=1)
    m['valid_future']=future.astype(int)
    # Keep current eligible contracts even if future data is missing, so active
    # contract selection cannot use future quote availability.
    return x.loc[good,FEATURES].to_numpy(),m.loc[good,META].to_numpy()


def outcomes(x,m,recipe,fee=10,slippage=1):
    n,target_r=RECIPES[recipe]
    tick=m[:,5]; mult=m[:,4]
    distance=np.maximum(tick,np.ceil(1.5*x[:,7]*m[:,3]/tick)*tick)
    risks=distance*mult+fee+2*slippage*tick*mult
    pnl=[]; exit_times=[]; worst=[]
    for side in (1,-1):
        entry=m[:,7]+side*slippage*tick
        stop=entry-side*distance
        target=entry+side*np.ceil(target_r*distance/tick)*tick
        alive=np.ones(len(m),bool)
        price=m[:,10 if n==1 else 14].copy()
        when=m[:,0]+n*900000
        adverse=np.zeros(len(m))
        for j in range(n):
            o,h,l,c=(m[:,7+4*j+k] for k in range(4))
            bad_open=(o<=stop) if side==1 else (o>=stop)
            bad_touch=(l<=stop) if side==1 else (h>=stop)
            good_touch=(h>=target) if side==1 else (l<=target)
            stop_hit=alive&(bad_open|bad_touch)
            take_hit=alive&~stop_hit&good_touch
            # If stop is touched first, adverse move is capped at stop except
            # opening gaps. Otherwise use the full candle extreme, conservatively.
            extreme=(l if side==1 else h).copy()
            extreme[stop_hit]=np.where(bad_open[stop_hit],o[stop_hit],stop[stop_hit])
            adverse=np.minimum(adverse,np.where(alive,side*(extreme-entry)*mult-fee-slippage*tick*mult,0))
            price[stop_hit]=np.where(bad_open[stop_hit],o[stop_hit],stop[stop_hit])
            price[take_hit]=target[take_hit]
            when[stop_hit|take_hit]=m[stop_hit|take_hit,0]+(j+1)*900000
            alive &= ~(stop_hit|take_hit)
        net=side*(price-side*slippage*tick-entry)*mult-fee
        pnl.append(net);exit_times.append(when);worst.append(adverse)
    return np.column_stack(pnl),np.column_stack(exit_times),np.column_stack(worst),risks


def replay(x,m,mean,win,recipe,edge,win_floor,products,allowed,slots,fee=10,slippage=1):
    pnl,ends,worst,risk=outcomes(x,m,recipe,fee,slippage)
    side=np.argmax(mean,axis=1); index=np.arange(len(m))
    eligible=(mean[index,side]>=edge)&(win[index,side]>=win_floor)
    eligible &= np.isin(products,list(allowed)) & np.isin(((m[:,0]//3600000+8)%24),list(slots))
    score=mean[index,side]
    order=np.lexsort((-score,m[:,0]))
    balance=50000.;peak=balance;drawdown=0.;locked=-1;ledger=[]
    for i in order:
        if not eligible[i] or m[i,0]<locked: continue
        budget=min(900.,balance*.018)
        margin=m[i,7]*m[i,4]*.15
        lots=min(10,int(budget/risk[i]),int(balance/max(margin,1)))
        if lots<1:continue
        s=side[i];before=balance
        drawdown=max(drawdown,(peak-(balance+lots*worst[i,s]))/peak)
        balance+=lots*pnl[i,s]
        peak=max(peak,balance);drawdown=max(drawdown,(peak-balance)/peak)
        locked=ends[i,s]
        ledger.append([int(m[i,0]),int(locked),int(m[i,1]),int(products[i]),
                       1 if s==0 else -1,lots,float(pnl[i,s]*lots),balance,float(before)])
        if balance<=0:break
    profits=[r[6] for r in ledger];pos=sum(v for v in profits if v>0);neg=-sum(v for v in profits if v<0)
    return dict(trades=len(ledger),net=balance-50000,return_fraction=(balance-50000)/50000,
                max_drawdown=drawdown,win_rate=sum(v>0 for v in profits)/len(profits) if profits else None,
                profit_factor=pos/neg if neg else None,eligible=int(eligible.sum()),
                coverage=float(eligible.mean()) if len(m) else 0,
                executed_coverage=len(ledger)/len(m) if len(m) else 0,
                ledger=ledger)

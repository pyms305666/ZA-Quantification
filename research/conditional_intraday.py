"""Past-only market context and fixed-time, stop-protected trade payoffs."""
import numpy as np
import pandas as pd
from research.intraday_profit import closed_features

CONTEXT_NAMES=['segment_return_atr','segment_vwap_deviation_atr','volatility_ratio',
               'path_efficiency_4','segment_age','clock_quarter']


def contextual_features(rows):
    d,_=closed_features(rows)
    segment=(d.time.diff()!=900000).cumsum()
    first=d.open.groupby(segment).transform('first')
    typical=d[['open','high','low','close']].mean(axis=1)
    volume=d.volume.groupby(segment).cumsum()
    vwap=(typical*d.volume).groupby(segment).cumsum()/volume.replace(0,np.nan)
    tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean().replace(0,np.nan)
    ret=d.close.pct_change(fill_method=None)
    path=d.close.diff().abs().rolling(4).sum().replace(0,np.nan)
    extra=np.column_stack([(d.close-first)/atr,(d.close-vwap)/atr,
        ret.rolling(4).std()/ret.rolling(48).std().replace(0,np.nan),
        (d.close-d.close.shift(4)).abs()/path,
        d.groupby(segment).cumcount().clip(upper=48),((d.time//900000+32)%96)])
    return d.time.to_numpy(),extra


def fixed_outcomes(x,m,horizon,fee=10,slippage=1):
    """Next-open entry; stop at 1.5 ATR; exit at horizon close; no take-profit."""
    tick=m[:,5];mult=m[:,4]
    distance=np.maximum(tick,np.ceil(1.5*x[:,7]*m[:,3]/tick)*tick)
    risk=distance*mult+fee+2*slippage*tick*mult
    pnls=[];exits=[];worsts=[]
    for side in (1,-1):
        entry=m[:,7]+side*slippage*tick;stop=entry-side*distance
        alive=np.ones(len(m),bool);price=m[:,10 if horizon==1 else 14].copy()
        end=m[:,0]+horizon*900000;adverse=np.zeros(len(m))
        for j in range(horizon):
            o,h,l,_=(m[:,7+4*j+k] for k in range(4))
            gap=(o<=stop) if side==1 else (o>=stop)
            touch=(l<=stop) if side==1 else (h>=stop)
            hit=alive&(gap|touch)
            extreme=(l if side==1 else h).copy()
            extreme[hit]=np.where(gap[hit],o[hit],stop[hit])
            adverse=np.minimum(adverse,np.where(alive,side*(extreme-entry)*mult-fee-slippage*tick*mult,0))
            price[hit]=np.where(gap[hit],o[hit],stop[hit]);end[hit]=m[hit,0]+(j+1)*900000
            alive &= ~hit
        pnls.append(side*(price-side*slippage*tick-entry)*mult-fee)
        exits.append(end);worsts.append(adverse)
    return np.column_stack(pnls),np.column_stack(exits),np.column_stack(worsts),risk


def replay(x,m,p,mean,prob,horizon,edge,floor,fee=10,slippage=1):
    pnl,ends,worst,risk=fixed_outcomes(x,m,horizon,fee,slippage)
    # Gate each direction before choosing, so one rejected side cannot hide a
    # qualifying alternative. Scores always refer to default-cost predictions.
    qualified=(mean>=edge)&(prob>=floor)
    costs=10+2*m[:,5]*m[:,4]
    normal_distance=np.maximum(m[:,5],np.ceil(1.5*x[:,7]*m[:,3]/m[:,5])*m[:,5])
    qualified &= (costs/(normal_distance*m[:,4])<=.35)[:,None]
    scores=np.where(qualified,mean,-np.inf);side=scores.argmax(axis=1)
    eligible=qualified.any(axis=1);order=np.lexsort((-scores[np.arange(len(m)),side],m[:,0]))
    balance=50000.;peak=balance;dd=0.;locked=-1;ledger=[]
    for i in order:
        if not eligible[i] or m[i,0]<locked:continue
        lots=min(10,int(min(900,balance*.018)/risk[i]),int(balance/max(m[i,7]*m[i,4]*.15,1)))
        if lots<1:continue
        s=side[i];before=balance;dd=max(dd,(peak-balance-lots*worst[i,s])/peak)
        balance+=lots*pnl[i,s];peak=max(peak,balance);dd=max(dd,(peak-balance)/peak)
        locked=ends[i,s]
        ledger.append([int(m[i,0]),int(locked),int(m[i,1]),int(p[i]),1 if s==0 else -1,
                       lots,float(pnl[i,s]*lots),float(balance),float(before)])
        if balance<=0:break
    profits=np.array([a[6] for a in ledger]);negative=-profits[profits<0].sum()
    return dict(trades=len(ledger),net=float(balance-50000),return_fraction=float((balance-50000)/50000),
        max_drawdown=float(dd),win_rate=float((profits>0).mean()) if len(profits) else None,
        profit_factor=float(profits[profits>0].sum()/negative) if negative else None,
        eligible=int(eligible.sum()),coverage=float(eligible.mean()),executed_coverage=len(ledger)/len(m),ledger=ledger)

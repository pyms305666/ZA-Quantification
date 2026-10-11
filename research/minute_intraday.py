"""Minute-path features and stop-protected execution for 15-minute decisions."""
import numpy as np

FEATURE_NAMES=['minute_realized_vol_atr','minute_path_efficiency','minute_return_first5_atr',
    'minute_return_last5_atr','minute_volume_last3_fraction','minute_up_volume_proxy',
    'minute_vwap_deviation_atr','minute_return_autocorrelation','minute_max_step_atr']


def features(past,atr):
    # Shape (events,15,[open,high,low,close,volume,oi]); all closed by decision.
    opening=past[:,:,0];close=past[:,:,3];volume=past[:,:,4]
    delta=np.diff(np.column_stack([opening[:,0],close]),axis=1)
    path=np.abs(delta).sum(axis=1);scale=np.maximum(atr,1e-10)
    v=volume.sum(axis=1);typical=past[:,:,:4].mean(axis=2)
    vwap=(typical*volume).sum(axis=1)/np.maximum(v,1)
    centered=delta-delta.mean(axis=1,keepdims=True)
    corr=(centered[:,:-1]*centered[:,1:]).sum(axis=1)/np.maximum((centered**2).sum(axis=1),1e-10)
    return np.column_stack([np.sqrt((delta**2).sum(axis=1))/scale,
        np.abs(close[:,-1]-opening[:,0])/np.maximum(path,1e-10),
        (close[:,4]-opening[:,0])/scale,(close[:,-1]-opening[:,10])/scale,
        volume[:,-3:].sum(axis=1)/np.maximum(v,1),
        (np.sign(close-opening)*volume).sum(axis=1)/np.maximum(v,1),
        (close[:,-1]-vwap)/scale,corr,np.abs(delta).max(axis=1)/scale])


def outcomes(x,m,future,horizon,fee=10,slippage=1):
    tick=m[:,5];mult=m[:,4]
    distance=np.maximum(tick,np.ceil(1.5*x[:,7]*m[:,3]/tick)*tick)
    risk=distance*mult+fee+2*slippage*tick*mult
    pnls=[];ends=[];worsts=[]
    for side in (1,-1):
        entry=future[:,0,0]+side*slippage*tick;stop=entry-side*distance
        alive=np.ones(len(m),bool);price=future[:,horizon*15-1,3].copy()
        end=m[:,0]+horizon*900000;worst=np.zeros(len(m))
        for j in range(horizon*15):
            o,h,l,_=(future[:,j,k] for k in range(4))
            gap=(o<=stop) if side==1 else (o>=stop)
            touched=(l<=stop) if side==1 else (h>=stop);hit=alive&(gap|touched)
            extreme=(l if side==1 else h).copy()
            extreme[hit]=np.where(gap[hit],o[hit],stop[hit])
            worst=np.minimum(worst,np.where(alive,side*(extreme-entry)*mult-fee-slippage*tick*mult,0))
            price[hit]=np.where(gap[hit],o[hit],stop[hit]);end[hit]=m[hit,0]+(j+1)*60000
            alive &= ~hit
        pnls.append(side*(price-side*slippage*tick-entry)*mult-fee);ends.append(end);worsts.append(worst)
    return np.column_stack(pnls),np.column_stack(ends),np.column_stack(worsts),risk


def replay(x,m,p,future,mean,prob,horizon,edge,floor,fee=10,slippage=1):
    pnl,ends,worst,risk=outcomes(x,m,future,horizon,fee,slippage)
    qualified=(mean>=edge)&(prob>=floor)
    distance=np.maximum(m[:,5],np.ceil(1.5*x[:,7]*m[:,3]/m[:,5])*m[:,5])
    qualified &= ((10+2*m[:,5]*m[:,4])/(distance*m[:,4])<=.35)[:,None]
    score=np.where(qualified,mean,-np.inf);side=score.argmax(axis=1);eligible=qualified.any(axis=1)
    order=np.lexsort((-score[np.arange(len(m)),side],m[:,0]))
    balance=50000.;peak=balance;dd=0.;locked=-1;ledger=[]
    for i in order:
        if not eligible[i] or m[i,0]<locked:continue
        lots=min(10,int(min(900,balance*.018)/risk[i]),int(balance/max(future[i,0,0]*m[i,4]*.15,1)))
        if lots<1:continue
        s=side[i];before=balance;dd=max(dd,(peak-balance-lots*worst[i,s])/peak)
        balance+=lots*pnl[i,s];peak=max(peak,balance);dd=max(dd,(peak-balance)/peak);locked=ends[i,s]
        ledger.append([int(m[i,0]),int(locked),int(m[i,1]),int(p[i]),1 if s==0 else -1,lots,
                       float(pnl[i,s]*lots),float(balance),float(before)])
        if balance<=0:break
    profits=np.array([a[6] for a in ledger]);loss=-profits[profits<0].sum()
    return dict(trades=len(ledger),net=float(balance-50000),max_drawdown=float(dd),
        win_rate=float((profits>0).mean()) if len(profits) else None,
        profit_factor=float(profits[profits>0].sum()/loss) if loss else None,
        coverage=float(eligible.mean()),eligible=int(eligible.sum()),executed_coverage=len(ledger)/len(m),ledger=ledger)

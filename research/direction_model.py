"""Past-only, normalized features and chronological, purged label partitions."""
import numpy as np
import pandas as pd

FEATURES=['r1','r4','r16','r48','trend4','trend16','trend48','atr14','volatility16',
          'volume_relative','oi_change4','range_relative','body_relative','gap_relative',
          'location16','days_to_expiry','clock_sin','clock_cos']
START=int(pd.Timestamp('2021-09-26',tz='Asia/Shanghai').timestamp()*1000)
VAL_START=int(pd.Timestamp('2024-09-26',tz='Asia/Shanghai').timestamp()*1000)
TEST_START=int(pd.Timestamp('2025-09-26',tz='Asia/Shanghai').timestamp()*1000)
END=int(pd.Timestamp('2026-09-24 15:00',tz='Asia/Shanghai').timestamp()*1000)+1


def examples(rows, period, horizon, meta, trading_days=None):
    """Return features, label return/cost, source and target close timestamps.

    Target is future close / observed close - 1, not an executable order return.
    Period-900 rows must be adjacent in clock time to label exactly 15 minutes.
    """
    if len(rows)<horizon+60:return None
    d=pd.DataFrame(rows,columns=['time','open','high','low','close','volume','oi'])
    d=d.drop_duplicates('time').sort_values('time').reset_index(drop=True)
    c=d['close']; previous=c.shift(1)
    valid_prices=(c>0)&(d['low']>0)&(d['high']>=d[['open','close','low']].max(axis=1))&(d['low']<=d[['open','close','high']].min(axis=1))
    # Invalid prices are excluded from current examples, never filled forward.
    c=c.where(valid_prices)
    ret=c.pct_change(fill_method=None)
    tr=pd.concat([d['high']-d['low'],(d['high']-previous).abs(),(d['low']-previous).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean()
    x=pd.DataFrame(index=d.index)
    for n in (1,4,16,48):x[f'r{n}']=c/c.shift(n)-1
    for n in (4,16,48):x[f'trend{n}']=c/c.rolling(n).mean()-1
    x['atr14']=atr/c
    x['volatility16']=ret.rolling(16).std()
    x['volume_relative']=np.log1p(d['volume'])-np.log1p(d['volume'].rolling(16).mean())
    x['oi_change4']=np.log1p(d['oi'])-np.log1p(d['oi'].shift(4))
    x['range_relative']=(d['high']-d['low'])/atr.replace(0,np.nan)
    x['body_relative']=(c-d['open'])/atr.replace(0,np.nan)
    x['gap_relative']=(d['open']-previous)/atr.replace(0,np.nan)
    lo=d['low'].rolling(16).min(); hi=d['high'].rolling(16).max()
    x['location16']=(c-lo)/(hi-lo).replace(0,np.nan)
    expiry=meta.get('expire_datetime')
    x['days_to_expiry']=np.clip((expiry*1000-d['time'])/86400000,0,1000) if expiry else 0
    minutes=((d['time']//60000)+8*60)%(24*60)
    x['clock_sin']=np.sin(2*np.pi*minutes/1440)
    x['clock_cos']=np.cos(2*np.pi*minutes/1440)
    timestamp=d['time']+(900000 if period==900 else 15*3600000)
    if period==86400 and trading_days is not None:
        future_dates={t:trading_days[i+horizon] for i,t in enumerate(trading_days[:-horizon])}
        future_start=d['time'].map(future_dates)
        future_close=future_start.map(pd.Series(c.to_numpy(),index=d['time']))
        y=future_close/c-1
        target=future_start+15*3600000
    else:
        y=c.shift(-horizon)/c-1
        target=d['time'].shift(-horizon)+(900000 if period==900 else 15*3600000)
    multiple=float(meta['volume_multiple']); tick=float(meta['price_tick'])
    costs=(10+2*tick*multiple)/(c*multiple)
    valid=np.isfinite(x).all(axis=1)&np.isfinite(y)&np.isfinite(target)&(d['volume']>=100)&(d['oi']>=100)
    if period==900:valid&=(d['time'].shift(-horizon)-d['time']==900000)
    if expiry:valid&=(target<expiry*1000)
    values=np.column_stack([x[FEATURES],y,costs,timestamp,target])
    return values[valid.to_numpy()]


def split_indices(values):
    """Purge every sample whose forward target crosses its partition boundary."""
    now,target=values[:,-2],values[:,-1]
    return {'train':(now>=START)&(now<VAL_START)&(target<VAL_START),
            'validation':(now>=VAL_START)&(now<TEST_START)&(target<TEST_START),
            'test':(now>=TEST_START)&(now<END)&(target<END)}

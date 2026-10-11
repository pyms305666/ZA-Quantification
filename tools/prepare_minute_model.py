"""Join minute paths, audit against original bars, and add past-only features."""
import argparse
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from research.minute_intraday import features,outcomes
from research.conditional_intraday import fixed_outcomes
from tools.research_15m_profit import save_json

CACHE=ROOT/'.tqsdk/research/fine15m'
OUT=ROOT/'docs/研究/2026-09-26-分钟路径模型'


@lru_cache(maxsize=20)
def original(symbol):
    payload=json.loads(gzip.decompress((ROOT/f'.tqsdk/research/history/{symbol}.json.gz').read_bytes()))
    return {a[0]:a[1:] for a in payload['bars']['900']}


def main(partial=False):
    OUT.mkdir(parents=True,exist_ok=True)
    source=ROOT/'.tqsdk/research/conditional15m_low_cost/data.npz';d=np.load(source)
    x,m,p=(d[a] for a in ('x','m','p'))
    meta=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))
    labels=pd.to_datetime(m[:,0],unit='ms',utc=True).tz_convert('Asia/Shanghai').strftime('%Y-%m')
    symbols=meta['universe']['symbols'];xs=[];ms=[];ps=[];fs=[];past_kept=[]
    audit=[];fingerprints={};missing=0
    for label in sorted(set(labels)):
        for si in np.unique(m[labels==label,1]).astype(int):
            symbol=symbols[si];path=CACHE/f'minutes/{symbol}-{label}.json.gz'
            ix=np.flatnonzero((labels==label)&(m[:,1]==si))
            if not path.exists():missing+=1;continue
            fingerprints[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
            raw=np.array(json.loads(gzip.decompress(path.read_bytes()))['bars'],dtype='float64')
            if not len(raw):audit.append(dict(symbol=symbol,month=label,opportunities=len(ix),complete=0,matched=0));continue
            raw=raw[np.argsort(raw[:,0])];times=raw[:,0]
            needed=m[ix,0,None]+np.arange(-15,30)[None,:]*60000
            pos=np.searchsorted(times,needed);bounded=np.minimum(pos,len(raw)-1)
            complete=(pos<len(raw)).all(axis=1)&(times[bounded]==needed).all(axis=1)
            series=raw[bounded];valid_ohlc=(series[:,:,3]>0)&(series[:,:,2]>=series[:,:,[1,3,4]].max(axis=2))&(series[:,:,3]<=series[:,:,[1,2,4]].min(axis=2))
            complete &= valid_ohlc.all(axis=1)&np.isfinite(series).all(axis=(1,2))
            full=np.flatnonzero(complete);selected=series[full];kept=ix[full]
            matching=np.ones(len(full),bool);maximum=0.
            for j in range(3):
                block=selected[:,15*j:15*(j+1),:]
                aggregate=np.column_stack([block[:,0,1],block[:,:,2].max(axis=1),block[:,:,3].min(axis=1),block[:,-1,4]])
                if j==0:
                    expected=np.array([original(symbol)[t-900000][:4] for t in m[kept,0]])
                else:expected=m[kept,7+4*(j-1):11+4*(j-1)]
                if len(full):
                    error=np.abs(aggregate-expected);maximum=max(maximum,float(error.max()))
                    matching &= (error<=1e-7).all(axis=1)
            if len(full):
                expected_volume=np.array([original(symbol)[t-900000][4] for t in m[kept,0]])
                volume_match=np.isclose(selected[:,:15,5].sum(axis=1),expected_volume,rtol=0,atol=1e-6)
                matching &= volume_match
            good=kept[matching];segments=selected[matching]
            new=features(segments[:,:15,1:],x[good,7]*m[good,3])
            xs.append(np.column_stack([x[good],new]));ms.append(m[good]);ps.append(p[good])
            fs.append(segments[:,15:,1:]);past_kept.append(segments[:,:15,1:])
            audit.append(dict(symbol=symbol,month=label,opportunities=len(ix),complete=int(complete.sum()),
                matched=len(good),max_ohlc_difference=maximum,
                zero_volume_entry_minutes=int((segments[:,15,5]<=0).sum())))
    result=dict(source_opportunities=len(m),missing_files=missing,checked_files=len(audit),
        complete=sum(a['complete'] for a in audit),matched=sum(a['matched'] for a in audit),
        audit=audit,source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),minute_sha256=fingerprints)
    result['zero_volume_entry_minutes']=sum(a.get('zero_volume_entry_minutes',0) for a in audit)
    if xs:
        combined_x=np.concatenate(xs);combined_m=np.concatenate(ms);combined_f=np.concatenate(fs)
        result['execution_resolution_audit']={}
        for horizon in (1,2):
            coarse=fixed_outcomes(combined_x,combined_m,horizon)[0]
            fine=outcomes(combined_x,combined_m,combined_f,horizon)[0]
            change=fine-coarse
            if (change>1e-7).any():raise AssertionError('finer stop-only fills unexpectedly improve per-lot PnL')
            result['execution_resolution_audit'][str(horizon)]=dict(direction_opportunities=int(change.size),
                worsened=int((change < -1e-7).sum()),maximum_extra_loss_per_lot=float(-change.min()),
                mean_pnl_difference=float(change.mean()))
    if partial:
        print(json.dumps({k:v for k,v in result.items() if k not in ('audit','minute_sha256')},ensure_ascii=False),flush=True)
        return
    if missing:raise RuntimeError(f'{missing} minute snapshot files still missing')
    np.savez_compressed(CACHE/'model-data.npz',x=np.concatenate(xs),m=np.concatenate(ms),p=np.concatenate(ps),
        future=np.concatenate(fs),past=np.concatenate(past_kept))
    result['data_sha256']=hashlib.sha256((CACHE/'model-data.npz').read_bytes()).hexdigest()
    save_json(OUT/'data-audit.json',result)
    print('minute matching',result['matched'],'/',len(m),'complete',result['complete'],flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--audit-partial',action='store_true');args=parser.parse_args();main(args.audit_partial)

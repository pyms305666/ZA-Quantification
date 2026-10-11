"""Additional fixed-parameter event comparison using all available training rows.

Motivation was low event count in the first validation, before historical
evaluation. Keep original sampled models and predictions unchanged.
"""
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from research.direction_model import START,VAL_START
from research.intraday_profit import contract_examples,closed_features
from tools.research_15m_cost_features import augment
import tools.validate_external_15m as bench
from tools.research_15m_profit import save_json


def main():
    meta=json.loads((bench.CACHE/'metadata.json').read_text(encoding='utf-8'))
    protocol=dict(reason='Sampled training only yielded 1343/2809 events and no validation signals; use all available training rows.',
        adaptive_validation_extension=True,historical_evaluation_not_used_for_choice=True,
        model_parameters_unchanged=True,validation_and_historical_datasets_unchanged=True)
    save_json(bench.OUT/'full-event-protocol.json',protocol)
    symbols=meta['universe']['symbols'];products=meta['universe']['products']
    lookup={s:i for i,s in enumerate(symbols)};xs=[];ms=[];ps=[];events=[]
    for selected in meta['protocol']['selected_products']:
        product=selected['product'];pi=selected['id'];parts=[]
        # Exact product membership from the previous inventory, not code prefixes.
        for path in sorted((ROOT/'.tqsdk/research/history').glob('*.json.gz')):
            symbol=path.name[:-8]
            if not symbol.startswith(product):continue
            payload=json.loads(gzip.decompress(path.read_bytes()));instrument=payload['meta']
            if instrument['exchange_id']+'.'+instrument['product_id']!=product:continue
            result=contract_examples(payload['bars']['900'],instrument,lookup[symbol])
            if result is None or not len(result[0]):continue
            x,m=result;d,_=closed_features(payload['bars']['900']);c=d.close
            fast=c.rolling(4).mean();slow=c.rolling(16).mean();sd=c.rolling(16).std()
            cross=np.where((fast>slow)&(fast.shift()<=slow.shift()),1,
                           np.where((fast<slow)&(fast.shift()>=slow.shift()),-1,0))
            cross=np.where(d.time.diff()==900000,cross,0)
            revert=np.where(c<slow-2*sd,1,np.where(c>slow+2*sd,-1,0))
            pos=np.searchsorted(d.time.to_numpy(),m[:,0]-900000)
            valid_history=(np.isfinite(d[['open','high','low','close','volume']]).all(axis=1)&
                           (d[['open','high','low','close']]>0).all(axis=1)).rolling(128).sum().to_numpy()==128
            parts.append(np.column_stack([x,m,cross[pos],revert[pos],valid_history[pos]]))
        a=np.concatenate(parts);m=a[:,18:34]
        order=np.lexsort((m[:,1],-m[:,2],m[:,0]));a=a[order];m=a[:,18:34]
        first=np.r_[True,m[1:,0]!=m[:-1,0]];a=a[first];m=a[:,18:34]
        good=(m[:,-1]==1)&(a[:,-1]==1)&(m[:,0]>=START)&(m[:,0]+1800000<VAL_START)
        a=a[good];xs.append(a[:,:18]);ms.append(a[:,18:34]);ps.append(np.full(len(a),pi));events.append(a[:,34:36].astype('int8'))
        print('full training',product,len(a),flush=True)
    x=np.concatenate(xs);m=np.concatenate(ms);p=np.concatenate(ps);primary=np.concatenate(events)
    target=bench.CACHE/'full_events';target.mkdir(exist_ok=True)
    np.savez_compressed(target/'train.npz',x=x,m=m,products=p,primary=primary)
    # Reuse the verified fitter with a disjoint cache and unchanged evaluation data.
    original_cache=bench.CACHE
    def load(split):
        if split=='train':
            a=augment((x,m,p));return *a,primary
        d=np.load(original_cache/f'{split}.npz')
        a=augment((d['x'],d['m'],d['products']))
        return *a,d['primary']
    bench.CACHE=target;bench.load=load
    bench.meta()
    save_json(bench.OUT/'full-event-training.json',dict(samples=len(x),
        MA_events=int((primary[:,0]!=0).sum()),BB_events=int((primary[:,1]!=0).sum()),
        train_sha256=hashlib.sha256((target/'train.npz').read_bytes()).hexdigest(),protocol=protocol))


if __name__=='__main__':main()

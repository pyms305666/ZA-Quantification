"""Frozen zero-shot Kronos forecasts, using past context and scheduled times only."""
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.tqsdk/research/external15m'
sys.path.insert(0,str(CACHE/'source'))
import numpy as np
import pandas as pd
import torch
from model import Kronos,KronosTokenizer,KronosPredictor

BATCH=64
SEED=20260926


def main():
    torch.set_num_threads(4);torch.set_float32_matmul_precision('high')
    if not torch.cuda.is_available():raise RuntimeError('CUDA runtime unavailable')
    model=Kronos.from_pretrained(str(CACHE/'Kronos-small'),local_files_only=True)
    tokenizer=KronosTokenizer.from_pretrained(str(CACHE/'Kronos-Tokenizer-base'),local_files_only=True)
    predictor=KronosPredictor(model,tokenizer,device='cuda:0',max_context=512)
    artifact_hash={name:hashlib.sha256((CACHE/name/'model.safetensors').read_bytes()).hexdigest()
                   for name in ['Kronos-small','Kronos-Tokenizer-base']}
    run=dict(batch=BATCH,seed=SEED,temperature=.6,top_p=.9,sample_count=4,lookback=128,
             torch=version('torch'),device=torch.cuda.get_device_name(),tf32_precision='high',precision='autocast-float16',
             artifact_sha256=artifact_hash,source=json.loads((CACHE/'downloads.json').read_text(encoding='utf-8')))
    for split in ['validation','test']:
        dataset=CACHE/f'{split}.npz';fingerprint=hashlib.sha256(dataset.read_bytes()).hexdigest()
        output=CACHE/f'kronos-{split}.npz';check=CACHE/f'kronos-{split}-run.json'
        if output.exists() and check.exists():
            previous=json.loads(check.read_text(encoding='utf-8'))
            if (previous['dataset_sha256']!=fingerprint or previous['artifact_sha256']!=artifact_hash
                    or previous.get('precision')!=run['precision'] or previous.get('batch')!=BATCH):
                raise RuntimeError('cached inference input differs')
            print('reuse',split,flush=True);continue
        data=np.load(dataset);raw=data['context'];stamp=data['stamps'];m=data['m']
        forecast=np.zeros((len(m),2,6),dtype='float32');start=time.perf_counter()
        for offset in range(0,len(m),BATCH):
            torch.manual_seed(SEED+offset//BATCH)
            x=raw[offset:offset+BATCH]
            mean=x.mean(axis=1,keepdims=True);std=x.std(axis=1,keepdims=True)
            x=np.clip((x-mean)/(std+1e-5),-5,5)
            known=m[offset:offset+BATCH,0]
            # Future bar start dates are implied by continuous session horizon.
            future=pd.to_datetime(np.column_stack([known,known+900000]).ravel(),unit='ms',utc=True).tz_convert('Asia/Shanghai')
            y=np.column_stack([future.minute,future.hour,future.weekday,future.day,future.month]).reshape(len(x),2,5).astype('float32')
            with torch.autocast(device_type='cuda',dtype=torch.float16):
                prediction=predictor.generate(x,stamp[offset:offset+BATCH],y,2,.6,0,.9,4,False)
            prediction=prediction*(std+1e-5)+mean
            if not np.isfinite(prediction).all() or (prediction[:,:,:4]<=0).any():
                raise RuntimeError('invalid generated prices; no silent repair')
            forecast[offset:offset+len(x)]=prediction
            if offset%(BATCH*10)==0:
                print('Kronos',split,offset,'/',len(m),'seconds',round(time.perf_counter()-start),flush=True)
        np.savez_compressed(output,forecast=forecast)
        result=run|dict(dataset_sha256=fingerprint,rows=len(m),seconds=time.perf_counter()-start,
                        forecast_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                        inference_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        peak_gpu_memory_bytes=torch.cuda.max_memory_allocated())
        check.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Kronos saved',split,len(m),flush=True)


if __name__=='__main__':main()

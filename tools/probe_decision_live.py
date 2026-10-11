"""Read-only current-feed readiness check for the actual ultra evaluator."""
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from config import load_config,RiskConfig
from market.evaluator import evaluate
from market.model import Instrument
from tools.forward_research import frame_rows,metadata
from tqsdk import TqApi,TqAuth,TqSim

BASE=ROOT/'.tqsdk/research/decision_forward'
OUTPUT=ROOT/'docs/研究/2026-09-28-开盘观察'
FIELDS=('datetime','open','high','low','close','volume','open_interest')


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    parent=json.loads((ROOT/'.tqsdk/research/forward15m/status.json').read_text(encoding='utf-8'))
    selected=parent.get('selected_symbols',{})
    c=load_config().tqsdk
    api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),debug=False,disable_print=True)
    try:
        frames={};quotes={}
        for product,symbol in selected.items():
            quotes[symbol]=api.get_quote(symbol)
            frames[symbol]={p:api.get_kline_serial(symbol,p,data_length=201) for p in (60,300,900)}
        limit=time.time()+35
        while time.time()<limit and not all(api.is_serial_ready(f) for m in frames.values() for f in m.values()):
            api.wait_update(deadline=time.time()+1)
        now=int(time.time()*1000);result=[]
        for product,symbol in selected.items():
            q=quotes[symbol];meta=metadata(q)
            try:qt=int(datetime.fromisoformat(q.datetime).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()*1000)
            except (ValueError,TypeError):qt=0
            row=dict(product=product,symbol=symbol,quote_age_ms=now-qt,source_time=q.datetime,
                frame_ready={str(p):bool(api.is_serial_ready(f)) for p,f in frames[symbol].items()})
            if not all(row['frame_ready'].values()):
                row['reason']='serial_not_ready';result.append(row);continue
            inputs={p:[dict(zip(FIELDS,r)) for r in frame_rows(f,p,now)[-200:].tolist()] for p,f in frames[symbol].items()}
            row['closed_bars']={str(p):len(b) for p,b in inputs.items()}
            row['last_completed']={str(p):b[-1]['datetime']+p*1000 if b else None for p,b in inputs.items()}
            if -5000<=row['quote_age_ms']<=5000 and math.isfinite(q.last_price) and q.last_price>0:
                instrument=Instrument(symbol=symbol,exchange=symbol.split('.')[0],instrument_id=symbol.split('.')[1],
                    price_tick=meta['price_tick'],volume_multiple=int(meta['volume_multiple']),expire_datetime=meta['expire_datetime'])
                evaluation=evaluate(instrument,dict(last=float(q.last_price),timestamp=qt,**meta),inputs,RiskConfig(),'ultra',now)
                row.update(data_ok=evaluation['data_ok'],direction_en=evaluation['direction_en'],score=evaluation['score'],
                    rationale_count=len(evaluation['rationale']))
            else:row['reason']='stale_quote'
            result.append(row)
        report=dict(asof_ms=now,source_parent_pid=parent.get('pid'),symbols=result,
                    warning='Read-only probe; asynchronous snapshot is not a committed real-time prediction or forward label.')
        (OUTPUT/'开盘行情就绪快照.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
    finally:api.close()


if __name__=='__main__':main()

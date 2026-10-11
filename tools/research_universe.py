"""Inventory all domestic commodity futures, including expired contracts."""
from collections import Counter
from datetime import date, datetime, timezone
import gzip
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from config import load_config
    from tqsdk import TqApi,TqAuth,TqSim
    credentials=load_config().tqsdk
    api=TqApi(TqSim(),auth=TqAuth(credentials.account,credentials.password),debug=False,disable_print=True)
    records=[]
    calendar=None
    try:
        symbols=sorted(api.query_quotes(ins_class='FUTURE',exchange_id=['SHFE','INE','DCE','CZCE','GFEX']))
        print('all commodity futures',len(symbols),flush=True)
        fields=('instrument_id','exchange_id','product_id','instrument_name','price_tick','volume_multiple',
                'expired','expire_datetime','delivery_year','delivery_month')
        for offset in range(0,len(symbols),500):
            data=api.query_symbol_info(symbols[offset:offset+500])
            for row in data.to_dict('records'):
                records.append({k:(None if isinstance(row.get(k),float) and not math.isfinite(row[k]) else row.get(k)) for k in fields})
            print('inventory',min(offset+500,len(symbols)),'/',len(symbols),flush=True)
        calendar=api.get_trading_calendar(start_dt=date(2021,9,26),end_dt=date(2026,9,24))
    finally:api.close()
    cutoff=datetime(2021,9,26,tzinfo=timezone.utc).timestamp()
    selected=[r for r in records if r['expire_datetime'] is None or r['expire_datetime']>=cutoff]
    payload=dict(captured_at=datetime.now(timezone.utc).isoformat(),start='2021-09-26',end='2026-09-24',
        all_count=len(records),selected_count=len(selected),products=sorted({r['exchange_id']+'.'+r['product_id'] for r in selected}),
        exchange_counts=dict(Counter(r['exchange_id'] for r in selected)),contracts=selected)
    output=ROOT/'.tqsdk/research/universe.json.gz';output.parent.mkdir(parents=True,exist_ok=True)
    calendar.to_csv(output.parent/'trading_calendar.csv',index=False)
    with gzip.open(output,'wt',encoding='utf-8') as stream:json.dump(payload,stream,ensure_ascii=False,allow_nan=False)
    print('selected',payload['selected_count'],'products',len(payload['products']),'exchanges',payload['exchange_counts'],flush=True)


if __name__=='__main__':main()

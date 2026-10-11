"""Read-only connection and catalog probe for the forward research collector."""
from datetime import date,timedelta
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from config import load_config
from tqsdk import TqApi,TqAuth,TqSim

def main():
    target=ROOT/'.tqsdk/research/forward15m';target.mkdir(exist_ok=True)
    c=load_config().tqsdk;api=TqApi(TqSim(),auth=TqAuth(c.account,c.password),debug=False,disable_print=True)
    try:
        products=['CZCE.OI','CZCE.AP','CZCE.SF','DCE.eb','DCE.p','DCE.pg','DCE.y','INE.lu'];catalog={}
        for p in products:
            exchange,product=p.split('.');catalog[p]=list(api.query_quotes(ins_class='FUTURE',exchange_id=exchange,product_id=product,expired=False))
        symbols=[s for ss in catalog.values() for s in ss];quotes={s:api.get_quote(s) for s in symbols}
        deadline=time.time()+30
        while time.time()<deadline and not all(q.datetime for q in quotes.values()):api.wait_update(deadline=time.time()+1)
        selected={p:max(ss,key=lambda s:float(quotes[s].open_interest) if quotes[s].open_interest==quotes[s].open_interest else -1) for p,ss in catalog.items() if ss}
        calendar=api.get_trading_calendar(date.today()-timedelta(days=7),date(date.today().year,12,31))
        payload=dict(catalog=catalog,selected_quote_oi_for_probe_only=selected,
            quote_times={s:quotes[s].datetime for s in selected.values()},
            calendar_columns=list(calendar.columns),calendar_rows=len(calendar))
        (target/'calendar-probe.json').write_text(calendar.to_json(orient='records',date_format='iso'),encoding='utf-8')
        (target/'connection-probe.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False),flush=True)
    finally:api.close()

if __name__=='__main__':main()

"""Independent sequential quote/PnL reconstruction for gated trade audit."""
import gzip
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import math
from tools.research_gated_15m import OUT,CACHE
from tools.research_rolling_15m import load,digest
from tools.research_15m_profit import save_json

def main():
    a=json.loads((OUT/'tick-audit.json').read_text(encoding='utf-8'))
    assert digest(ROOT/'tools/audit_gated_tick_fills.py')==a['code_sha256']
    for name,sha in a['tick_sha256'].items():assert digest(CACHE/'ticks'/name)==sha
    for arm,sha in a['ledger_sha256'].items():assert digest(OUT/f'{arm}-成交.csv')==sha
    x,m,_,_=load();names=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols']
    lookup={(int(row[0]),names[int(row[1])]):i for i,row in enumerate(m)};count=0
    for arm,s in a['results'].items():
        for r in s['records']:
            if not r['usable']:continue
            entry=r['entry_ms'];symbol=r['symbol'];side=r['direction'];lots=r['lots'];i=lookup[entry,symbol]
            tick=m[i,5];mult=m[i,4];distance=max(tick,math.ceil(1.5*x[i,7]*m[i,3]/tick)*tick)
            raw=json.loads(gzip.decompress((CACHE/'ticks'/f'{symbol}-{entry}.json.gz').read_bytes()))['rows']
            quotes=[q for q in raw if q[2]>0 and q[4]>=q[2] and q[3]>0 and q[5]>0]
            start=next(q for q in quotes if q[0]>=entry);timed=next(q for q in quotes if q[0]>=entry+900000)
            assert start[0]-entry<=5000 and timed[0]-entry-900000<=5000
            for v in r['variants']:
                extra=v['extra_ticks'];opened=(start[4] if side==1 else start[2])+side*extra*tick;stop=opened-side*distance
                end=timed
                for q in quotes:
                    if q[0]<start[0]:continue
                    if q[0]>timed[0]:break
                    executable=q[2] if side==1 else q[4]
                    if (side==1 and executable<=stop) or (side==-1 and executable>=stop):end=q;break
                close=(end[2] if side==1 else end[4])-side*extra*tick
                net=(side*(close-opened)*mult-10)*lots
                assert abs(net-v['net'])<1e-7 and end[0]==v['exit_ms']
                depth=min(start[5] if side==1 else start[3],end[3] if side==1 else end[5])
                assert bool(depth>=lots)==v['enough_displayed_depth']
            count+=1
        usable=[r for r in s['records'] if r['usable']]
        assert len(usable)==s['usable'] and len(s['records'])==s['total']
        assert sum(r['variants'][1]['net'] for r in usable)==s['quote_plus_tick_net']
    save_json(OUT/'盘口核验记录.json',dict(passed=True,checked_trade_records=count,unique_episodes=a['unique_episodes'],
        checks=['raw tick/code/ledger fingerprints','independent sequential entry/stop/exit/PnL and displayed-depth reconstruction'],
        limitation='Checks arithmetic and provenance only; not actual order fills or forward evidence.'))
    print('Independent L1 reconstruction passed',count,flush=True)

if __name__=='__main__':main()

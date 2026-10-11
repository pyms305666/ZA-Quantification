"""Feature parity and fault-case verification before collecting genuine forward evidence."""
import ast
import gzip
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
from research.forward_runtime import Journal,PaperBook,live_feature,ARMS
from tools.forward_research import OUT,trading_day

def main():
    z=np.load(ROOT/'.tqsdk/research/fine15m/model-data.npz');x=z['x'];m=z['m'];past=z['past'];p=z['p']
    names=json.loads((ROOT/'.tqsdk/research/external15m/metadata.json').read_text(encoding='utf-8'))['universe']['symbols'];cached={}
    for i in np.linspace(0,len(m)-1,32).astype(int):
        symbol=names[int(m[i,1])]
        if symbol not in cached:cached[symbol]=json.loads(gzip.decompress((ROOT/f'.tqsdk/research/history/{symbol}.json.gz').read_bytes()))
        raw=cached[symbol];bars=np.array(raw['bars']['900']);signal=int(m[i,0]);bars=bars[bars[:,0]+900000<=signal][-256:]
        mins=np.column_stack([signal+np.arange(-15,0)*60000,past[i]])
        result,_=live_feature(bars,mins,raw['meta'],signal,int(p[i]))
        np.testing.assert_allclose(result,x[i],rtol=1e-9,atol=1e-9)
        altered=np.vstack([bars,[signal,1,1,1,1,0,0]])
        later=np.vstack([mins,[signal,999999,999999,999999,999999,0,0]])
        np.testing.assert_array_equal(live_feature(altered,later,raw['meta'],signal,int(p[i]))[0],result)
        try:live_feature(bars,mins[:-1],raw['meta'],signal,int(p[i]))
        except ValueError:pass
        else:raise AssertionError('missing minute did not fail closed')
    # Timed synthetic events are isolated in a temporary DB, never counted as forward samples.
    with tempfile.TemporaryDirectory(prefix='forward-verify-') as tmp:
        j=Journal(Path(tmp)/'test.sqlite3');t=1800000000000;book=PaperBook(j,clock=lambda:t+1100)
        row=dict(symbol='TEST',spec=dict(tick=1.,multiple=10.,distance=10.,close=100.,cost_stop_ratio=.3),scores={a:[.1,None] for a in ARMS})
        assert book.prediction(t,[row],t+1000)
        assert not book.prediction(t,[row],t+1200)
        # Precommit quote cannot create a fill.
        book.tick('TEST',[t+1050,100,99,100,100,100,1,1,1],t+1200)
        assert all(s['position'] is None for s in book.state.values())
        book.tick('TEST',[t+1500,100,99,100,100,100,1,1,1],t+1600)
        assert all(s['position']['entry']==101 and s['position']['lots']==6 for s in book.state.values())
        # Quote at 15-minute due: this synthetic large gap is explicitly invalid.
        book.tick('TEST',[t+900000,107,107,100,108,100,1,1,1],t+900010)
        assert all(s['cash']==50240 and s['trades']==1 and s['invalid_trades']==1 for s in book.state.values())
        assert j.verify()>0;j.db.close()
        j=Journal(Path(tmp)/'depth.sqlite3');book=PaperBook(j,clock=lambda:t+1100)
        book.prediction(t,[row],t+1000);book.tick('TEST',[t+1500,100,99,1,100,1,1,1,1],t+1600)
        assert all(s['position'] is None and s['trades']==0 for s in book.state.values())
        assert not book.prediction(t+900000,[row],t+905000)
        j.verify();j.db.close()
        j=Journal(Path(tmp)/'partial.sqlite3');book=PaperBook(j,clock=lambda:t+1100)
        assert not book.prediction(t,[row],t+1000,allow_entries=False)
        assert all(s['pending'] is None for s in book.state.values());j.db.close()
    import pandas as pd
    cal={'2026-09-25':False,'2026-09-28':True,'2026-09-29':True}
    assert trading_day(int(pd.Timestamp('2026-09-27 10:00',tz='Asia/Shanghai').timestamp()*1000),cal) is None
    assert trading_day(int(pd.Timestamp('2026-09-28 21:00',tz='Asia/Shanghai').timestamp()*1000),cal)=='2026-09-29'
    forbidden={'insert_order','cancel_order','TargetPosTask','TqAccount','TqKq','TqCtp'}
    for path in [ROOT/'tools/forward_research.py',ROOT/'research/forward_runtime.py']:
        tree=ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.Call):
                name=node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id if isinstance(node.func,ast.Name) else ''
                assert name not in forbidden
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'上线前程序核验.json').write_text(json.dumps(dict(passed=True,historical_parity_samples=32,
        checks=['37 live features reproduce frozen historical inputs','future bars ignored and missing minutes rejected',
        'precommit/late/duplicate/partial forecasts cannot enter','entry depth, one-position budget and net PnL examples',
        'quote gaps invalidate evidence without erasing losses','SQLite journal chain and calendar mapping',
        'AST check: no brokerage order functions in collector or paper engine'],
        limitation='These checks do not constitute future data or prove model profitability.'),ensure_ascii=False,indent=2),encoding='utf-8')
    print('Forward feature/paper/fault tests passed; synthetic data excluded from real evidence',flush=True)

if __name__=='__main__':main()

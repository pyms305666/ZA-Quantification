"""One-time audited clock-skew amendment, permitted only before any prediction."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from research.forward_runtime import Journal
from tools.forward_research import atomic

ARCHIVE=ROOT/'docs/研究/2026-09-28-开盘观察/冻结源码v1'
EXPERIMENTS=[
    (ROOT/'.tqsdk/research/forward15m',ROOT/'docs/研究/2026-09-27-新行情前向验证',
     ['tools/forward_research.py','research/forward_runtime.py']),
    (ROOT/'.tqsdk/research/decision_forward',ROOT/'docs/研究/2026-09-27-决策准确度多品种',
     ['tools/forward_decision_accuracy.py','tools/forward_research.py','research/forward_runtime.py']),
]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    import msvcrt
    locks=[];checked=[]
    try:
        for base,out,changed in EXPERIMENTS:
            lock=(base/'service.lock').open('a+b');lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            locks.append(lock)
            manifest=base/'manifest.json';old=json.loads(manifest.read_text(encoding='utf-8'))
            if old.get('version',1)!=1:raise RuntimeError('Experiment version already amended')
            journal=Journal(base/'events.sqlite3');journal.verify()
            kinds=dict(journal.db.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
            if any(kinds.get(k,0) for k in ('prediction','prediction_committed','paper_entry','paper_exit','outcome')):
                raise RuntimeError('Forward evidence already exists; start a new experiment instead')
            for source,expected in old['source_sha256'].items():
                candidate=ARCHIVE/Path(source).name if source in changed else ROOT/source
                if sha(candidate)!=expected:raise RuntimeError('Original source hash differs: '+source)
            current=dict(old)
            current['version']=2
            current['amended_at_utc']=datetime.now(timezone.utc).isoformat()
            current['amendment']='Observed exchange timestamp ahead of local receipt by less than 1 second; allow bounded ±5s skew, using actual receipt timestamp for ticks. No predictions or outcomes existed before amendment.'
            current['source_sha256']={s:sha(ROOT/s) for s in old['source_sha256']}
            checked.append((base,out,manifest,old,current,journal,kinds))
        for base,out,manifest,old,current,journal,kinds in checked:
            oldhash=sha(manifest)
            atomic(base/'manifest-v1.json',old)
            atomic(out/'冻结方案-v1.json' if base.name=='forward15m' else out/'前向冻结方案-v1.json',old)
            atomic(manifest,current)
            atomic(out/'冻结方案.json' if base.name=='forward15m' else out/'前向冻结方案.json',current)
            journal.event('manifest_amendment',dict(previous_manifest_sha256=oldhash,new_manifest_sha256=sha(manifest),
                reason=current['amendment'],prior_event_counts=kinds))
            journal.db.close()
            print(base.name,'amended to version 2 before first prediction')
    finally:
        for lock in locks:
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()


if __name__=='__main__':main()

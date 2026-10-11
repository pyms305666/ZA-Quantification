"""Download pinned official source and public safetensors into ignored cache."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.tqsdk/research/external15m'
SOURCE='67b630e67f6a18c9e9be918d9b4337c960db1e9a'
MODELS={'Kronos-small':('NeoQuasar/Kronos-small','ac5c409a313c4eadd1ca78201322f5cadb9c34ab'),
        'Kronos-Tokenizer-base':('NeoQuasar/Kronos-Tokenizer-base','9ef143b98ee3c2488eebd85404e0c215c112b46a')}


def fetch(url,path):
    if not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix(path.suffix+'.part')
        with urllib.request.urlopen(url,timeout=90) as r,tmp.open('wb') as f:
            while data:=r.read(1024*1024):f.write(data)
        tmp.replace(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    print('downloaded',path.name,path.stat().st_size,flush=True)
    return {'path':str(path.relative_to(ROOT)),'sha256':digest,'url':url}


def main():
    CACHE.mkdir(parents=True,exist_ok=True)
    tasks=[]
    for name in ['model/__init__.py','model/kronos.py','model/module.py','LICENSE']:
        tasks.append((f'https://raw.githubusercontent.com/shiyu-coder/Kronos/{SOURCE}/{name}',CACHE/'source'/name))
    for name,(repo,revision) in MODELS.items():
        for filename in ['config.json','model.safetensors']:
            tasks.append((f'https://huggingface.co/{repo}/resolve/{revision}/{filename}',CACHE/name/filename))
    with ThreadPoolExecutor(max_workers=4) as pool:
        files=list(pool.map(lambda task:fetch(*task),tasks))
    (CACHE/'downloads.json').write_text(json.dumps({'source_commit':SOURCE,'models':MODELS,
        'weight_publication_date':'2025-06-30','files':files},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

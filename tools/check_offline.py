"""统一离线工程门禁：不打包、不启动实盘，失败立即返回。"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--git-only',action='store_true')
    parser.add_argument('--python38',type=Path)
    args=parser.parse_args()
    environment={key:value for key,value in os.environ.items() if not key.upper().startswith('TQ_')}
    environment.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    commands=[
        [sys.executable,'-B','tools/source_guard.py','--git-only' if args.git_only else '--require-main'],
        [sys.executable,'-B','tools/check_dependencies.py','--environment','test'],
        [sys.executable,'-B','tools/check_core_drift.py','--strict'],
        [sys.executable,'-B','tools/run_offline_tests.py','--quiet'],
        *[['node','electron/test/'+name+'.test.js'] for name in ['backend','startup','market-connection','ui-consistency','decision-ui']],
    ]
    if args.python38:
        commands.append([str(args.python38.resolve()),'-B','-X','utf8','tools/run_python38_compat.py'])
    for command in commands:
        print('CHECK '+ ' '.join(command),flush=True)
        result=subprocess.run(command,cwd=str(ROOT),env=environment)
        if result.returncode:
            return result.returncode
    diff=subprocess.run(['git','diff','--check'],cwd=str(ROOT))
    if diff.returncode:
        return diff.returncode
    print('PASS: offline gates; '+('Python 3.8 included' if args.python38 else 'Python 3.8 requires its separate CI job'))
    return 0


if __name__=='__main__':
    raise SystemExit(main())

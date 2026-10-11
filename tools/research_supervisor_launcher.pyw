"""Run the existing research supervisor without allocating a console window."""
import ctypes
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.tqsdk/research/decision_forward'
CN = timezone(timedelta(hours=8))


def save_status(value):
    path = DATA / 'launcher_status.json'
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = os.environ.copy()
    environment['FUTURES_RESEARCH_PYTHON'] = str(Path(sys.executable).with_name('python.exe'))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    with (DATA / 'launcher.log').open('ab', buffering=0) as log:
        try:
            child = subprocess.Popen(
                [str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                 '-File', str(ROOT / 'tools/supervise_decision_research.ps1')],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup,
            )
            value = dict(state='running', pid=os.getpid(), supervisor_pid=child.pid,
                         started_at=datetime.now(CN).isoformat(),
                         console_window=int(ctypes.windll.kernel32.GetConsoleWindow()),
                         child_create_no_window=True)
            save_status(value)
            result = child.wait()
            value.update(state='stopped', exit_code=result, stopped_at=datetime.now(CN).isoformat())
            save_status(value)
            return result
        except Exception as exc:
            value = dict(state='error', pid=os.getpid(), time=datetime.now(CN).isoformat(),
                         error_type=type(exc).__name__, error=str(exc))
            save_status(value)
            log.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
            return 1


if __name__ == '__main__':
    sys.exit(main())

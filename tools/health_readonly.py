"""独立只读旁路：只读状态与 PID 存活，不导入监督器或研究模块。"""
import argparse
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import time

STATUS_PATHS = ('.tqsdk/research/forward15m/status.json', '.tqsdk/research/decision_forward/status.json')


def pid_alive(pid):
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError as error:
        return False if error.errno == errno.ESRCH else None


def inspect_status(path, now=None, probe=pid_alive, stale_seconds=120):
    result = {'source': str(path), 'pid':None, 'pid_alive':None, 'heartbeat_age_seconds':None,
              'state':None, 'status':'unavailable', 'identity_verified':False}
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        pid, wall = raw['pid'], raw['wall_ms']
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 2**32:
            raise ValueError('invalid pid')
        if isinstance(wall, bool) or not isinstance(wall, (float,int)) or not math.isfinite(wall) or wall <= 0:
            raise ValueError('invalid heartbeat')
        age = (time.time() if now is None else now) - wall / 1000
        result.update(pid=pid, pid_alive=probe(pid), heartbeat_age_seconds=round(age, 1),
                      state=raw.get('state') if raw.get('state') in {'collecting','running','waiting','stopped','error','starting','stopping','completed','connecting','observing','waiting_for_fresh_market','connection_error'} else 'unknown',
                      status='clock_skew' if age < -5 else 'stale' if age > stale_seconds else 'recent')
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--stale-seconds', type=float, default=120)
    args = parser.parse_args()
    results = [inspect_status(args.root / rel, stale_seconds=args.stale_seconds) for rel in STATUS_PATHS]
    print(json.dumps({'readonly':True, 'collectors':results}, ensure_ascii=False, indent=2))
    return 0 if all(r['status'] == 'recent' and r['pid_alive'] is True for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())

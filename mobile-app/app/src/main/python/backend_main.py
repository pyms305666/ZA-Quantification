"""Android 入口：仅在模块导入时启动一次移动版后端（线程内），避免重复绑定 8000。"""
import os
import sys
import threading
from pathlib import Path

BASE = Path(__file__).parent
_started = threading.Event()


def files_dir() -> str:
    try:
        from java import jclass
        return jclass("java.lang.System").getProperty("za.filesdir") or "."
    except Exception:
        return "."


def _run_server():
    data = Path(files_dir())
    data.mkdir(parents=True, exist_ok=True)
    os.chdir(data)
    sys.path.insert(0, str(BASE))
    sys.path.insert(0, str(BASE / "static"))
    import mobile_api
    try:
        mobile_api.run_mobile_server("127.0.0.1", 8000, static_dir=BASE / "static")
    except Exception as exc:
        print("backend start error:", exc)


def start() -> None:
    if _started.is_set():
        return
    _started.set()
    threading.Thread(target=_run_server, daemon=True).start()


threading.Thread(target=start, daemon=True).start()

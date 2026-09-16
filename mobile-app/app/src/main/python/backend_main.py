"""Android 入口：仅在模块导入时启动一次移动版后端（线程内），避免重复绑定 8000。"""
import os
import sys
import threading
from pathlib import Path

BASE = Path(__file__).parent
_started = threading.Event()


def files_dir() -> str:
    """App 私有可写目录（凭据/临时文件应写入这里，绝不能是只读的 assets 目录）。

    MainActivity 在启动后端前已通过 System.setProperty("za.filesdir", ...) 写入，
    这里直接读取即可；读不到则通过环境变量 TQ_MOBILE_FILES_DIR 兜底。
    """
    try:
        from java import jclass
        value = jclass("java.lang.System").getProperty("za.filesdir")
    except Exception:
        value = None
    value = value or os.environ.get("TQ_MOBILE_FILES_DIR")
    return value or "."


def _run_server():
    data = Path(files_dir())
    data.mkdir(parents=True, exist_ok=True)
    os.chdir(data)
    # 让 config.py 的凭据/配置路径稳定指向 App 私有目录（Android 上 os.chdir 可能指向只读 assets）
    os.environ.setdefault("TQ_MOBILE_FILES_DIR", str(data))
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

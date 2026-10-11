"""Android 后端入口：由前台服务显式启动和停止受管的服务器线程。"""
import os
import sys
import threading
from pathlib import Path

BASE = Path(__file__).parent
_state_lock = threading.RLock()
_stop_requested = threading.Event()
_thread = None
_server = None


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


def _register_server(server) -> None:
    """与停止请求串行登记；停止早于 Server 创建时也不能丢失。"""
    global _server
    with _state_lock:
        _server = server
        if _stop_requested.is_set():
            server.should_exit = True


def _run_server():
    """后端线程主函数：切到私有目录、设环境变量、补模块路径、阻塞跑 uvicorn。

    任何异常都只打印不重抛（线程内崩溃只影响本线程，Java 侧通知仍存活）。
    """
    global _server
    try:
        if _stop_requested.is_set():
            return
        data = Path(files_dir())
        data.mkdir(parents=True, exist_ok=True)
        os.chdir(data)
        # config.py 的凭据/配置路径稳定指向 App 私有目录。
        os.environ.setdefault("TQ_MOBILE_FILES_DIR", str(data))
        for module_dir in (str(BASE), str(BASE / "static")):
            if module_dir not in sys.path:
                sys.path.insert(0, module_dir)
        import mobile_api
        mobile_api.run_mobile_server(
            "127.0.0.1", 8000, static_dir=BASE / "static",
            on_server=_register_server, should_exit=_stop_requested.is_set)
    except Exception as exc:
        print("backend start error:", exc)
    finally:
        with _state_lock:
            _server = None


def start() -> None:
    """供 BackendService.onCreate 调用；旧线程退出前不会启动第二个服务器。"""
    global _thread, _server
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_requested.clear()
        _server = None
        _thread = threading.Thread(target=_run_server, name="mobile-backend", daemon=True)
        try:
            _thread.start()
        except Exception:
            _thread = None
            raise


def request_stop() -> None:
    """非阻塞停止请求；Android 主线程不等待 uvicorn 或行情线程退出。"""
    with _state_lock:
        _stop_requested.set()
        if _server is not None:
            _server.should_exit = True


def stop(timeout: float = 3.0) -> bool:
    """等待受管线程退出；超时保留句柄，后续 start 仍不能重复启动。"""
    global _thread, _server
    request_stop()
    with _state_lock:
        thread = _thread
    if thread is None:
        return True
    if thread is threading.current_thread():
        return False
    thread.join(timeout=max(0.0, timeout))
    if thread.is_alive():
        return False
    with _state_lock:
        if _thread is thread:
            _thread = None
            _server = None
    return True

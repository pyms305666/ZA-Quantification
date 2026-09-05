"""ZA量化 桌面启动器（打包入口）。

启动流程：
1. 首次运行时若当前目录没有 config.json，自动生成模板并提示填写天勤账号；
2. 启动行情网关服务（http://127.0.0.1:8000）；
3. 自动打开默认浏览器访问界面。

路线保护：两条路线（AB协程版 / C直连版）共用 8000 端口，不能同时运行。
启动前会探测端口：已有"本版本"在跑 → 直接打开页面复用；
被"另一条路线"占用 → 明确提示并退出，绝不打开误导性的页面。

用法：
    python launcher.py        # 开发调试
    打包后运行 ZA量化.exe      # 生产运行
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import requests

import uvicorn

from api.http import create_app
from config import load_config


def resource_path(name: str) -> Path:
    """PyInstaller 打包后资源在 sys._MEIPASS 内，开发时在项目根目录。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def ensure_config() -> None:
    """首次运行生成 config.json 模板（复制打包内置的 example）。"""
    target = Path("config.json")
    if target.exists():
        return
    example = resource_path("config.json.example")
    if not example.exists():
        print("[提示] 未找到配置模板，请手动创建 config.json（参考 config.json.example）。")
        return
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print("=" * 56)
    print("  首次运行：已生成 config.json 配置文件。")
    print("  请编辑 config.json，填入天勤账号（手机号）与密码后重新启动。")
    print("=" * 56)


MY_ROUTE = "C 直连版"   # 本构建的路线标识（与 /api/v1/status 的 route 字段一致）


def _route_on_port(port: int):
    """探测端口上是否已有 ZA量化 在跑：返回路线名；没有服务返回 None"""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/api/v1/status", timeout=2)
        return r.json().get("route")
    except Exception:
        return None


def main() -> None:
    ensure_config()
    config = load_config()
    host, port = config.server.host, config.server.port
    url = f"http://{host}:{port}"

    existing = _route_on_port(port)
    if existing is not None:
        if existing == MY_ROUTE:
            print(f"检测到 {MY_ROUTE} 已在运行，直接打开页面（不重复启动）。")
            webbrowser.open(url)
            return
        print("=" * 60)
        print(f"⚠  8000 端口已被另一条路线占用：{existing}")
        print(f"    本次要启动的是：{MY_ROUTE}")
        print("    两条路线共用 8000 端口，不能同时运行。请任选其一：")
        print("    ① 任务管理器里结束另一个 ZA量化 进程后，再双击本程序")
        print(f"    ② 修改 config.json 的 server.port，给 {MY_ROUTE} 换个端口")
        print("=" * 60)
        webbrowser.open("data:text/html;charset=utf-8," + quote(
            f"<h2>端口被占用</h2><p>8000 端口已被 <b>{existing}</b> 占用，"
            f"本次要启动的是 <b>{MY_ROUTE}</b>。</p>"
            "<p>请在任务管理器里结束另一个 ZA量化 程序后重试，"
            "或修改 config.json 的 server.port 换端口。</p>"))
        return

    def open_browser() -> None:
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"ZA量化 v{version()} 启动中：{url} （关闭本窗口即退出）")
    app = create_app(config)
    uvicorn.run(app, host=host, port=port, log_level=config.server.log_level)


def version() -> str:
    try:
        return Path("VERSION").read_text(encoding="utf-8").strip() or "1.1.1"
    except OSError:
        return "1.1.1"


if __name__ == "__main__":
    main()

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
import os
from html import escape
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

import requests

import uvicorn

from api.http import create_app
from config import load_config
from app_runtime import config_path, configure_logging, public_url, resource_path, version


def ensure_config() -> None:
    """首次运行生成 config.json 模板（复制打包内置的 example）。"""
    target = config_path()
    if target.exists():
        return
    example = resource_path("config.json.example")
    if not example.exists():
        print("[提示] 未找到配置模板，请手动创建 config.json（参考 config.json.example）。")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print("=" * 56)
    print("  首次运行：已生成 config.json 配置文件。")
    print("  请编辑 config.json，填入天勤账号（手机号）与密码后重新启动。")
    print("=" * 56)


MY_ROUTE = "C 直连版"   # 本构建的路线标识（与 /api/v1/status 的 route 字段一致）


def _route_on_port(port: int, host: str = "127.0.0.1"):
    """探测端口上是否已有 ZA量化 在跑：返回路线名；没有服务返回 None"""
    try:
        r = requests.get(public_url(host, port) + "/api/v1/status", timeout=1)
        return r.json().get("route")
    except Exception:
        return None


def main() -> None:
    ensure_config()
    config = load_config()
    configure_logging(config)
    host, port = config.server.host, config.server.port
    url = public_url(host, port)

    def show_page(address):
        if os.environ.get("TQ_GATEWAY_NO_BROWSER") != "1":
            webbrowser.open(address)

    existing = _route_on_port(port, host)
    if existing is not None:
        if existing == MY_ROUTE:
            print(f"检测到 {MY_ROUTE} 已在运行，直接打开页面（不重复启动）。")
            show_page(url)
            return
        print("=" * 60)
        print(f"⚠  {url} 已被另一条路线占用：{existing}")
        print(f"    本次要启动的是：{MY_ROUTE}")
        print("    两条路线共用 8000 端口，不能同时运行。请任选其一：")
        print("    ① 任务管理器里结束另一个 ZA量化 进程后，再双击本程序")
        print(f"    ② 修改 config.json 的 server.port，给 {MY_ROUTE} 换个端口")
        print("=" * 60)
        show_page("data:text/html;charset=utf-8," + quote(
            f"<h2>端口被占用</h2><p>{escape(url)} 已被 <b>{escape(str(existing))}</b> 占用，"
            f"本次要启动的是 <b>{MY_ROUTE}</b>。</p>"
            "<p>请在任务管理器里结束另一个 ZA量化 程序后重试，"
            "或修改 config.json 的 server.port 换端口。</p>"))
        return

    def open_browser() -> None:
        time.sleep(1.2)
        try:
            show_page(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"ZA量化 v{version()} 启动中：{url} （关闭本窗口即退出）")
    # 打包模式（exe）启用保险丝：无浏览器连接 10 分钟自动退出，释放端口与内存
    auto_exit = 600 if getattr(sys, "frozen", False) else None
    app = create_app(config, auto_exit_idle_seconds=auto_exit)
    uvicorn.run(app, host=host, port=port, log_level=config.server.log_level, log_config=None)


if __name__ == "__main__":
    main()

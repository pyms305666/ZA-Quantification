"""ZA量化 桌面启动器（打包入口）。

启动流程：
1. 首次运行时若当前目录没有 config.json，自动生成模板并提示填写天勤账号；
2. 启动行情网关服务（http://127.0.0.1:8000）；
3. 自动打开默认浏览器访问界面。

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


def main() -> None:
    ensure_config()
    config = load_config()
    host, port = config.server.host, config.server.port
    url = f"http://{host}:{port}"

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

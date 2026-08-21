"""国内期货行情网关入口。

用法：
    python main.py

启动后：
    http://127.0.0.1:8000           接口索引
    ws://127.0.0.1:8000/ws/market  实时行情推送
"""

from __future__ import annotations

import faulthandler
import sys

import uvicorn

from api.http import create_app
from config import load_config


def main() -> None:
    # 诊断：每 30 秒转储所有线程栈到 stderr（python main.py --debug-stack）
    if "--debug-stack" in sys.argv:
        faulthandler.dump_traceback_later(30, repeat=True, file=sys.stderr)
    config = load_config()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )


if __name__ == "__main__":
    main()

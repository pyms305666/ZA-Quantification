"""显式执行真实合约搜索探针，不读取账号、不登录、不订阅行情、不下载完整目录。

示例：python tools/probe_contract_search.py --live --output build/contract-search-live.json
通过公开名称服务获取行情端点，在独立短连接内调用正式搜索实现，并保存有限元数据。
与单元测试分开运行；网络不可用时应报告失败，不能伪造挂牌结果。
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests
import websockets

from tqdiff.auth import NS_URL
from tqdiff.client import DiffClient, TqClientError


def public_md_url() -> str:
    """公开元数据查询不携带认证头，不访问本机配置或凭据。"""
    response = requests.get(NS_URL, params={"stock": "true", "backtest": "false"}, timeout=12)
    response.raise_for_status()
    endpoint = response.json().get("mdurl")
    if not isinstance(endpoint, str) or not endpoint.startswith("wss://"):
        raise RuntimeError("公开名称服务未返回合法行情端点")
    return endpoint


async def probe(keywords: list[str], endpoint: str | None = None) -> dict:
    """用真实收包泵验证 DiffClient→SymbolSearch→ins_query 完整链路。"""
    loop = asyncio.get_running_loop()
    endpoint_source = "explicit" if endpoint else "public_name_service"
    endpoint = endpoint or await loop.run_in_executor(None, public_md_url)
    if not endpoint.startswith("wss://"):
        raise ValueError("探针只接受 wss:// 端点")
    parsed = urlsplit(endpoint)
    report = {"at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
              "endpoint_source": endpoint_source,
              "authenticated": False, "queries": []}
    client = DiffClient("", "")
    client._loop = loop
    client._send_lock = asyncio.Lock()
    async with websockets.connect(endpoint, open_timeout=12) as ws:
        client._ws = ws
        client._set_connected(True)

        async def receive() -> None:
            async for raw in ws:
                message = json.loads(raw)
                for diff in message.get("data") or []:
                    if isinstance(diff, dict):
                        client._apply_diff(diff)
                await client._send({"aid": "peek_message"})

        receiver = asyncio.create_task(receive())
        try:
            await client._send({"aid": "peek_message"})
            for keyword in keywords:
                started = time.perf_counter()
                rows = await client._execute("search_instruments", ("", keyword, False, 200))
                result = {"keyword": keyword, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                          "count": len(rows), "items": rows}
                report["queries"].append(result)
                print(json.dumps({"keyword": keyword, "elapsed_ms": result["elapsed_ms"],
                                  "count": len(rows), "symbols": [row["symbol"] for row in rows]},
                                 ensure_ascii=True), flush=True)
            # 探针绝不登记行情订阅，不能让本机测量成为新的长期行情连接。
            assert not client._subscribed
            report["market_subscriptions"] = []
            report["pending_queries"] = len(client._symbol_queries)
        finally:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver
            client._set_connected(False)
            client._ws = None
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True,
                        help="明确执行只读网络查询；不使用任何本机账户")
    parser.add_argument("--keyword", action="append", dest="keywords")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--endpoint", help="使用已经核实的公开 wss 端点，隔离名称服务网络问题")
    args = parser.parse_args()
    try:
        report = asyncio.run(probe(args.keywords or ["烧碱", "纯碱", "棕榈油2701", "p2701", "SA2701"],
                                   args.endpoint))
    except (requests.RequestException, TqClientError, OSError, asyncio.TimeoutError,
            websockets.exceptions.WebSocketException) as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__,
                          "error": str(error) or repr(error)}, ensure_ascii=True), file=sys.stderr)
        return 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

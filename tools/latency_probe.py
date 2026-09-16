"""端到端延迟采样工具（对应 docs/待办与验收计划 P0"实时更新与 200ms 目标"）。

在**交易时段**、后端已启动的前提下运行（休市时段无行情推送，采不到样本）：

  python tools/latency_probe.py --seconds 600 --symbols SHFE.rb2610 SHFE.au2612
  python tools/latency_probe.py --seconds 600 --symbols SHFE.rb2610 --out latency.json

测量口径：服务端 WebSocket 发送时刻（消息里的 ts，见 api/websocket.py）→ 本工具收到。
输出：count/avg/p50/p95/max/min（毫秒）+ 每分钟均值序列；--out 保存原始样本与统计。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("缺少 websockets 依赖：pip install websockets")


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(q * len(sorted_values)) - 1))
    return sorted_values[idx]


async def run(url: str, symbols: list[str], seconds: float) -> list[float]:
    samples: list[float] = []
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"action": "subscribe", "symbols": symbols}))
        print(f"已连接 {url}，订阅 {symbols}，采样 {seconds:.0f} 秒 …")
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") in ("quote", "quote_snapshot") and msg.get("ts"):
                lat_ms = max(0.0, time.time() - float(msg["ts"])) * 1000.0
                samples.append(lat_ms)
                if len(samples) % 50 == 0:
                    print(f"  已采样 {len(samples)} 笔，当前均值 {statistics.mean(samples):.0f}ms")
    return samples


def report(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "avg_ms": round(statistics.mean(ordered), 1),
        "p50_ms": round(percentile(ordered, 0.50), 1),
        "p95_ms": round(percentile(ordered, 0.95), 1),
        "max_ms": round(ordered[-1], 1),
        "min_ms": round(ordered[0], 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/market")
    parser.add_argument("--symbols", nargs="+", default=["SHFE.rb2610"])
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--out", default="", help="把统计与原始样本写入该 JSON 文件")
    args = parser.parse_args()

    samples = asyncio.run(run(args.url, args.symbols, args.seconds))
    stats = report(samples)
    print("\n===== 端到端延迟统计（毫秒） =====")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.out and samples:
        Path(args.out).write_text(
            json.dumps({"stats": stats, "samples_ms": samples}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"原始样本已写入 {args.out}")
    if stats.get("count", 0) == 0:
        print("提示：未采到样本——请确认处于交易时段且后端已连接天勤。")


if __name__ == "__main__":
    main()

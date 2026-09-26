"""合约目录精简索引：把 268MB 原始 JSON 逐条解析为轻量 record，避免手机端 OOM。

背景：合约文件 latest.json（线上约 334MB）是顶层大字典 {symbol: entry}。
旧代码用 json.loads() 一次性读入并整体持有，手机端内存爆掉导致"下载完成即被杀、又从 0 重下"。

本模块：
- 用 ijson 流式逐条解析（不把全量 JSON 塞进内存）；
- 每条 entry 立即精简成 Instrument record（只保留查询/展示需要的字段）写入 dict；
- 支持"边解析、边计数、边持久化"——解析到一定条数即可让目录部分可用；
- 落盘用 pickle（存精简 record dict），二次打开秒级加载，无需再碰 268MB 原始文件。

注意：iqson 在 Chatquopy（Python 3.8）用 3.2.3（无 requires_python 限制），纯 Python 后端。
"""

from __future__ import annotations

import gzip
import io
import json
import os
import pickle
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import ijson


# 内存里每条 record 的 key；精简后每条约 100~200 字节。
RECORD_FIELDS = ("exchange", "instrument_id", "name", "kind", "expired",
                 "price_tick", "volume_multiple", "expire_rest_days")


def parse_record(symbol: str, entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """静态合约文件条目 → 轻量 record（与旧 parse_instrument_record 输出同构，但可空/可缺）。

    与旧逻辑一致：exchange 取大写，instrument_id 去交易所前缀，仅保留查询/展示所需字段。
    """
    if not isinstance(entry, dict) or not entry:
        return None
    instrument_id = str(entry.get("instrument_id") or "")
    exchange = str(entry.get("exchange_id") or "").upper()
    if not instrument_id or not exchange:
        return None
    if instrument_id.upper().startswith(exchange + "."):
        instrument_id = instrument_id[len(exchange) + 1:]
    expire_datetime = entry.get("expire_datetime")
    expire_rest_days: Optional[int] = None
    if expire_datetime:
        try:
            expire_rest_days = max(0, int((float(expire_datetime) - time.time()) // 86400))
        except (TypeError, ValueError):
            expire_rest_days = None
    price_tick = entry.get("price_tick")
    volume_multiple = entry.get("volume_multiple")
    return {
        "symbol": symbol,
        "exchange": exchange,
        "instrument_id": instrument_id,
        "name": str(entry.get("ins_name") or entry.get("instrument_name") or ""),
        "kind": str(entry.get("class") or "FUTURE").upper(),
        "expired": bool(entry.get("expired") or False),
        "price_tick": float(price_tick) if price_tick is not None else None,
        "volume_multiple": int(volume_multiple) if volume_multiple is not None else None,
        "expire_rest_days": expire_rest_days,
        "expire_datetime": float(expire_datetime) if expire_rest_days is not None else None,
    }


def is_useful_future(symbol: str, record: dict[str, Any]) -> bool:
    """是否国内期货主连/主力（用于 query_instruments 的过滤）。

    排除：外盘主连（KQD.）、指数/期权/非 FUTURE、已过期。
    """
    if symbol.upper().startswith("KQD."):
        return False
    if record.get("kind") != "FUTURE":
        return False
    if record.get("expired"):
        return False
    return True


def iter_records_from_file(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """用 ijson 流式解析明文 latest.json，逐条 yield (symbol, 精简 record)。

    逐条解析，内存恒定；不把整份 JSON 读过内存（断点续传路径的产物就是明文
    文件，走这里解析）。无效条目（parse_record 返回 None）被静默跳过。

    Args:
        path: 明文 JSON 文件路径（约 354MB，ijson 逐 token 读取不会整体载入）。

    Yields:
        (合约完整代码, 精简 record) 二元组，顺序即文件顺序。
    """
    with path.open("rb") as handle:
        # kvitems 流式迭代顶层键值对
        for symbol, entry in ijson.kvitems(handle, ""):
            record = parse_record(symbol, entry)
            if record is not None:
                yield symbol, record


def index_from_file(path: Path,
                    progress: Optional[Callable[[int], None]] = None) -> dict[str, dict[str, Any]]:
    """把 latest.json 流式解析为精简 record dict。

    progress(count) 每解析 N 条后回调，便于上层更新进度/部分就绪。
    """
    result: dict[str, dict[str, Any]] = {}
    count = 0
    for symbol, record in iter_records_from_file(path):
        # 只保留国内相关合约，省内存：KQD 外盘、非 FUTURE、过期 跳过（但保留全部以减少二次解析？）
        # 保留全部（含外盘/期权），因为 _file_entry 也要查任意代码；过滤在 _query_instruments 做。
        result[symbol] = record
        count += 1
        if progress is not None and count % 2000 == 0:
            progress(count)
    if progress is not None:
        progress(count)
    return result


def iter_records_from_gzip(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """用 ijson 从 gzip 压缩的 latest.json 流式解析，逐条 yield (symbol, 精简 record)。

    用 gzip.open 边解压边喂给 kvitems，不把解压后的 334MB 明文档读进内存/磁盘
    （gzip 快路径的首选解析入口；手机端闪存与内存都按最低峰值设计）。
    """
    with gzip.open(path, "rb") as handle:
        for symbol, entry in ijson.kvitems(handle, ""):
            record = parse_record(symbol, entry)
            if record is not None:
                yield symbol, record


def index_from_gzip_file(path: Path,
                         progress: Optional[Callable[[int], None]] = None) -> dict[str, dict[str, Any]]:
    """把 gzip 压缩的 latest.json 流式解析为精简 record dict（边解压边逐条）。

    progress(count) 每解析 N 条后回调，便于上层更新进度/部分就绪。
    """
    result: dict[str, dict[str, Any]] = {}
    count = 0
    for symbol, record in iter_records_from_gzip(path):
        result[symbol] = record
        count += 1
        if progress is not None and count % 2000 == 0:
            progress(count)
    if progress is not None:
        progress(count)
    return result

def save_index(index: dict[str, dict[str, Any]], path: Path, downloaded_at: Optional[float] = None) -> None:
    """把精简 record dict 用 pickle 落盘（含下载时间戳），二次打开秒级加载。

    pickle 体积约 25MB（24 万条），反序列化远快于重新解析 354MB JSON；
    时间戳供 load_index 做 TTL 判定。

    Args:
        index: 精简 record 字典（symbol → record）。
        path: 目标 pickle 路径（父目录不存在会自动创建）。
        downloaded_at: 下载完成时间戳；缺省取当前时间。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_downloaded_at": downloaded_at if downloaded_at is not None else time.time(),
               "index": index}
    with path.open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)


def load_index(path: Path, max_age: float) -> Optional[dict[str, dict[str, Any]]]:
    """读取 pickle 索引，做过期判定；任何异常/过期/格式不符都返回 None（安全降级）。

    Args:
        path: pickle 索引路径。
        max_age: 有效期（秒），与文件内 _downloaded_at 比较判定过期。

    Returns:
        未过期 → 精简 record 字典；文件不存在/损坏/过期/非本格式 → None
        （调用方据此走"内置表兜底 + 后台重下载"）。
    """
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
        if not isinstance(payload, dict):
            return None
        if "_index" not in payload and "index" not in payload:
            return None  # 非本格式
        if time.time() - payload.get("_downloaded_at", 0) >= max_age:
            return None
        index = payload.get("index") or {}
        return index if isinstance(index, dict) else None
    except (OSError, ValueError, pickle.PickleError, EOFError, AttributeError, TypeError):
        return None

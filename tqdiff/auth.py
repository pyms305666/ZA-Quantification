"""DIFF 路线认证与静态数据获取。

流程（与 TqSdk 同源，均已按其源码核实）：
1. OAuth 密码模式登录 auth.shinnytech.com 换 access_token（JWT，内含行情权限）；
2. 名称服务 api.shinnytech.com/ns 用 token 换行情 WebSocket 地址（mdurl）；
3. 合约目录从 openmd 静态合约服务 latest.json 一次性拉取（磁盘缓存 24 小时）。

本模块全部为阻塞 HTTP（在 DIFF 客户端的启动阶段调用，不进入事件循环热路径）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

AUTH_BASE = os.getenv("TQ_AUTH_URL", "https://auth.shinnytech.com")
NS_URL = "https://api.shinnytech.com/ns"
SYMBOL_FILE_URL = os.getenv("TQ_INS_URL", "https://openmd.shinnytech.com/t/md/symbols/latest.json")
OAUTH_CLIENT = {"client_id": "shinny_tq", "client_secret": "be30b9f4-6862-488a-99ad-21bde0400081"}
SYMBOL_CACHE_TTL = 24 * 3600.0  # 合约文件磁盘缓存有效期（秒）


class DiffAuthError(RuntimeError):
    """认证 / 名称服务 / 合约文件获取失败。"""


def _headers(access_token: str) -> dict:
    # HTTP 头只能 latin-1：UA 必须纯 ASCII
    return {
        "User-Agent": "ZAQuant-diff/1.0",
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }


def login(account: str, password: str) -> dict:
    """OAuth 密码模式登录，返回 {"access_token", "refresh_token"}。"""
    if not account or not password:
        raise DiffAuthError("未配置天勤账号/密码（config.json 或环境变量 TQ_ACCOUNT/TQ_PASSWORD）")
    data = {
        "grant_type": "password",
        "username": account,
        "password": password,
        **OAUTH_CLIENT,
    }
    url = f"{AUTH_BASE}/auth/realms/shinnytech/protocol/openid-connect/token"
    try:
        response = requests.post(url, data=data, timeout=30)
    except requests.RequestException as error:
        raise DiffAuthError(f"认证服务连接失败：{error}") from error
    if response.status_code != 200:
        raise DiffAuthError(f"天勤登录失败（HTTP {response.status_code}），请检查账号密码")
    content = json.loads(response.content)
    return {"access_token": content["access_token"], "refresh_token": content["refresh_token"]}


def get_md_url(access_token: str) -> str:
    """名称服务：用 access_token 换行情 WebSocket 地址。

    实测（对照 TqSdk 抓包）：stock=true 返回的 nfmd 前置带 K 线历史；
    stock=false 返回的新前置只有实时快照，没有 K 线。
    """
    try:
        response = requests.get(
            NS_URL,
            params={"stock": "true", "backtest": "false"},
            headers=_headers(access_token),
            timeout=30,
        )
    except requests.RequestException as error:
        raise DiffAuthError(f"名称服务连接失败：{error}") from error
    if response.status_code != 200:
        raise DiffAuthError(f"名称服务失败（HTTP {response.status_code}）")
    content = json.loads(response.content)
    md_url = content.get("mdurl")
    if not md_url:
        raise DiffAuthError(f"名称服务未返回行情地址：{content}")
    return md_url


def _cache_path() -> Path:
    return Path(os.getenv("TQ_GATEWAY_CACHE", ".tqsdk")) / "symbol_file.json"


def load_cached_symbol_file(max_age: float = SYMBOL_CACHE_TTL) -> Optional[dict]:
    """读取仍在有效期内的磁盘缓存；无有效缓存返回 None。"""
    cache = _cache_path()
    if not cache.exists():
        return None
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - payload.get("_downloaded_at", 0) < max_age:
            payload.pop("_downloaded_at", None)
            return payload
    except (OSError, ValueError):
        pass
    return None


def download_symbol_file(access_token: str,
                         progress: Optional[Callable[[int, int], None]] = None) -> dict[str, Any]:
    """流式拉取全量合约目录（约 10~20MB），先写临时文件再原子落盘。

    progress(received_bytes, total_bytes) 在每个数据块后回调（可为 None）。
    """
    cache = _cache_path()
    try:
        response = requests.get(SYMBOL_FILE_URL, headers=_headers(access_token),
                                timeout=(15, 60), stream=True)
    except requests.RequestException as error:
        raise DiffAuthError(f"合约服务下载失败：{error}") from error
    if response.status_code != 200:
        raise DiffAuthError(f"合约服务下载失败（HTTP {response.status_code}）")
    total = int(response.headers.get("content-length") or 0)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    received = 0
    try:
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
        symbols = json.loads(tmp.read_text(encoding="utf-8"))
        payload = {"_downloaded_at": time.time(), **symbols}
        cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError) as error:
        raise DiffAuthError(f"合约文件解析失败：{error}") from error
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return symbols


def parse_instrument_record(symbol: str, entry: dict[str, Any]) -> Optional[dict]:
    """静态合约文件条目 → 与 TqClient.get_instrument 相同结构的记录。"""
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
        "symbol": f"{exchange}.{instrument_id}",
        "exchange": exchange,
        "instrument_id": instrument_id,
        "name": str(entry.get("ins_name") or entry.get("instrument_name") or ""),
        "kind": str(entry.get("class") or "FUTURE").upper(),
        "expired": bool(entry.get("expired") or False),
        "price_tick": float(price_tick) if price_tick is not None else None,
        "volume_multiple": int(volume_multiple) if volume_multiple is not None else None,
        "expire_rest_days": expire_rest_days,
    }


def parse_kline_row(row: Any) -> Optional[dict]:
    """DIFF K 线行 → 标准 K 线 dict。

    实测（TqSdk 抓包）行是字典：
    {"datetime": ns, "open":.., "high":.., "low":.., "close":..,
     "volume":.., "open_oi":.., "close_oi":..}
    同时兼容数组形式 [datetime_ns, open, high, low, close, volume, open_oi, close_oi]。
    """
    if isinstance(row, dict):
        values = [row.get(k) for k in
                  ("datetime", "open", "high", "low", "close", "volume", "open_oi", "close_oi")]
    elif isinstance(row, (list, tuple)) and len(row) >= 7:
        values = list(row[:8])
        while len(values) < 8:
            values.append(None)
    else:
        return None
    try:
        datetime_ms = int(values[0]) // 1_000_000
        open_, high, low, close = (float(values[1]), float(values[2]), float(values[3]), float(values[4]))
        volume = float(values[5]) if values[5] is not None else 0.0
        close_oi = float(values[7]) if values[7] is not None else None
    except (TypeError, ValueError):
        return None
    if close != close or open_ != open_:  # NaN
        return None
    return {
        "datetime": datetime_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume if volume == volume else 0.0,
        "open_interest": close_oi,
    }

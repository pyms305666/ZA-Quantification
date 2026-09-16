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
import pickle
import time
from pathlib import Path
from typing import Any

import requests

AUTH_BASE = os.getenv("TQ_AUTH_URL", "https://auth.shinnytech.com")
NS_URL = "https://api.shinnytech.com/ns"
SYMBOL_FILE_URL = os.getenv("TQ_INS_URL", "https://openmd.shinnytech.com/t/md/symbols/latest.json")
OAUTH_CLIENT = {"client_id": "shinny_tq", "client_secret": "be30b9f4-6862-488a-99ad-21bde0400081"}
SYMBOL_CACHE_TTL = 7 * 24 * 3600.0  # 合约文件磁盘缓存有效期（秒）：7 天，避免每日重下 256MB；底层合约数据变更频率远低于此


class DiffAuthError(RuntimeError):
    """认证 / 名称服务 / 合约文件获取失败。"""


def _headers(access_token: str) -> dict:
    # HTTP 头只能 latin-1：UA 必须纯 ASCII
    return {
        "User-Agent": "ZAQuant-diff/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",   # 显式请求 gzip：334MB JSON 压缩后约 8~11MB，大幅降下载量
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
    """合约文件磁盘缓存路径（symbol_file.json）。

    Android（Chaquopy）环境必须用 App 私有目录的绝对路径，否则相对路径会落到
    只读的 assets 目录，导致缓存写不进去、每次启动都重新下载 ~20MB 合约文件。
    TQ_GATEWAY_CACHE 覆盖优先；移动端回退到 TQ_MOBILE_FILES_DIR / za.filesdir。
    """
    override = os.getenv("TQ_GATEWAY_CACHE")
    if override:
        return Path(override) / "symbol_file.json"
    mobile_dir = _mobile_files_dir()
    if mobile_dir is not None:
        return mobile_dir / ".tqsdk" / "symbol_file.json"
    return Path(".tqsdk") / "symbol_file.json"


def _mobile_files_dir() -> Path | None:
    """Android（Chaquopy）私有可写目录；非移动端返回 None。"""
    try:
        from java import jclass  # type: ignore[import-not-found]  # 仅 Chaquopy 存在
        value = jclass("java.lang.System").getProperty("za.filesdir")
        if value:
            return Path(value)
    except Exception:
        pass
    env = os.getenv("TQ_MOBILE_FILES_DIR")
    if env:
        return Path(env)
    return None


def load_builtin_catalog() -> dict[str, Any]:
    """内置兜底合约目录：服务器下载慢/失败时先用，搜索/自选立即可用。

    返回 dict[symbol -> record]，与 load_cached_symbol_file 的索引结构一致。
    内置表只覆盖国内主流品种的近期合约（约 260 个）；完整目录下载成功后会自动替换。
    """
    try:
        from . import builtin_symbols
        index = getattr(builtin_symbols, "BUILTIN_SYMBOLS", None)
        if isinstance(index, dict):
            return dict(index)
    except Exception:
        pass
    return {}


def load_cached_symbol_file(max_age: float = SYMBOL_CACHE_TTL) -> Optional[dict]:
    """读取仍在有效期内的磁盘索引（精简 record dict）；无有效缓存返回 None。

    索引以 pickle 存储（见 symbol_index.save_index），反序列化快、内存友好，手机端秒级加载。
    兼容旧版本落盘的整段 JSON（symbol_file.json）：读不到索引时回退 JSON 解析。
    """
    index_path = _index_path()
    from . import symbol_index
    index = symbol_index.load_index(index_path, max_age)
    if index is not None:
        return index
    # 回退：旧 JSON 缓存（整段 entry dict）
    cache = _cache_path()
    if not cache.exists():
        return None
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    if time.time() - payload.get("_downloaded_at", 0) < max_age:
        payload.pop("_downloaded_at", None)
        # 旧格式是完整 entry，抽成精简 record 便于使用
        result = {}
        for symbol, entry in payload.items():
            rec = symbol_index.parse_record(symbol, entry)
            if rec is not None:
                result[symbol] = rec
        # 一次性迁移：把旧 JSON 转换结果落盘为 pickle 索引，下次启动秒级加载
        # （桌面端存在 256MB 旧 JSON 缓存，转换一次后不再重复整段解析）
        try:
            symbol_index.save_index(result, _index_path())
        except OSError:
            pass
        return result
    return None


def _index_path() -> Path:
    """精简索引 pickle 路径（与原始 JSON 缓存同目录，独立文件名）。"""
    return _cache_path().with_name("symbol_index.pkl")


def _gz_path() -> Path:
    """gzip 压缩后的原始合约文件落盘路径（下载目标，约 8~11MB）。"""
    return _cache_path().with_name("symbol_file.json.gz")


def download_symbol_file(access_token: str,
                         progress: Optional[Callable[[int, int], None]] = None,
                         on_index_progress: Optional[Callable[[int], None]] = None) -> dict[str, Any]:
    """拉取合约目录（线上约 334MB JSON），gzip 压缩传输并落盘，再从 gzip 流式解析为精简索引。

    为什么 gzip：
    - 明文 JSON 334MB，服务器支持 gzip，压缩后约 8~11MB → 网络传输量降 ~30 倍；
    - 落盘只写 .gz（约 11MB），不再把 334MB 明文写进手机闪存；
    - 用 gzip.open + ijson 边解压边解析，一次遍历，避免"先写 334MB 再读再解析"的磁盘/内存峰值。

    progress(received_bytes, total_bytes) 每下载一个数据块回调（received 为压缩后字节）；
    on_index_progress(count) 每解析一批条数回调（用于让目录部分就绪/展示进度）。
    """
    try:
        # stream=True + 显式 Accept-Encoding: gzip；decode_content=False 拿到压缩字节（不先解压）
        response = requests.get(SYMBOL_FILE_URL, headers=_headers(access_token),
                                timeout=(15, 120), stream=True)
    except requests.RequestException as error:
        raise DiffAuthError(f"合约服务下载失败：{error}") from error
    if response.status_code != 200:
        raise DiffAuthError(f"合约服务下载失败（HTTP {response.status_code}）")
    gz_path = _gz_path()
    gz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = gz_path.with_suffix(".tmp")
    total = int(response.headers.get("content-length") or 0)
    received = 0
    try:
        with tmp.open("wb") as handle:
            # 读原始字节流（压缩后），按块写 .tmp；received 为压缩后字节数
            # 注意 urllib3 的 stream() 参数是 amt（不是 chunk_size）
            for chunk in response.raw.stream(amt=256 * 1024, decode_content=False):
                if not chunk:
                    continue
                handle.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
        tmp.replace(gz_path)   # 原子落盘 gz
        # 再 gzip.open + ijson 流式解析（边解压边逐条，不一次性加载全量）
        from . import symbol_index
        def _idx_progress(count: int) -> None:
            if on_index_progress is not None:
                on_index_progress(count)
        symbols = symbol_index.index_from_gzip_file(gz_path, progress=_idx_progress)
        idx_path = _index_path()
        symbol_index.save_index(symbols, idx_path)
    except (OSError, ValueError, TypeError) as error:
        # 捕获 TypeError：避免 stream() 等 API 用错时被当作"下载中"卡住
        raise DiffAuthError(f"合约文件下载/解析失败：{error}") from error
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

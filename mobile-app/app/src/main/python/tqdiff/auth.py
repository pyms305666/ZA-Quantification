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
from typing import Any, Callable, Optional

import requests
import urllib3.exceptions

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


def _part_path() -> Path:
    """identity 续传临时文件（明文 JSON 字节；可中断，下次按 Range 续）。"""
    return _cache_path().with_name("symbol_file.json.part")


def _resume_chunk_size() -> int:
    return 256 * 1024


def download_symbol_file(access_token: str,
                         progress: Optional[Callable[[int, int], None]] = None,
                         on_index_progress: Optional[Callable[[int], None]] = None) -> dict[str, Any]:
    """拉取合约目录（线上明文约 354MB JSON），双路径下载后流式解析为精简索引。

    路径 A（首选）：全量 GET + gzip。服务器压缩后仅 8~11MB，限速下 ~17 秒；
    落盘 .gz 后用 gzip.open + ijson 边解压边解析，不产生 354MB 中间文件。

    路径 B（续传）：identity + Range 断点续传。服务器实测（2026-09-18）对 Range 请求
    只返回未压缩字节（gzip 仅在无 Range 的全量 GET 生效），故续传只能基于 identity
    表示，全量 354MB、限速下约 12 分钟，但可中断续传。状态落盘
    ``symbol_file.json.part`` + ``.part.etag``：本函数单次调用不做内部重试，
    上层（DiffClient._load_symbol_file）的 5 次重试每轮经此推进续传进度。
    .part 已存在时直接走路径 B（说明快路径失败过，避免每轮重复浪费一次 gzip 尝试）。

    progress(received_bytes, total_bytes) 每下载一个数据块回调（路径 A 为压缩后字节，
    路径 B 为明文字节）；on_index_progress(count) 每解析一批条数回调。
    """
    from . import symbol_index
    if not _part_path().exists():
        try:
            return _download_gzip_index(access_token, progress, on_index_progress)
        except DiffAuthError as error:
            # 任何失败（网络/HTTP/写盘/gz 损坏）都降级到续传路径
            print(f"[catalog] gzip fast path failed: {error}; fall back to identity resume",
                  flush=True)
    symbols = _download_symbol_file_resume(access_token, progress, on_index_progress)
    symbol_index.save_index(symbols, _index_path())
    return symbols


def _download_gzip_index(access_token: str,
                         progress: Optional[Callable[[int, int], None]],
                         on_index_progress: Optional[Callable[[int], None]]) -> dict[str, Any]:
    """路径 A：全量 GET + gzip 下载 → gzip 流式解析 → 落盘索引。

    gzip 半成品无法续传，失败即删 .tmp；成功后原子落盘 .gz。
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
        symbols = symbol_index.index_from_gzip_file(gz_path, progress=on_index_progress)
        symbol_index.save_index(symbols, _index_path())
    except (OSError, ValueError, TypeError, urllib3.exceptions.HTTPError) as error:
        # 捕获 TypeError：避免 stream() 等 API 用错时被当作"下载中"卡住；
        # HTTPError（ProtocolError）不是 OSError 子类——连接中途被重置必须落到这里，才能降级续传
        raise DiffAuthError(f"合约文件下载失败：{error}") from error
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return symbols


def _load_resume_state(part: Path, etag_file: Path) -> tuple[int, str]:
    """读取续传状态：(.part 字节数, ETag)。缺 ETag 或空文件视为无从续传，从 0 收。"""
    if not part.exists():
        return 0, ""
    size = part.stat().st_size
    try:
        etag = etag_file.read_text(encoding="utf-8").strip()
    except OSError:
        etag = ""
    if size <= 0 or not etag:
        return 0, ""
    return size, etag


def _clear_resume_state(part: Path, etag_file: Path) -> None:
    for state_file in (part, etag_file):
        try:
            state_file.unlink(missing_ok=True)
        except OSError:
            pass


def _resume_headers(access_token: str, offset: int, etag: str) -> dict:
    headers = _headers(access_token)
    # 续传必须基于未压缩字节：实测服务器对 Range 请求不启用 gzip，这里显式钉死 identity
    headers["Accept-Encoding"] = "identity"
    if offset > 0:
        headers["Range"] = f"bytes={offset}-"
        if etag:
            # ETag 不匹配时服务器返回 200 全量（语义上等于"目录已更新，从头收"），防字节错位
            headers["If-Range"] = etag
    return headers


def _parse_content_range(raw: str) -> tuple[Optional[int], int]:
    """解析 'bytes 123-456/789' → (起点 123, 全量 789)；解析失败 → (None, 0)。"""
    raw = raw.strip()
    if not raw.startswith("bytes "):
        return None, 0
    body = raw[len("bytes "):]
    if "/" not in body:
        return None, 0
    span, _, total_s = body.rpartition("/")
    try:
        total = int(total_s)
    except ValueError:
        return None, 0
    head = span.split("-", 1)
    if len(head) != 2:
        return None, total
    try:
        return int(head[0]), total
    except ValueError:
        return None, total


def _total_from_416(response) -> int:
    """416 响应的 'Content-Range: bytes */TOTAL' → TOTAL（其余形式返回 0）。"""
    raw = (response.headers.get("content-range") or "").strip()
    if raw.startswith("bytes */"):
        try:
            return int(raw[len("bytes */"):])
        except ValueError:
            return 0
    return 0


def _download_symbol_file_resume(access_token: str,
                                 progress: Optional[Callable[[int, int], None]],
                                 on_index_progress: Optional[Callable[[int], None]]) -> dict[str, Any]:
    """路径 B：identity + Range 断点续传（明文 JSON 落盘 .part，完成后解析并清理）。

    分支语义：
    - 206 → 从本地偏移追加（起点不匹配视为字节错位，清状态报错）；
    - 200 → If-Range 未命中/目录已更新（或首次全量），丢弃 .part 从头收；
    - 416 → .part 比远端还大（异常）重收；若 .part 大小恰好等于远端全量，
      说明上次"下载完、解析前"中断，直接进入解析不再请求。
    解析失败（字节损坏无法靠续传修复）→ 清状态报错，下轮从 0 重收。
    """
    from . import symbol_index
    part = _part_path()
    etag_file = Path(str(part) + ".etag")
    part.parent.mkdir(parents=True, exist_ok=True)
    offset, etag = _load_resume_state(part, etag_file)
    print(f"[catalog] identity path: part_offset={offset} etag={'stored' if etag else 'none'}",
          flush=True)
    total = 0
    response = None
    for attempt in range(2):
        try:
            response = requests.get(SYMBOL_FILE_URL, headers=_resume_headers(access_token, offset, etag),
                                    timeout=(15, 120), stream=True)
        except requests.RequestException as error:
            raise DiffAuthError(f"合约服务下载失败：{error}") from error
        if response.status_code == 416 and attempt == 0:
            remote_total = _total_from_416(response)
            size = part.stat().st_size if part.exists() else 0
            if remote_total and size == remote_total:
                total = remote_total
                response = None   # .part 已完整，跳过下载直接解析
                break
            offset, etag = 0, ""   # .part 异常 → 丢弃，第二轮全量重收
            continue
        break
    if response is not None and response.status_code not in (200, 206):
        raise DiffAuthError(f"合约服务下载失败（HTTP {response.status_code}）")
    if response is not None:
        encoding = (response.headers.get("content-encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            # 续传字节必须与已落盘的 identity 字节同一表示，否则字节错位
            raise DiffAuthError(f"合约续传响应带 {encoding} 编码，拒绝续传（服务器行为变化）")
        if response.status_code == 206:
            start, total = _parse_content_range(response.headers.get("content-range") or "")
            if start is not None and start != offset:
                _clear_resume_state(part, etag_file)
                raise DiffAuthError("合约续传起点不匹配（服务器返回区间与本地偏移不一致）")
            mode, received = "ab", offset
            if progress is not None:
                progress(offset, total)
        else:
            # 200：首次全量或 If-Range 未命中（目录已更新）→ 从头收
            offset, mode, received = 0, "wb", 0
            total = int(response.headers.get("content-length") or 0)
        # ETag 必须在收第一个字节之前落盘：identity 全量约 354MB/限速 ~12 分钟，
        # 中途断流是常态；若等收完再写，首次中断会丢 ETag，续传只能从头再来
        new_etag = (response.headers.get("etag") or "").strip()
        if new_etag:
            try:
                etag_file.write_text(new_etag, encoding="utf-8")
            except OSError:
                pass
        try:
            with part.open(mode) as handle:
                for chunk in response.raw.stream(amt=_resume_chunk_size(), decode_content=False):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
        except (OSError, ValueError, TypeError, urllib3.exceptions.HTTPError) as error:
            # HTTPError（ProtocolError）不是 OSError 子类：中途断流后 .part/.etag 必须保留
            raise DiffAuthError(f"合约文件下载失败：{error}") from error
    try:
        symbols = symbol_index.index_from_file(part, progress=on_index_progress)
    except (OSError, ValueError, TypeError, symbol_index.ijson.JSONError) as error:
        # JSONError（含 IncompleteJSONError）不是 ValueError 子类，需显式列出
        _clear_resume_state(part, etag_file)
        raise DiffAuthError(f"合约文件解析失败：{error}") from error
    print(f"[catalog] identity download complete: {total} bytes, {len(symbols)} contracts",
          flush=True)
    _clear_resume_state(part, etag_file)
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

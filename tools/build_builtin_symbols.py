"""重新生成手机版内置精简合约表（tqdiff/builtin_symbols.py）。

【为什么需要这个工具】
内置表是"兜底种子"：只在"从未成功下载过完整目录 / 服务器故障"时让搜索/自选立即可用。
它包含的是【当前】的主流近期合约（期月会随时间切换），所以需要**定期重跑本脚本**刷新：
- 主力合约从 rb2609 换成 rb2610 —— 期月变了；
- 新品种上市 / 老品种退市 —— 集合变了；
- 交易所调整乘数/最小变动 —— 字段变了。

【什么时候需要跑】
- 正常情况下【几乎不用】：一旦完整目录下载成功，会持久化成 pickle 索引（见
  auth.load_cached_symbol_file，TTL 7 天），之后每次启动都读索引，内置表被完全取代。
- 只有当"新装机用户从未成功下载 + 期月已切换"时，旧内置表才显得过时。
  建议：**每 1~2 个月重跑一次**并随版本重新打包 APK即可。

【用法】
    # 方式一：用本机已缓存的完整目录生成（快，推荐——.tqsdk/symbol_file.json 是之前下载的）
    python tools/build_builtin_symbols.py

    # 方式二：指定缓存的完整目录文件
    python tools/build_builtin_symbols.py --src E:/xxx/symbol_file.json

    # 方式三：不指定时若本机无缓存，会尝试从天勤重新下载完整目录（慢，约需网络）

    # 可选参数
    --out     输出文件路径（默认 mobile-app/app/src/main/python/tqdiff/builtin_symbols.py）
    --top     每个品种保留的近期合约数量（默认 3）

【依赖】
    Python 3.8+，pip install ijson（解析完整目录用）。

【生成规则】
    从完整目录中，过滤出国内六大交易所（SHFE/DCE/CZCE/CFFEX/INE/GFEX）的未过期 FUTURE，
    按品种（合约代码的字母前缀）分组，每个品种取"剩余到期天数最近"的 --top 个合约。
    这样内置表始终跟着当前主力/近月走，覆盖主流品种。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# 保证能 import 到 mobile 版的 tqdiff（复用其中的 symbol_index 解析逻辑）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "mobile-app" / "app" / "src" / "main" / "python"))

from tqdiff import symbol_index  # noqa: E402

DOMESTIC_EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")
# 内置表记录的字段（与 market.model.Instrument / 完整索引 record 一致）
RECORD_FIELDS = ("symbol", "exchange", "instrument_id", "name", "kind", "expired",
                 "price_tick", "volume_multiple", "expire_rest_days")


def product_of(instrument_id: str) -> str:
    """合约代码的字母前缀即品种（rb2610 -> rb；IF2609 -> IF）。"""
    match = re.match(r"[A-Za-z]+", instrument_id or "")
    return match.group(0) if match else instrument_id or ""


def group_records(symbols: dict[str, dict]) -> dict[tuple[str, str], list[dict]]:
    """按 (交易所, 品种) 分组国内主流未过期 FUTURE。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for sym, rec in symbols.items():
        if sym.upper().startswith("KQD."):
            continue
        if rec.get("kind") != "FUTURE" or rec.get("expired"):
            continue
        exchange = rec.get("exchange")
        if exchange not in DOMESTIC_EXCHANGES:
            continue
        groups[(exchange, product_of(rec.get("instrument_id", "")))].append(rec)
    return groups


def build_builtin(symbols: dict[str, dict], top: int) -> dict[str, dict]:
    """每个品种取剩余到期天数最近的 top 个合约。"""
    def key(rec):
        return rec.get("expire_rest_days") if rec.get("expire_rest_days") is not None else 9999
    builtin: dict[str, dict] = {}
    for recs in group_records(symbols).values():
        recs.sort(key=key)
        for rec in recs[:top]:
            builtin[rec["symbol"]] = rec
    return builtin


def _render_py(builtin: dict[str, dict]) -> str:
    lines = [
        '"""内置精简合约目录（兜底使用）——由 tools/build_builtin_symbols.py 生成，请勿手改。',
        '',
        '用途：服务器下载慢/失败时，先用这些主流国内期货合约让"搜索/自选"立刻可用；',
        '完整目录（24万条）后台下载成功后会自动替换本表。',
        '',
        '维护：期月会切换，建议每 1~2 个月重跑一次生成脚本刷新（见 tools/build_builtin_symbols.py）。',
        '字段结构见 market.model.Instrument。',
        '"""',
        '',
        'BUILTIN_SYMBOLS: dict = {',
    ]
    for sym in sorted(builtin):
        rec = builtin[sym]
        lines.append(f"    {sym!r}: {{")
        for field in RECORD_FIELDS:
            value = rec.get(field)
            if isinstance(value, bool):
                rendered = "True" if value else "False"
            elif isinstance(value, float):
                rendered = repr(round(value, 6))
            elif value is None:
                rendered = "None"
            else:
                rendered = repr(value)
            lines.append(f"        {field!r}: {rendered},")
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines)


def load_symbols(src: Path | None) -> dict[str, dict]:
    """加载完整目录：优先指定 src，否则找本机缓存，否则重下（简化：找不到就报错提示）。"""
    if src is not None:
        if not src.exists():
            raise SystemExit(f"找不到指定目录文件：{src}")
        return symbol_index.index_from_file(src)
    # 本机可能有的完整目录缓存
    for candidate in (_PROJECT_ROOT / ".tqsdk" / "symbol_file.json",
                      _PROJECT_ROOT / ".tqsdk" / "symbol_file.json.gz"):
        if candidate.exists():
            if candidate.suffix == ".gz":
                return symbol_index.index_from_gzip_file(candidate)
            return symbol_index.index_from_file(candidate)
    raise SystemExit(
        "未找到本机缓存目录。请先用 --src 指定一份完整目录（如 .tqsdk/symbol_file.json），"
        "或先让程序成功下载一次得到缓存再生成。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="重新生成手机版内置精简合约表")
    parser.add_argument("--src", type=Path, default=None,
                        help="完整目录文件路径（.tqsdk/symbol_file.json 或 .gz）；默认自动找本机缓存")
    parser.add_argument("--out", type=Path,
                        default=_PROJECT_ROOT / "mobile-app/app/src/main/python/tqdiff/builtin_symbols.py",
                        help="输出文件路径")
    parser.add_argument("--top", type=int, default=3, help="每个品种保留的近期合约数量")
    args = parser.parse_args()

    print("加载完整目录（流式解析，不一次性读内存）...", flush=True)
    symbols = load_symbols(args.src)
    print(f"完整目录 {len(symbols)} 条", flush=True)

    builtin = build_builtin(symbols, args.top)
    content = _render_py(builtin)

    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"已生成 {out}（{len(builtin)} 个合约，{out.stat().st_size // 1024} KB）", flush=True)
    print("提示：提交后需要重新打包 APK 才会生效。")


if __name__ == "__main__":
    main()

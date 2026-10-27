"""按需查询天勤合约服务；中文品种发现和行情订阅彼此独立。

只缓存用户查过的品种，绝不请求整份月份/期权目录。CONT 查询仅返回少量
主连的名称和代码，用来发现中文名对应的品种；挂牌月份始终由 FUTURE 查询确认。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from typing import Awaitable, Callable

from .auth import load_builtin_catalog, parse_instrument_record

EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")
FUTURE_SYMBOL_RE = re.compile(r"^(SHFE|DCE|CZCE|CFFEX|INE|GFEX)\.[A-Za-z]{1,3}\d{3,4}$")

# 合约服务的变量名、参数名和类型使用白名单，搜索文本仅作为变量值传递。
# 天勤实际服务要求声明的每个变量都有值；不能一次声明全部可选变量后只传一部分，
# 也不能用 null 填充未使用的过滤器。按本次条件生成声明和参数可以兼容此限制。
QUERY_PARAMETERS = {
    "classes": ("class", "[Class]"),
    "exchanges": ("exchange_id", "[String]"),
    "products": ("product_id", "[String]"),
    "symbols": ("instrument_id", "[String]"),
    "expired": ("expired", "Boolean"),
}
SYMBOL_FIELDS = """
    ... on basic { instrument_id instrument_name exchange_id class price_tick }
    ... on future { expired expire_datetime volume_multiple product_id }
"""


def build_symbol_query(filters: dict) -> str:
    """只声明本次提供的变量，避免真实服务返回 variable does not have the key。"""
    if not filters or any(key not in QUERY_PARAMETERS for key in filters):
        raise ValueError("合约查询包含不支持的条件")
    if any(value is None for value in filters.values()):
        raise ValueError("合约查询条件不能使用空值")
    keys = [key for key in QUERY_PARAMETERS if key in filters]
    declarations = ", ".join(f"${key}:{QUERY_PARAMETERS[key][1]}" for key in keys)
    arguments = ", ".join(f"{QUERY_PARAMETERS[key][0]}:${key}" for key in keys)
    return f"query({declarations}) {{ multi_symbol_info({arguments}) {{ {SYMBOL_FIELDS} }} }}"


def clean_keyword(value: str) -> str:
    """兼容全角输入和名称/月份间的空白，例如“棕榈油 ２７０１”。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))[:80]


def delivery_matches(instrument: str, month: str) -> bool:
    """郑商所使用三位年月：用户输入 SA2701 时也能找到真实代码 SA701。"""
    digits = re.sub(r"^[A-Za-z]+", "", instrument)
    if not month:
        return True
    if len(month) == 4:
        return digits == month or (len(digits) == 3 and digits == month[1:])
    # 输入尚未完成时也给出候选，如 p27→p2701，SA27→SA701。
    return digits.startswith(month) or (len(digits) == 3 and len(month) == 2
                                        and digits.startswith(month[1:]))


class SymbolSearch:
    """运行于行情事件循环的查询缓存；相同条件的并发请求共享一条上游请求。"""

    def __init__(self, query: Callable[[dict], Awaitable[list[dict]]]) -> None:
        self._query = query
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._known: dict[str, tuple[float, dict]] = {}

    async def _fetch(self, filters: dict, refresh: bool = False) -> list[dict]:
        """正常结果缓存 60 秒，空结果仅 5 秒；失败不缓存为“合约不存在”。"""
        key = json.dumps(filters, sort_keys=True)
        cached = self._cache.get(key)
        if not refresh and cached and cached[0] > time.monotonic():
            return [dict(row) for row in cached[1]]
        task = self._pending.get(key)
        if task is None:
            async def fetch() -> list[dict]:
                try:
                    rows = await self._query(filters)
                    # 品种/月份数据量有限，限制缓存的查询条件数量，防止长期使用累积。
                    if len(self._cache) >= 128:
                        self._cache.pop(next(iter(self._cache)))
                    ttl = 3600 if filters.get("classes") == ["CONT"] else 60
                    self._cache[key] = (time.monotonic() + (ttl if rows else 5), rows)
                    return rows
                finally:
                    self._pending.pop(key, None)
            task = asyncio.create_task(fetch())
            # 首个浏览器取消请求后，共享任务仍可能继续；消费其异常避免后台任务警告。
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self._pending[key] = task
        return [dict(row) for row in await asyncio.shield(task)]

    async def _products(self, refresh: bool) -> dict[tuple[str, str], set[str]]:
        """发现中文品种名。内置表只提供名称别名，绝不提供挂牌月份或交易参数。"""
        products: dict[tuple[str, str], set[str]] = {}
        for record in load_builtin_catalog().values():
            exchange = record.get("exchange", "")
            code = re.sub(r"\d+$", "", record.get("instrument_id", ""))
            name = re.sub(r"\d+$", "", record.get("name", ""))
            if exchange in EXCHANGES and code and name:
                products.setdefault((exchange, code), set()).add(name)
        # 部分内置表缺少这些品种/使用了简称；这里只补别名，不编造任何月份。
        for exchange, code, name in (("DCE", "p", "棕榈油"), ("CZCE", "SH", "烧碱"),
                                      ("CZCE", "SA", "纯碱")):
            products.setdefault((exchange, code), set()).add(name)
        # 主连每个品种仅一条，避免为了中文搜索下载所有月份及几十万条期权。
        rows = await self._fetch({"classes": ["CONT"]}, refresh)
        for row in rows:
            match = re.fullmatch(r"KQ\.m@([A-Z]+)\.([A-Za-z]{1,3})", row.get("instrument_id", ""))
            if match and match[1] in EXCHANGES:
                name = row.get("instrument_name") or ""
                name = re.sub(r"(主力连续|主力合约|主力|主连|连续)$", "", name)
                products.setdefault((match[1], match[2]), set()).add(name)
        return products

    @staticmethod
    def _records(rows: list[dict]) -> list[dict]:
        """只接受上游确认的国内期货，并按当前时间排除已经到期的记录。"""
        output: dict[str, dict] = {}
        for row in rows:
            symbol = row.get("instrument_id", "")
            if not FUTURE_SYMBOL_RE.fullmatch(symbol) or row.get("class") != "FUTURE":
                continue
            try:
                entry = dict(row)
                # GraphQL 的 expire_datetime 实测为纳秒；旧静态文件解析器接收秒。
                # 在这一协议适配边界转换，避免把剩余天数误算成数十万亿天。
                expiry = entry.get("expire_datetime")
                if expiry is not None and abs(float(expiry)) >= 1e15:
                    entry["expire_datetime"] = float(expiry) / 1e9
                record = parse_instrument_record(symbol, entry)
            except (TypeError, ValueError, OverflowError):
                continue
            if not record or record["expired"]:
                continue
            expiry = record.get("expire_datetime")
            if expiry is not None and expiry <= time.time():
                continue
            output[record["symbol"]] = record
        return [output[key] for key in sorted(output)]

    async def search(self, exchange: str, keyword: str, refresh: bool = False,
                     limit: int = 200) -> list[dict]:
        """输入为空时立即返回；代码按品种查询，中文先解析品种再查询该品种月份。"""
        text = clean_keyword(keyword)
        exchange = exchange.strip().upper()
        if not text or (exchange and exchange not in EXCHANGES):
            return []
        filters: dict = {"classes": ["FUTURE"], "expired": False}
        if exchange:
            filters["exchanges"] = [exchange]
        match = re.fullmatch(r"(?:([A-Za-z]+)\.)?([A-Za-z]{1,3})(\d{0,4})", text)
        if match:
            prefix, code, month = match.groups()
            if prefix:
                prefix = prefix.upper()
                if prefix not in EXCHANGES or (exchange and exchange != prefix):
                    return []
                filters["exchanges"] = [prefix]
            filters["products"] = sorted({code.upper(), code.lower()})
        else:
            name_match = re.fullmatch(r"([^\d.]+?)(\d{3,4})?", text)
            if name_match is None:
                return []
            name, month = name_match.groups()
            pairs = [(ex, code) for (ex, code), names in (await self._products(refresh)).items()
                     if (not exchange or ex == exchange) and any(name in alias for alias in names)]
            if not pairs:
                return []
            filters["products"] = sorted({code for _, code in pairs})
            filters["exchanges"] = sorted({ex for ex, _ in pairs})
        rows = self._records(await self._fetch(filters, refresh))
        for row in rows:
            self._known[row["symbol"]] = (time.monotonic() + 60, dict(row))
        # 数字是月份条件；不让“纯碱2701”作为连续字符串去匹配“纯碱701”。
        rows = [row for row in rows if delivery_matches(row["instrument_id"], month or "")]
        return rows[:max(1, min(limit or 200, 300))]

    async def lookup(self, symbol: str) -> dict | None:
        """单合约校验也走服务端，旧目录的本地未命中不能证明新合约不存在。"""
        cached = self._known.get(symbol)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
        rows = self._records(await self._fetch({"classes": ["FUTURE"],
                                              "symbols": [symbol], "expired": False}))
        return next((row for row in rows if row["symbol"] == symbol), None)

    async def lookup_many(self, symbols: list[str]) -> list[dict]:
        """批量元数据入口，保持调用者不依赖内部缓存的数据结构。"""
        return self._records(await self._fetch({"classes": ["FUTURE"],
                                               "symbols": symbols, "expired": False}))

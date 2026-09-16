"""tq 层测试：动态订阅幂等、合约归一化（用假 TqClient，不依赖网络）。"""

from __future__ import annotations

import unittest

from tq.client import SymbolNotFoundError, TqClientError
from tq.instruments import InstrumentManager, normalize_symbol
from tq.subscriber import SubscriptionManager

VALID_LOWER = {"shfe.rb2610", "dce.m2609", "czce.sr609", "cffex.if2609"}
CATALOG = sorted(["SHFE.rb2610", "DCE.m2609", "CZCE.SR609", "CFFEX.IF2609"])


class FakeClient:
    """模拟 TqClient 的命令接口。"""

    connected = True
    catalog_ready = True

    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple]] = []
        self.subscribed: set[str] = set()

    def run_command(self, command: str, *args: object, timeout: float = 8.0) -> object:
        self.commands.append((command, args))
        if command == "get_instrument":
            key = str(args[0]).lower()
            if key not in VALID_LOWER:
                # 与生产一致：目录就绪但查无此合约 → SymbolNotFoundError（区别于目录未就绪）
                raise SymbolNotFoundError(f"合约不存在：{key}")
            exchange, _, instrument = key.partition(".")
            return {
                "symbol": f"{exchange.upper()}.{instrument}", "exchange": exchange.upper(),
                "instrument_id": instrument, "name": "测试品种", "kind": "FUTURE",
                "expired": False, "price_tick": 1.0, "volume_multiple": 10,
            }
        if command == "get_instruments_info":
            # 批量查询：对每个 symbol 返回与 get_instrument 一致的记录
            symbols = list(args[0])
            return {s: self.run_command("get_instrument", s) for s in symbols}
        if command == "subscribe":
            self.subscribed.add(str(args[0]))
            return str(args[0])
        if command == "unsubscribe":
            self.subscribed.discard(str(args[0]))
            return None
        if command == "query_instruments":
            return CATALOG
        raise TqClientError(f"未知命令：{command}")


class NormalizeSymbolTests(unittest.TestCase):
    def test_prefixed_symbol_passthrough(self):
        self.assertEqual(normalize_symbol(FakeClient(), "SHFE.rb2610"), "SHFE.rb2610")

    def test_bare_code_discovers_exchange(self):
        client = FakeClient()
        self.assertEqual(normalize_symbol(client, "m2609"), "DCE.m2609")
        # 郑商所/中金所合约代码规范为大写
        self.assertEqual(normalize_symbol(client, "SR609"), "CZCE.SR609")
        self.assertEqual(normalize_symbol(client, "IF2609"), "CFFEX.IF2609")

    def test_invalid_symbol(self):
        self.assertIsNone(normalize_symbol(FakeClient(), "XX9999"))

    def test_empty(self):
        self.assertIsNone(normalize_symbol(FakeClient(), ""))


class InstrumentManagerTests(unittest.TestCase):
    def test_list_and_get(self):
        client = FakeClient()
        manager = InstrumentManager(client)
        self.assertEqual(manager.futures(), CATALOG)
        item = manager.get("SHFE.rb2610")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.symbol, "SHFE.rb2610")
        self.assertEqual(item.name, "测试品种")
        records = manager.list(exchange="SHFE")
        self.assertEqual([r["symbol"] for r in records], ["SHFE.rb2610"])

    def test_keyword_matches_name_not_just_code(self):
        """缺陷 E：中文关键字（如"测试"）应能匹配到名称，而非只匹配代码。

        此前预过滤只按 symbol.lower() 截断候选，名称含"测试"但代码里没有的
        合约会被提前丢弃，后置名称匹配永远收不到。改为关键字搜索时不预过滤。
        """
        client = FakeClient()
        manager = InstrumentManager(client)
        # "测试品种" 是 FakeClient.get_instrument 返回的 name；代码里没有"测试"
        results = manager.list(keyword="测试")
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["name"], "测试品种")


class SubscriptionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.manager = SubscriptionManager(self.client, InstrumentManager(self.client))

    def test_subscribe_is_idempotent(self):
        first = self.manager.subscribe(["SHFE.rb2610", "rb2610"])
        self.assertEqual(first["subscribed"], ["SHFE.rb2610"])
        self.assertEqual(first["skipped"], [])  # 同一次请求内的重复被静默去重
        self.assertEqual(first["failed"], [])
        second = self.manager.subscribe(["SHFE.rb2610", "SHFE.rb2610"])
        self.assertEqual(second["subscribed"], [])
        self.assertEqual(second["skipped"], ["SHFE.rb2610"])
        self.assertEqual(self.manager.subscribed(), ["SHFE.rb2610"])

    def test_subscribe_invalid_symbol_reports_failure(self):
        result = self.manager.subscribe(["SHFE.xx9999"])
        self.assertEqual(result["subscribed"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("合约不存在", result["failed"][0]["reason"])

    def test_subscribe_skips_catalog_lookup_until_catalog_is_searchable(self):
        self.client.catalog_ready = False
        result = self.manager.subscribe(["SHFE.au2612"])
        self.assertEqual(result["subscribed"], ["SHFE.au2612"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.client.commands, [("subscribe", ("SHFE.au2612",))])

    def test_unsubscribe(self):
        self.manager.subscribe(["DCE.m2609"])
        result = self.manager.unsubscribe(["DCE.m2609"])
        self.assertEqual(result["unsubscribed"], ["DCE.m2609"])
        self.assertEqual(self.manager.subscribed(), [])

    def test_subscribed_list_is_sorted_unique(self):
        self.manager.subscribe(["CZCE.SR609", "SHFE.rb2610"])
        self.assertEqual(self.manager.subscribed(), ["CZCE.SR609", "SHFE.rb2610"])

    def test_disconnected_fails_fast(self):
        client = FakeClient()
        client.connected = False
        manager = SubscriptionManager(client, InstrumentManager(client))
        result = manager.subscribe(["SHFE.rb2610"])
        self.assertEqual(result["subscribed"], [])
        # 缺陷 A：未连接时记入 pending（连接就绪后自动重放），reason 文案随之更新
        self.assertEqual(result["failed"], [{"symbol": "SHFE.rb2610", "reason": "天勤未连接，已挂起待重连"}])

    def test_pending_replayed_on_connected(self):
        """缺陷 A：未连接时挂起的订阅，连接就绪后 on_connected 自动重放。"""
        client = FakeClient()
        client.connected = False
        manager = SubscriptionManager(client, InstrumentManager(client))
        # 未连接时订阅——挂起，不应出现在订阅表
        manager.subscribe(["SHFE.rb2610"])
        self.assertEqual(manager.subscribed(), [])
        # 模拟连接就绪
        client.connected = True
        manager.on_connected()
        # pending 被重放，合约进入订阅表
        self.assertEqual(manager.subscribed(), ["SHFE.rb2610"])

    def test_reconnect_replays_existing_subscriptions(self):
        """缺陷 F：重连后 on_connected 重放当前订阅表中的合约。

        模拟"已订阅 → 掉线 → 重连"：重连后 pending 为空，但订阅表仍记录
        之前的合约，on_connected 应确保它们仍被订阅（服务器侧状态已丢）。
        这里验证 on_connected 不会清空已有订阅表（重放幂等）。
        """
        client = FakeClient()
        manager = SubscriptionManager(client, InstrumentManager(client))
        manager.subscribe(["SHFE.rb2610", "DCE.m2609"])
        self.assertEqual(manager.subscribed(), ["DCE.m2609", "SHFE.rb2610"])
        # 重连：on_connected 对已订阅合约幂等，不重复报错
        manager.on_connected()
        self.assertEqual(manager.subscribed(), ["DCE.m2609", "SHFE.rb2610"])


if __name__ == "__main__":
    unittest.main()

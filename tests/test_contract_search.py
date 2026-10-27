"""按需合约搜索回归：真实协议形状的假响应，不登录或连接任何行情账户。"""

import asyncio
import re
import time
import unittest
from unittest.mock import AsyncMock, patch

from tq.instruments import InstrumentManager, normalize_symbol
from tqdiff.client import DiffClient, SymbolNotFoundError, TqClientError
from tqdiff.search import SymbolSearch, build_symbol_query


def future(symbol, name):
    """上游 GraphQL FUTURE 记录，包含真实代码形状和远期到期时间。"""
    return {"instrument_id": symbol, "instrument_name": name,
            "exchange_id": symbol.split('.')[0], "class": "FUTURE",
            "expired": False, "expire_datetime": int((time.time() + 86400 * 180) * 1e9),
            "price_tick": 1.0, "volume_multiple": 10}


ROWS = [future('DCE.p2701', '棕榈油2701'), future('DCE.p2705', '棕榈油2705'),
        future('CZCE.SH701', '烧碱701'), future('CZCE.SA701', '纯碱701'),
        future('CFFEX.IF2701', '沪深3002701')]
PRODUCTS = [{"instrument_id": "KQ.m@DCE.p", "instrument_name": "棕榈油主连"},
            {"instrument_id": "KQ.m@CZCE.SH", "instrument_name": "烧碱主连"},
            {"instrument_id": "KQ.m@CZCE.SA", "instrument_name": "纯碱主连"}]


class QueryCompatibilityTests(unittest.TestCase):
    def test_every_declared_variable_has_a_value_for_each_real_query_shape(self):
        # 真实天勤错误回归：旧固定模板声明 products/symbols 等变量，CONT 只传 classes，
        # 上游因此返回 variable does not have the key。模拟器也必须检查这一约束。
        cases = [{"classes": ["CONT"]},
                 {"classes": ["FUTURE"], "products": ["SH"], "exchanges": ["CZCE"], "expired": False},
                 {"classes": ["FUTURE"], "symbols": ["DCE.p2701"], "expired": False}]
        for filters in cases:
            query = build_symbol_query(filters)
            declared = set(re.findall(r"\$(\w+)\s*:", query.split("{", 1)[0]))
            self.assertEqual(declared, set(filters))
            self.assertEqual(set(re.findall(r"\$(\w+)", query)), set(filters))

    def test_unknown_filter_names_and_null_filters_are_rejected(self):
        with self.assertRaises(ValueError):
            build_symbol_query({"unknown": "value"})
        with self.assertRaises(ValueError):
            build_symbol_query({"products": None})

    def test_graphql_nanoseconds_become_seconds_before_expiry_validation(self):
        row = future("CZCE.SH701", "烧碱2701")
        record = SymbolSearch._records([row])[0]
        self.assertLess(abs(record["expire_datetime"] - row["expire_datetime"] / 1e9), 0.001)
        self.assertTrue(178 <= record["expire_rest_days"] <= 180)
        row["expire_datetime"] = int((time.time() - 10) * 1e9)
        self.assertEqual(SymbolSearch._records([row]), [])


class ContractSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        async def query(filters):
            self.calls.append(filters)
            await asyncio.sleep(0.01)  # 让并发请求有机会进入同一个查询。
            if filters['classes'] == ['CONT']:
                return PRODUCTS
            output = ROWS
            if 'symbols' in filters:
                output = [row for row in output if row['instrument_id'] in filters['symbols']]
            if 'exchanges' in filters:
                output = [row for row in output if row['exchange_id'] in filters['exchanges']]
            if 'products' in filters:
                import re
                output = [row for row in output
                          if re.sub(r'\d+$', '', row['instrument_id'].split('.')[1]) in filters['products']]
            return output

        self.search = SymbolSearch(query)

    async def test_empty_and_number_only_input_never_queries_full_catalog(self):
        self.assertEqual(await self.search.search('', ''), [])
        self.assertEqual(await self.search.search('', '2701'), [])
        self.assertEqual(self.calls, [])

    async def test_reported_products_and_chinese_month(self):
        cases = {'棕榈油 ２７０１': ['DCE.p2701'], '烧碱': ['CZCE.SH701'],
                 '纯碱': ['CZCE.SA701'], 'sa2701': ['CZCE.SA701'],
                 'CFFEX.IF2701': ['CFFEX.IF2701']}
        for keyword, expected in cases.items():
            rows = await self.search.search('', keyword)
            self.assertEqual([row['symbol'] for row in rows], expected, keyword)
        # 每次 FUTURE 请求都必须按代码或品种收窄；CONT 是轻量名称发现。
        for call in self.calls:
            if call['classes'] == ['FUTURE']:
                self.assertTrue('products' in call or 'symbols' in call)

    async def test_partial_month_input_keeps_product_candidates(self):
        self.assertEqual([row['symbol'] for row in await self.search.search('', 'p27')],
                         ['DCE.p2701', 'DCE.p2705'])
        self.assertEqual([row['symbol'] for row in await self.search.search('', 'SA27')], ['CZCE.SA701'])

    async def test_search_does_not_invent_unlisted_months(self):
        self.assertEqual(await self.search.search('', '棕榈油2901'), [])

    async def test_exchange_filter_and_mismatched_prefix(self):
        self.assertEqual(await self.search.search('SHFE', 'p2701'), [])
        self.assertEqual(await self.search.search('SHFE', 'DCE.p2701'), [])

    async def test_same_product_queries_share_request_and_short_cache(self):
        results = await asyncio.gather(*(self.search.search('', 'p2701') for _ in range(4)))
        self.assertTrue(all(rows[0]['symbol'] == 'DCE.p2701' for rows in results))
        self.assertEqual(len(self.calls), 1)
        await self.search.search('', 'p2705')
        self.assertEqual(len(self.calls), 1)
        await self.search.search('', 'p2701', refresh=True)
        self.assertEqual(len(self.calls), 2)

    async def test_search_then_open_detail_reuses_verified_metadata(self):
        await self.search.search('', 'p2701')
        record = await self.search.lookup('DCE.p2701')
        self.assertEqual(record['symbol'], 'DCE.p2701')
        self.assertEqual(len(self.calls), 1)

    async def test_cached_results_expire_and_failures_are_not_negative_results(self):
        await self.search.search('', 'p')
        for key, (_, rows) in list(self.search._cache.items()):
            self.search._cache[key] = (0, rows)
        await self.search.search('', 'p')
        self.assertEqual(len(self.calls), 2)
        failing = SymbolSearch(AsyncMock(side_effect=TqClientError('服务暂不可用')))
        for _ in range(2):
            with self.assertRaises(TqClientError):
                await failing.search('', 'SA')
        self.assertEqual(failing._query.await_count, 2)

    async def test_dynamic_product_names_find_new_products(self):
        async def query(filters):
            if filters['classes'] == ['CONT']:
                return [{'instrument_id': 'KQ.m@DCE.zz', 'instrument_name': '新挂牌品种主连'}]
            self.assertEqual(filters['products'], ['zz'])
            return [future('DCE.zz2701', '新挂牌品种2701')]
        self.assertEqual((await SymbolSearch(query).search('', '新挂牌品种'))[0]['symbol'], 'DCE.zz2701')


class SearchProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_is_one_shot_and_never_subscribes_market(self):
        client = DiffClient('', '')
        client._ws = object()
        packets = []

        async def send(packet):
            packets.append(packet)
            if packet.get('query'):
                declared = set(re.findall(r'\$(\w+)\s*:', packet['query'].split('{', 1)[0]))
                self.assertEqual(declared, set(packet['variables']))
                client._apply_diff({'symbols': {packet['query_id']: {
                    'result': {'multi_symbol_info': [ROWS[0]]}}}})
        client._send = send
        rows = await client._execute('search_instruments', ('', 'p2701', False, 200))
        self.assertEqual(rows[0]['symbol'], 'DCE.p2701')
        self.assertEqual([packet['aid'] for packet in packets], ['ins_query', 'ins_query'])
        self.assertEqual(packets[-1]['query'], '')
        self.assertEqual(client._subscribed, set())
        self.assertFalse(client.catalog_loading)

    async def test_upstream_rejection_is_logged_and_query_is_released(self):
        client = DiffClient('', '')
        client._ws = object()
        packets = []

        async def send(packet):
            packets.append(packet)
            if packet.get('query'):
                client._apply_diff({'symbols': {packet['query_id']: {
                    'error': 'variable does not have the key: products'}}})
        client._send = send
        with self.assertLogs('gateway.tqdiff', level='WARNING') as logs:
            with self.assertRaises(TqClientError):
                await client._query_symbols({'classes': ['CONT']})
        self.assertIn('variable does not have the key: products', logs.output[0])
        self.assertEqual(packets[-1]['query'], '')
        self.assertFalse(client.catalog_loading)

    async def test_startup_never_downloads_full_catalog(self):
        client = DiffClient('', '')
        with patch('tqdiff.auth.download_symbol_file', side_effect=AssertionError('禁止完整下载')):
            await client._load_symbol_file()
        self.assertFalse(client.catalog_complete)
        self.assertEqual(client.known_futures_count, 0)

    async def test_local_complete_cache_cannot_hide_new_contract(self):
        client = DiffClient('', '')
        client._catalog_complete.set()  # 模拟旧路径留下的标记；不能据此拒绝新合约。
        client._symbol_search = SymbolSearch(AsyncMock(return_value=[ROWS[0]]))
        self.assertEqual((await client._get_instrument('DCE.p2701'))['symbol'], 'DCE.p2701')

    async def test_successful_empty_response_is_not_found(self):
        client = DiffClient('', '')
        client._symbol_search = SymbolSearch(AsyncMock(return_value=[]))
        with self.assertRaises(SymbolNotFoundError):
            await client._get_instrument('DCE.p2901')

    async def test_unsubscribe_last_quote_and_chart_releases_server_resources(self):
        client = DiffClient('', '')
        client._subscribed.add('CZCE.SA701')
        client._charts[('CZCE.SA701', 300000000000)] = {}
        client._send = AsyncMock()
        await client._unsubscribe('CZCE.SA701')
        packets = [call.args[0] for call in client._send.await_args_list]
        self.assertEqual({packet['aid'] for packet in packets}, {'set_chart', 'subscribe_quote'})
        self.assertTrue(all(packet['ins_list'] == '' for packet in packets))
        self.assertEqual(client._charts, {})


class SearchManagerTests(unittest.TestCase):
    def test_prefixed_czce_four_digit_month_normalizes_to_exchange_code(self):
        self.assertEqual(normalize_symbol(unittest.mock.Mock(), 'czce.sa2701'), 'CZCE.SA701')

    def test_manager_empty_search_does_not_enumerate_or_subscribe(self):
        client = unittest.mock.Mock(supports_symbol_search=True)
        self.assertEqual(InstrumentManager(client).list(), [])
        client.run_command.assert_not_called()

    def test_bare_code_does_one_search_instead_of_six_exchange_lookups(self):
        client = unittest.mock.Mock(supports_symbol_search=True)
        client.run_command.return_value = [{'symbol': 'CZCE.SA701'}]
        self.assertEqual(normalize_symbol(client, 'sa2701'), 'CZCE.SA701')
        client.run_command.assert_called_once_with('search_instruments', '', 'sa2701', False, 2, timeout=12.0)

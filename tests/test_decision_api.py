"""Exercise both ASGI APIs with identical fake market data (no network/account)."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import config
from api.http import create_app
from tests.test_decision_profiles import NOW, INSTRUMENT, history, fresh_quote


async def get(app, query="", path="/api/v1/decision/SHFE.rb2705"):
    messages = []
    async def receive():
        return {"type":"http.request", "body":b"", "more_body":False}
    async def send(message):
        messages.append(message)
    await app(dict(type="http", asgi={"version":"3.0"}, method="GET", path=path,
                   raw_path=path.encode(), root_path="", query_string=query.encode(),
                   headers=[], scheme="http", server=("test",80), client=("test",1),
                   http_version="1.1"), receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = json.loads(b"".join(m.get("body",b"") for m in messages if m["type"] == "http.response.body"))
    return status, body


class DecisionAPITests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.bars = history()
        def command(name, symbol, period, count, **kwargs):
            self.calls.append((period,count))
            return self.bars[period][-count:]
        self.services = SimpleNamespace(config=config.Config(),
            client=SimpleNamespace(connected=True,ready=True,run_command=command),
            instruments=SimpleNamespace(get=lambda _: INSTRUMENT), subscriptions=None,
            cache={INSTRUMENT.symbol: SimpleNamespace(to_dict=fresh_quote)})
        with patch("api.http.build_services", return_value=self.services):
            self.desktop = create_app(self.services.config)
        path = Path(__file__).resolve().parents[1]/"mobile-app/app/src/main/python/mobile_api.py"
        spec = importlib.util.spec_from_file_location("qa_mobile_api",path)
        module = importlib.util.module_from_spec(spec)
        with patch.object(config,"clear_credentials",create=True):
            spec.loader.exec_module(module)
        with patch.object(module,"build_services",return_value=self.services):
            self.mobile = module.create_mobile_app(module.MobileHub(self.services.config))

    def test_modes_settings_and_metadata_match_on_both_apis(self):
        for mode in ("ultra","short","medium","long"):
            query = "mode=%s&account_equity=10000&max_loss_per_trade=100&risk_percent=0.5&max_contracts=2" % mode
            with self.subTest(mode=mode), patch("market.evaluator.time.time",return_value=NOW/1000):
                a = asyncio.run(get(self.desktop,query))
                b = asyncio.run(get(self.mobile,query))
                self.assertEqual(a[0],200)
                self.assertEqual(a,b)
                self.assertEqual(a[1]["risk_budget"],50)
                self.assertEqual(a[1]["mode"],mode)
        self.calls.clear()
        asyncio.run(get(self.desktop,"mode=long"))
        self.assertEqual(self.calls,[(86400,400)])

    def test_invalid_parameters_rejected_before_loading_history(self):
        for app in (self.desktop,self.mobile):
            for query in ("mode=bogus","mode=ultra&risk_percent=nan","max_contracts=1.5"):
                self.assertEqual(asyncio.run(get(app,query))[0],422)
        self.assertEqual(self.calls,[])

    def test_legacy_requests_and_public_catalog(self):
        for app in (self.desktop,self.mobile):
            status, body = asyncio.run(get(app))
            self.assertEqual(status,200)
            self.assertEqual(body["mode"],"legacy")
            status, body = asyncio.run(get(app,path="/api/v1/decision-profiles"))
            self.assertEqual(status,200)
            self.assertEqual(len(body["profiles"]),4)
            self.assertNotIn("tqsdk",body)


if __name__ == "__main__":
    unittest.main()

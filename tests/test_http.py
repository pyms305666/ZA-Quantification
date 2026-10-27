"""HTTP 状态接口回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

from api.http import create_app



def find_route(app, path, method="GET"):
    """兼容 FastAPI 不同版本对 include_router 的路由展开方式。"""
    pending = list(app.routes)
    while pending:
        route = pending.pop(0)
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route
        pending.extend(getattr(getattr(route, "original_router", None), "routes", []))
    raise AssertionError(f"missing route: {method} {path}")

class StatusEndpointTests(unittest.TestCase):
    def test_partial_catalog_reports_loading_until_complete(self):
        services = SimpleNamespace(
            client=SimpleNamespace(
                connected=True,
                account="",
                error=None,
                catalog_ready=True,
                catalog_complete=False,
                catalog_progress="解析 1000 条",
            ),
            subscriptions=SimpleNamespace(subscribed=lambda: []),
            cache={},
            instruments=SimpleNamespace(futures=lambda: ["SHFE.rb2610"]),
            last_quote_unix=0.0,
            quote_recv_total=0,
            connections=SimpleNamespace(client_count=lambda: 0),
        )
        with patch("api.http.build_services", return_value=services):
            app = create_app(SimpleNamespace())
        status_route = find_route(app, "/api/v1/status")
        body = status_route.endpoint(services)

        self.assertTrue(body["catalog_ready"])
        self.assertFalse(body["catalog_complete"])
        self.assertTrue(body["catalog_loading"])

    def test_search_status_does_not_enumerate_catalog_or_claim_download(self):
        services = SimpleNamespace(
            client=SimpleNamespace(connected=True, account="", error=None, supports_symbol_search=True,
                                   known_futures_count=7, catalog_ready=True, catalog_complete=False,
                                   catalog_loading=False, catalog_progress=None),
            subscriptions=SimpleNamespace(subscribed=lambda: []), cache={},
            instruments=SimpleNamespace(futures=MagicMock(side_effect=AssertionError("禁止枚举目录"))),
            last_quote_unix=0, quote_recv_total=0,
            connections=SimpleNamespace(client_count=lambda: 0))
        with patch("api.http.build_services", return_value=services):
            app = create_app(SimpleNamespace())
        body = find_route(app, "/api/v1/status").endpoint(services)
        self.assertEqual(body["catalog_mode"], "search")
        self.assertEqual(body["futures_count"], 7)
        self.assertFalse(body["catalog_loading"])
        services.instruments.futures.assert_not_called()

    def test_delete_auth_clears_credentials_and_resets_client(self):
        mock_client = MagicMock()
        services = SimpleNamespace(client=mock_client)
        with patch("api.http.build_services", return_value=services):
            app = create_app(SimpleNamespace())
        delete_route = find_route(app, "/api/v1/auth", "DELETE")
        with patch("api.http.clear_credentials") as mock_clear:
            body = delete_route.endpoint(services)
            self.assertEqual(body, {"ok": True})
            mock_clear.assert_called_once()
            mock_client.set_credentials.assert_called_once_with("", "")


if __name__ == "__main__":
    unittest.main()

"""HTTP 状态接口回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.http import create_app


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
        api_router = next(
            route.original_router for route in app.routes
            if any(getattr(child, "path", None) == "/api/v1/status"
                   for child in getattr(getattr(route, "original_router", None), "routes", []))
        )
        status_route = next(route for route in api_router.routes
                            if route.path == "/api/v1/status")
        body = status_route.endpoint(services)

        self.assertTrue(body["catalog_ready"])
        self.assertFalse(body["catalog_complete"])
        self.assertTrue(body["catalog_loading"])


if __name__ == "__main__":
    unittest.main()

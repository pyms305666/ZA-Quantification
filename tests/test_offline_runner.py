"""Isolation boundaries of the offline test entry point (no live services)."""
from contextlib import nullcontext
import asyncio
import io
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import run_offline_tests as offline


class OfflineEnvironmentTests(unittest.TestCase):
    def test_account_and_runtime_overrides_are_replaced(self):
        environment = {"PATH": "keep", "TQ_ACCOUNT": "synthetic-account",
                       "TQ_PASSWORD": "synthetic-password", "TQ_GATEWAY_KEYRING": "off",
                       "TQ_GATEWAY_CONFIG": "outside-config", "TQ_MD_URL": "outside-url"}
        root = Path("isolated")
        result = offline.isolated_environment(environment, root)
        self.assertEqual(result["PATH"], "keep")
        for key in ("TQ_ACCOUNT", "TQ_PASSWORD", "TQ_GATEWAY_KEYRING", "TQ_MD_URL"):
            self.assertNotIn(key, result)
        self.assertEqual(result["TQ_GATEWAY_CONFIG"], str(root / "config.json"))
        self.assertEqual(result["TQ_GATEWAY_CREDENTIALS"], str(root / "credentials.json"))
        self.assertEqual(result["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(environment["TQ_GATEWAY_CONFIG"], "outside-config")

    def test_relative_writes_and_nested_temps_are_removed_and_cwd_restored(self):
        original_directory = Path.cwd()
        original_environment = dict(os.environ)
        with offline.isolated_runtime() as root:
            self.assertEqual(Path.cwd(), root)
            Path("relative-file.txt").write_text("fixture", encoding="utf-8")
            nested = tempfile.mkdtemp()
            self.assertTrue(Path(nested).is_relative_to(root))
            self.assertEqual(os.environ["TQ_GATEWAY_CONFIG"], str(root / "config.json"))
        self.assertFalse(root.exists())
        self.assertEqual(Path.cwd(), original_directory)
        self.assertEqual(dict(os.environ), original_environment)


class OfflineNetworkTests(unittest.TestCase):
    def test_asyncio_internal_socketpair_remains_available(self):
        async def synthetic():
            return "synthetic"
        with offline.network_guard():
            self.assertEqual(asyncio.run(synthetic()), "synthetic")

    def test_connection_and_dns_attempts_are_blocked_without_opening_a_socket(self):
        with offline.network_guard(), socket.socket() as client:
            for operation in (lambda: client.connect(("203.0.113.1", 443)),
                              lambda: client.connect_ex(("127.0.0.1", 8000)),
                              lambda: socket.getaddrinfo("example.invalid", 443)):
                with self.assertRaises(offline.OfflineNetworkError):
                    operation()

    def test_only_literal_loopback_is_eligible_for_fixture_networks(self):
        self.assertTrue(offline._loopback(("127.0.0.1", 1234)))
        self.assertTrue(offline._loopback(("::1", 1234)))
        self.assertFalse(offline._loopback(("localhost", 1234)))
        self.assertFalse(offline._loopback(("203.0.113.1", 1234)))


class OfflineExitCodeTests(unittest.TestCase):
    def run_with_result(self, count, successful):
        suite = mock.Mock()
        suite.countTestCases.return_value = count
        loader = mock.Mock()
        loader.discover.return_value = suite
        runner = mock.Mock()
        runner.run.return_value = SimpleNamespace(wasSuccessful=lambda: successful)
        with mock.patch.object(offline, "isolated_runtime", return_value=nullcontext()), \
                mock.patch.object(offline, "network_guard", return_value=nullcontext()), \
                mock.patch.object(offline.unittest, "TestLoader", return_value=loader), \
                mock.patch.object(offline.unittest, "TextTestRunner", return_value=runner), \
                mock.patch.object(offline.sys, "stderr", io.StringIO()):
            code = offline.run_tests()
        return code, runner

    def test_failure_returns_nonzero(self):
        code, _ = self.run_with_result(1, False)
        self.assertEqual(code, 1)

    def test_success_returns_zero(self):
        code, _ = self.run_with_result(1, True)
        self.assertEqual(code, 0)

    def test_empty_discovery_is_an_error_not_a_pass(self):
        code, runner = self.run_with_result(0, True)
        self.assertEqual(code, 2)
        runner.run.assert_not_called()

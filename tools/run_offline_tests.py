"""Run Python tests with temporary runtime paths and no account or network access.

Usage: python -B tools/run_offline_tests.py
Only synthetic tests run here. Real GUI/market tests use a separate, explicit flow.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, ExitStack
import ipaddress
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class OfflineNetworkError(RuntimeError):
    """A test attempted a network operation outside its fixture boundary."""


def isolated_environment(environment: dict[str, str], directory: Path) -> dict[str, str]:
    """Build a child/test environment without inheriting TQ account settings."""
    result = {key: value for key, value in environment.items()
              if not key.upper().startswith("TQ_")}
    result.update({
        "TQ_GATEWAY_CONFIG": str(directory / "config.json"),
        "TQ_GATEWAY_CREDENTIALS": str(directory / "credentials.json"),
        "TQ_GATEWAY_CACHE": str(directory / "cache"),
        "TQ_MOBILE_FILES_DIR": str(directory / "mobile"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "TMP": str(directory / "tmp"),
        "TEMP": str(directory / "tmp"),
        "TMPDIR": str(directory / "tmp"),
    })
    return result


def _loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        # Require a literal IP: a hostname could resolve outside loopback.
        return False


@contextmanager
def network_guard(allow_loopback: bool = False):
    """Block Python socket/DNS/async connection calls; fixture loopback is opt-in."""
    internal = threading.local()

    def guard(original):
        def checked(*args, **kwargs):
            # Socket methods receive (self, address); resolver functions (host, ...).
            address = args[1] if args and isinstance(args[0], socket.socket) else args[0]
            if (allow_loopback or getattr(internal, "socketpair", False)) and _loopback(address):
                return original(*args, **kwargs)
            raise OfflineNetworkError("Network access is disabled in offline tests")
        return checked

    async def no_async_connection(*args, **kwargs):
        raise OfflineNetworkError("Async network access is disabled in offline tests")

    original_socketpair = socket.socketpair

    def local_socketpair(*args, **kwargs):
        # Windows implements asyncio's self-wakeup pipe with a private socket pair.
        # Allow only its synchronous, same-thread literal loopback connection.
        previous = getattr(internal, "socketpair", False)
        internal.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            internal.socketpair = previous

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(socket, "socketpair", local_socketpair))
        for name in ("connect", "connect_ex"):
            stack.enter_context(mock.patch.object(socket.socket, name,
                                                   guard(getattr(socket.socket, name))))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            stack.enter_context(mock.patch.object(socket, name, guard(getattr(socket, name))))
        if not allow_loopback:
            stack.enter_context(mock.patch.object(asyncio.BaseEventLoop, "create_connection",
                                                   no_async_connection))
        yield


@contextmanager
def isolated_runtime():
    """Keep config, credentials, caches and test-created relative files temporary."""
    previous_directory = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="za-offline-tests-") as name:
        directory = Path(name)
        for child in ("tmp", "cache", "mobile"):
            (directory / child).mkdir()
        environment = isolated_environment(dict(os.environ), directory)
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(tempfile, "tempdir", str(directory / "tmp")):
            try:
                os.chdir(directory)
                yield directory
            finally:
                os.chdir(previous_directory)


def run_tests(pattern: str = "test*.py", verbosity: int = 2,
              allow_loopback: bool = False) -> int:
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with isolated_runtime(), network_guard(allow_loopback):
        import config
        # Individual credential tests inject their own FakeVault. No real vault is used.
        with mock.patch.object(config, "keyring", None):
            suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern,
                                                   top_level_dir=str(ROOT))
            if not suite.countTestCases():
                print("No offline tests discovered", file=sys.stderr)
                return 2
            result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--allow-loopback", action="store_true",
                        help="Allow literal loopback addresses for private fixture servers")
    args = parser.parse_args(argv)
    return run_tests(args.pattern, 1 if args.quiet else 2, args.allow_loopback)


if __name__ == "__main__":
    sys.exit(main())

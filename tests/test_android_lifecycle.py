"""Android 生命周期测试：仅使用内存 Fake，不启动端口或读取凭据。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ANDROID_PYTHON = (Path(__file__).resolve().parents[1] / "mobile-app" / "app" /
                  "src" / "main" / "python")


def load_android_module(name):
    spec = importlib.util.spec_from_file_location(
        "_android_lifecycle_" + name, ANDROID_PYTHON / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    # 桌面 config 可能没有移动版的退出登录函数；导入时也不访问凭据。
    fake_services = types.SimpleNamespace(build_services=mock.Mock())
    with mock.patch("config.clear_credentials", create=True), mock.patch.dict(
            sys.modules, {"services": fake_services}):
        spec.loader.exec_module(module)
    return module


class FakeServices:
    def __init__(self):
        self.client = types.SimpleNamespace(connected=False)
        self.instruments = object()
        self.subscriptions = object()
        self.cache = mock.Mock()
        self.last_quote_unix = 0
        self.quote_recv_total = 0
        self.starts = 0
        self.closes = 0

    def start(self):
        self.starts += 1

    async def close(self):
        self.closes += 1


class MobileHubLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_android_module("mobile_api")
        self.services = FakeServices()
        with mock.patch.object(self.module, "build_services", return_value=self.services):
            self.hub = self.module.MobileHub(object())

    async def asyncTearDown(self):
        if self.hub.loop is not None or self.hub.broadcast_task is not None:
            await self.hub.on_shutdown()

    async def test_startup_is_idempotent_and_retains_broadcast_task(self):
        self.hub.on_startup()
        task = self.hub.broadcast_task
        self.hub.on_startup()
        self.assertIs(task, self.hub.broadcast_task)
        self.assertEqual(self.services.starts, 1)
        self.assertIs(self.hub.loop, asyncio.get_running_loop())
        await self.hub.on_shutdown()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.services.closes, 1)

    async def test_shutdown_cancels_broadcast_and_closes_all_sockets(self):
        closed = []
        slow_cancelled = asyncio.Event()

        class Socket:
            def __init__(self, name):
                self.name = name

            async def accept(self):
                pass

            async def send_json(self, payload):
                pass

            async def close(self, code):
                closed.append(self.name)
                if self.name == "failed":
                    raise RuntimeError("already closed")
                if self.name == "slow":
                    try:
                        await asyncio.Event().wait()
                    finally:
                        slow_cancelled.set()

        for name in ("fast", "failed", "slow"):
            await self.hub.connections.connect(Socket(name), {"type": "hello"})
        self.hub.on_startup()
        task = self.hub.broadcast_task
        await asyncio.sleep(0)  # 让广播泵进入 queue.get，不依赖定时 sleep。
        original_wait_for = asyncio.wait_for

        async def short_wait_for(awaitable, timeout):
            self.assertEqual(timeout, 1.0)
            return await original_wait_for(awaitable, timeout=0.01)

        with mock.patch.object(self.module.asyncio, "wait_for", side_effect=short_wait_for):
            await self.hub.on_shutdown()
        self.assertEqual(set(closed), {"fast", "failed", "slow"})
        self.assertTrue(slow_cancelled.is_set())
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.hub.loop)
        self.assertIsNone(self.hub.ingress)
        self.assertIsNone(self.hub.broadcast_task)
        self.assertEqual(self.hub.connections.client_count(), 0)
        self.assertEqual(self.services.closes, 1)

    async def test_quote_after_shutdown_still_updates_cache_without_enqueue(self):
        self.hub.on_startup()
        await self.hub.on_shutdown()
        quote = object()
        self.hub.on_quote_change(quote)
        self.services.cache.set.assert_called_once_with(quote)
        self.assertEqual(self.services.quote_recv_total, 1)
        self.assertIsNone(self.hub.ingress)

    async def test_lifespan_shuts_down_after_body_error(self):
        hub = types.SimpleNamespace(static_dir=None, on_startup=mock.Mock(),
                                    on_shutdown=mock.AsyncMock())
        app = self.module.create_mobile_app(hub)
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("body failed")
        hub.on_startup.assert_called_once_with()
        hub.on_shutdown.assert_awaited_once_with()

    async def test_lifespan_shuts_down_after_startup_error(self):
        hub = types.SimpleNamespace(static_dir=None,
                                    on_startup=mock.Mock(side_effect=RuntimeError("start failed")),
                                    on_shutdown=mock.AsyncMock())
        app = self.module.create_mobile_app(hub)
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            async with app.router.lifespan_context(app):
                self.fail("startup should have failed")
        hub.on_shutdown.assert_awaited_once_with()


class MobileServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.module = load_android_module("mobile_api")
        self.events = []
        events = self.events

        class FakeServer:
            def __init__(self, config):
                self.config = config
                self.should_exit = False

            def run(self):
                events.append("run")

        self.uvicorn = types.SimpleNamespace(Config=mock.Mock(return_value=object()),
                                             Server=FakeServer)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(sys.modules, {"uvicorn": self.uvicorn}))
        self.stack.enter_context(mock.patch.object(self.module, "load_config", return_value=object()))
        self.stack.enter_context(mock.patch.object(self.module, "configure_logging"))
        self.wait_stopped = mock.Mock()
        self.stack.enter_context(mock.patch.object(self.module, "MobileHub", return_value=
            types.SimpleNamespace(services=types.SimpleNamespace(wait_stopped=self.wait_stopped))))
        self.addCleanup(self.wait_stopped.assert_called_once_with)
        self.app = object()
        self.stack.enter_context(mock.patch.object(self.module, "create_mobile_app", return_value=self.app))

    def test_server_is_registered_before_run(self):
        registered = []

        def on_server(server):
            registered.append(server)
            self.events.append("register")

        self.module.run_mobile_server(on_server=on_server, should_exit=lambda: False)
        self.assertEqual(self.events, ["register", "run"])
        self.assertEqual(len(registered), 1)
        self.uvicorn.Config.assert_called_once_with(
            self.app, host="127.0.0.1", port=8000, log_level="warning", access_log=False, log_config=None)

    def test_persistent_stop_skips_server_run(self):
        registered = []
        self.module.run_mobile_server(on_server=registered.append, should_exit=lambda: True)
        self.assertEqual(self.events, [])
        self.assertTrue(registered[0].should_exit)

    def test_stop_during_registration_skips_server_run(self):
        def on_server(server):
            server.should_exit = True

        self.module.run_mobile_server(on_server=on_server, should_exit=lambda: False)
        self.assertEqual(self.events, [])

    def test_server_error_still_waits_for_managed_threads(self):
        with mock.patch.object(self.uvicorn.Server, "run", side_effect=RuntimeError("Fake server failed")):
            with self.assertRaisesRegex(RuntimeError, "Fake server failed"):
                self.module.run_mobile_server()

    def test_app_initialization_error_still_waits_for_managed_threads(self):
        with mock.patch.object(self.module, "create_mobile_app", side_effect=RuntimeError("Fake app failed")):
            with self.assertRaisesRegex(RuntimeError, "Fake app failed"):
                self.module.run_mobile_server()


class BackendThreadLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.module = load_android_module("backend_main")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # 替换目录、环境与模块搜索表，后台测试不会改变真实 cwd/env 或创建文件。
        self.stack.enter_context(mock.patch.object(self.module, "files_dir", return_value="fake-mobile-files"))
        self.stack.enter_context(mock.patch.object(self.module.Path, "mkdir"))
        self.stack.enter_context(mock.patch.object(self.module.os, "chdir"))
        self.stack.enter_context(mock.patch.object(self.module.os, "environ", {}))
        self.stack.enter_context(mock.patch.object(self.module.sys, "path", list(sys.path)))
        self.release_events = []
        self.addCleanup(self.cleanup_thread)

    def cleanup_thread(self):
        for event in self.release_events:
            event.set()
        self.assertTrue(self.module.stop(timeout=1.0), "Fake backend thread did not exit")

    def install_server(self, func):
        self.stack.enter_context(mock.patch.dict(
            sys.modules, {"mobile_api": types.SimpleNamespace(run_mobile_server=func)}))

    def test_import_has_no_automatic_start(self):
        with mock.patch("threading.Thread") as thread:
            load_android_module("backend_main")
        thread.assert_not_called()

    def test_start_and_stop_are_idempotent_and_stopping_keeps_thread(self):
        entered = threading.Event()
        release = threading.Event()
        self.release_events.append(release)
        calls = []
        server = types.SimpleNamespace(should_exit=False)

        def run_server(*args, **kwargs):
            calls.append(1)
            kwargs["on_server"](server)
            entered.set()
            release.wait(2.0)

        self.install_server(run_server)
        self.module.start()
        self.assertTrue(entered.wait(1.0))
        thread = self.module._thread
        self.module.start()
        self.assertEqual(calls, [1])
        self.module.request_stop()
        self.assertTrue(server.should_exit)
        self.assertFalse(self.module.stop(timeout=0.0))
        self.assertIs(self.module._thread, thread)
        self.module.start()
        self.assertIs(self.module._thread, thread)
        release.set()
        self.assertTrue(self.module.stop(timeout=1.0))
        self.assertIsNone(self.module._thread)
        self.assertTrue(self.module.stop(timeout=0.0))

    def test_stop_before_server_registration_is_retained(self):
        preparing = threading.Event()
        release = threading.Event()
        registered = threading.Event()
        self.release_events.append(release)
        server = types.SimpleNamespace(should_exit=False)
        stop_state = []

        def run_server(*args, **kwargs):
            preparing.set()
            release.wait(2.0)
            kwargs["on_server"](server)
            stop_state.append(kwargs["should_exit"]())
            registered.set()

        self.install_server(run_server)
        self.module.start()
        self.assertTrue(preparing.wait(1.0))
        self.module.request_stop()
        release.set()
        self.assertTrue(registered.wait(1.0))
        self.assertTrue(server.should_exit)
        self.assertEqual(stop_state, [True])
        self.assertTrue(self.module.stop(timeout=1.0))

    def test_restart_only_after_previous_thread_has_exited(self):
        entered = threading.Event()

        def run_server(*args, **kwargs):
            kwargs["on_server"](types.SimpleNamespace(should_exit=False))
            entered.set()

        self.install_server(run_server)
        self.module.start()
        self.assertTrue(entered.wait(1.0))
        old_thread = self.module._thread
        self.assertTrue(self.module.stop(timeout=1.0))
        entered.clear()
        self.module.start()
        self.assertTrue(entered.wait(1.0))
        self.assertIsNot(self.module._thread, old_thread)
        self.assertTrue(self.module.stop(timeout=1.0))

    def test_server_exit_during_http_work_retains_wrapper_and_blocks_new_hub(self):
        from config import RiskConfig
        from services import Services
        from tq.subscriber import SubscriptionManager
        from tqdiff.client import DiffClient

        mobile = load_android_module("mobile_api")
        entered = threading.Event()
        release = threading.Event()
        server_running = threading.Event()
        server_exited = threading.Event()
        clients = []
        services = []
        self.release_events.append(release)
        self.addCleanup(lambda: [client.close(timeout=1.0) for client in clients])
        self.addCleanup(release.set)

        def build(config, **kwargs):
            client = DiffClient("fake-account", "fake-password")

            async def session():
                def blocking_work():
                    entered.set()
                    release.wait()
                await asyncio.get_running_loop().run_in_executor(None, blocking_work)
                await asyncio.Event().wait()

            client._session = session
            real_close = client.close
            # 加速原 3 秒 join，保留最终后台 join(None) 和真实 executor 收尾。
            client.close = lambda timeout=3.0: real_close(timeout=None if timeout is None else 0.01)
            subscriptions = SubscriptionManager(client, object())
            service = Services(config, client, object(), subscriptions, mock.Mock(), types.SimpleNamespace())
            clients.append(client)
            services.append(service)
            return service

        class FakeServer:
            def __init__(self, config):
                self.config = config
                self.should_exit = False

            def run(self):
                async def lifecycle():
                    async with self.config.app.router.lifespan_context(self.config.app):
                        server_running.set()
                        while not self.should_exit:
                            await asyncio.sleep(0.001)
                    server_exited.set()
                asyncio.run(lifecycle())

        uvicorn = types.SimpleNamespace(Config=lambda app, **kwargs: types.SimpleNamespace(app=app), Server=FakeServer)
        self.stack.enter_context(mock.patch.dict(sys.modules, {"uvicorn": uvicorn}))
        self.stack.enter_context(mock.patch.object(mobile, "load_config", return_value=types.SimpleNamespace(risk=RiskConfig())))
        self.stack.enter_context(mock.patch.object(mobile, "configure_logging"))
        self.stack.enter_context(mock.patch.object(mobile, "build_services", side_effect=build))
        self.install_server(mobile.run_mobile_server)

        self.module.start()
        self.assertTrue(server_running.wait(1.0))
        self.assertTrue(entered.wait(1.0))
        wrapper = self.module._thread
        diff_thread = clients[0]._thread
        self.module.request_stop()
        self.assertTrue(server_exited.wait(1.0))
        self.assertTrue(diff_thread.is_alive())
        self.assertFalse(self.module.stop(timeout=0.0))
        for _ in range(3):
            self.module.start()
        self.assertIs(self.module._thread, wrapper)
        self.assertEqual(len(clients), 1)

        release.set()
        self.assertTrue(self.module.stop(timeout=1.0))
        self.assertIsNone(clients[0]._thread)
        self.assertIsNone(clients[0]._executor)
        self.assertIsNone(services[0].subscriptions._worker)
        server_running.clear()
        self.module.start()
        self.assertTrue(server_running.wait(1.0))
        self.assertEqual(len(clients), 2)
        self.assertFalse(diff_thread.is_alive())
        self.assertTrue(self.module.stop(timeout=1.0))


if __name__ == "__main__":
    unittest.main()

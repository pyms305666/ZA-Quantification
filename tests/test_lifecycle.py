"""订阅恢复与关闭故障测试：全部 Fake，不启动服务或访问账号。"""

from __future__ import annotations

import asyncio
import threading
import time
import types
import unittest
from unittest import mock

from api.websocket import ConnectionManager
from services import Services
from tq.client import TqClientError
from tq.instruments import InstrumentManager, EXCHANGES
from tq.subscriber import SubscriptionManager
from tqdiff.client import DiffClient
from tests.test_tq import FakeClient


def wait_until(test, predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while not predicate() and time.monotonic() < deadline:
        pause.wait(0.005)
    test.assertTrue(predicate(), "Fake 状态未在期限内完成")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.manager = SubscriptionManager(self.client, InstrumentManager(self.client))
        self.addCleanup(self.manager.close)

    def test_connected_callback_only_wakes_worker(self):
        self.client.connected = False
        self.manager.subscribe(["SHFE.rb2610"])
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        run = self.client.run_command
        command_threads = []

        def command(name, *args, **kwargs):
            command_threads.append((name, threading.current_thread().name))
            if name == "subscribe":
                entered.set()
                if not release.wait(1.0):
                    raise TqClientError("Fake send blocked")
            return run(name, *args, **kwargs)

        self.client.run_command = command
        self.client.connected = True
        self.manager.start()

        async def callback():
            self.manager.on_connected()
            # 同一事件循环还能调度下一轮，未同步等待阻塞中的 SDK 命令。
            await asyncio.sleep(0)
            return "returned"

        self.assertEqual(asyncio.run(asyncio.wait_for(callback(), 0.1)), "returned")
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(self.client.catalog_complete)
        self.assertIn(("get_instrument", "subscription-recovery"), command_threads)
        self.assertEqual(self.manager.subscribed(), [])
        release.set()
        wait_until(self, lambda: self.manager.subscribed() == ["SHFE.rb2610"])

    def test_temporary_bare_lookup_keeps_pending_until_recovery(self):
        self.client.connected = False
        self.manager.subscribe(["rb2610"])
        run = self.client.run_command
        failed = threading.Event()
        temporary = threading.Event()
        temporary.set()
        queries = []

        def command(name, *args, **kwargs):
            if name == "get_instrument" and temporary.is_set():
                queries.append(args[0])
                if len(queries) == len(EXCHANGES):
                    failed.set()
                raise TqClientError("Fake catalog unavailable")
            return run(name, *args, **kwargs)

        self.client.run_command = command
        self.client.connected = True
        self.manager.start()
        self.assertTrue(failed.wait(1.0))
        self.assertIn("rb2610", self.manager._pending)
        self.assertEqual(queries[:len(EXCHANGES)], [exchange + "." +
                         ("RB2610" if exchange in ("CZCE", "CFFEX") else "rb2610")
                         for exchange in EXCHANGES])
        temporary.clear()
        self.manager.on_connected()
        wait_until(self, lambda: self.manager.subscribed() == ["SHFE.rb2610"])
        self.assertEqual(self.manager._pending, {})

    def test_partial_queue_is_pending_until_real_send_succeeds(self):
        self.client.catalog_ready = False
        self.client.catalog_complete = False
        result = self.manager.subscribe(["SHFE.rb2610"])
        self.assertEqual(result["subscribed"], ["SHFE.rb2610"])
        self.assertIn("SHFE.rb2610", self.manager._pending)
        failed = threading.Event()
        succeed = threading.Event()
        run = self.client.run_command

        def command(name, *args, **kwargs):
            if name == "subscribe" and not succeed.is_set():
                failed.set()
                raise TqClientError("Fake send failed")
            return run(name, *args, **kwargs)

        self.client.run_command = command
        self.manager.start()
        self.assertTrue(failed.wait(1.0))
        self.assertIn("SHFE.rb2610", self.manager._pending)
        succeed.set()
        self.manager.on_connected()
        wait_until(self, lambda: not self.manager._pending)
        self.assertEqual(self.client.queued_subscriptions, ["SHFE.rb2610"])
        self.assertTrue(any(name == "subscribe" for name, _ in self.client.commands))

    def test_bare_pending_cancelled_offline_does_not_revive(self):
        self.client.connected = False
        self.manager.subscribe(["rb2610"])
        run = self.client.run_command

        def command(name, *args, **kwargs):
            if not self.client.connected:
                raise TqClientError("Fake disconnected")
            return run(name, *args, **kwargs)

        self.client.run_command = command
        self.manager.unsubscribe([" rb2610 "])
        self.assertEqual(self.manager._pending, {})
        self.client.connected = True
        self.manager.start()
        self.manager.on_connected()
        # 成功的新请求作为 worker 的屏障；取消前的裸代码不得恢复。
        self.assertEqual(self.manager.subscribe(["DCE.m2609"])["subscribed"], ["DCE.m2609"])
        self.assertEqual(self.manager.subscribed(), ["DCE.m2609"])
        self.assertNotIn("SHFE.rb2610", self.client.subscribed)

    def test_cancel_racing_successful_replay_does_not_commit(self):
        self.client.connected = False
        self.manager.subscribe(["rb2610"])
        run = self.client.run_command
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)

        def command(name, *args, **kwargs):
            if name == "subscribe":
                entered.set()
                release.wait(1.0)
            return run(name, *args, **kwargs)

        self.client.run_command = command
        self.client.connected = True
        self.manager.start()
        self.assertTrue(entered.wait(1.0))

        def cancel():
            try:
                self.manager.unsubscribe(["SHFE.rb2610"])
            finally:
                finished.set()

        cancel_thread = threading.Thread(target=cancel, daemon=True)
        cancel_thread.start()
        wait_until(self, lambda: bool(self.manager._canonical_cancel))
        release.set()
        self.assertTrue(finished.wait(1.0))
        cancel_thread.join(1.0)
        self.assertEqual(self.manager.subscribed(), [])
        self.assertEqual(self.manager._pending, {})
        self.assertEqual(self.client.subscribed, set())

    def test_unsubscribe_timeout_then_new_subscribe_really_sends(self):
        self.manager.subscribe(["SHFE.rb2610"])
        run = self.client.run_command

        def command(name, *args, **kwargs):
            if name == "unsubscribe":
                run(name, *args, **kwargs)
                raise TqClientError("Fake timeout after removing desired table")
            return run(name, *args, **kwargs)

        self.client.run_command = command
        self.assertEqual(self.manager.unsubscribe(["rb2610"])["missing"], ["rb2610"])
        self.assertEqual(self.manager.subscribed(), [])
        self.assertEqual(self.manager.subscribe(["SHFE.rb2610"])["subscribed"], ["SHFE.rb2610"])
        sends = [name for name, _ in self.client.commands if name == "subscribe"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(self.client.subscribed, {"SHFE.rb2610"})

    def test_late_old_alias_unsubscribe_keeps_new_idempotent_intent(self):
        self.manager.subscribe(["SHFE.rb2610"])
        run = self.client.run_command
        resolving = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)

        def command(name, *args, **kwargs):
            if name == "get_instrument" and threading.current_thread().name == "old-cancel":
                resolving.set()
                release.wait(1.0)
            return run(name, *args, **kwargs)

        self.client.run_command = command

        def cancel():
            try:
                self.manager.unsubscribe(["rb2610"])
            finally:
                finished.set()

        thread = threading.Thread(target=cancel, name="old-cancel", daemon=True)
        thread.start()
        self.assertTrue(resolving.wait(1.0))
        self.assertEqual(self.manager.subscribe(["SHFE.rb2610"])["skipped"], ["SHFE.rb2610"])
        release.set()
        self.assertTrue(finished.wait(1.0))
        thread.join(1.0)
        self.assertEqual(self.manager.subscribed(), ["SHFE.rb2610"])
        self.assertEqual(self.client.subscribed, {"SHFE.rb2610"})
        self.assertFalse(any(name == "unsubscribe" for name, _ in self.client.commands))

    def test_worker_start_close_is_idempotent_and_retains_busy_thread(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        self.manager._recover = lambda: (entered.set(), release.wait(1.0))
        self.manager.start()
        self.assertTrue(entered.wait(1.0))
        worker = self.manager._worker
        self.manager.start()
        self.assertIs(self.manager._worker, worker)
        self.manager.close(timeout=0.0)
        self.assertIs(self.manager._worker, worker)
        self.manager.start()
        self.assertIs(self.manager._worker, worker)
        release.set()
        self.manager.close(timeout=1.0)
        self.assertIsNone(self.manager._worker)
        self.manager.close(timeout=0.0)


class FakeLoopClient(DiffClient):
    def __init__(self):
        super().__init__("fake-account", "fake-password")
        self.fake_ready = threading.Event()
        self.session_exited = threading.Event()
        self.command_entered = threading.Event()
        self.command_exited = threading.Event()
        self.catalog_exited = threading.Event()
        self.socket_closed = threading.Event()

    async def _session(self):
        async def close():
            self.socket_closed.set()

        async def catalog():
            try:
                await asyncio.Event().wait()
            finally:
                self.catalog_exited.set()

        self._ws = types.SimpleNamespace(close=close)
        self._file_task = asyncio.create_task(catalog())
        self._set_connected(True)
        self.fake_ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.session_exited.set()

    async def _execute(self, command, args):
        self.command_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.command_exited.set()


class DiffLifecycleTests(unittest.TestCase):
    def start_client(self):
        client = FakeLoopClient()
        self.addCleanup(client.close)
        client.start()
        self.assertTrue(client.fake_ready.wait(1.0))
        return client

    def test_same_loop_sync_command_fails_without_self_wait(self):
        client = self.start_client()

        async def command():
            with self.assertRaisesRegex(TqClientError, "事件循环线程"):
                client.run_command("anything", timeout=60.0)
            return "returned"

        self.assertEqual(asyncio.run_coroutine_threadsafe(command(), client._loop).result(1.0), "returned")
        self.assertFalse(client.command_entered.is_set())

    def test_command_timeout_cancels_coroutine(self):
        client = self.start_client()
        with self.assertRaisesRegex(TqClientError, "命令超时"):
            client.run_command("anything", timeout=0.05)
        self.assertTrue(client.command_exited.wait(1.0))
        wait_until(self, lambda: not client._command_tasks)

    def test_close_cancels_idle_session_catalog_and_command(self):
        client = self.start_client()
        errors = []

        def command():
            try:
                client.run_command("anything", timeout=60.0)
            except TqClientError as error:
                errors.append(error)

        thread = threading.Thread(target=command, daemon=True)
        thread.start()
        self.assertTrue(client.command_entered.wait(1.0))
        client.close(timeout=1.0)
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(client.session_exited.is_set())
        self.assertTrue(client.command_exited.is_set())
        self.assertTrue(client.catalog_exited.is_set())
        self.assertTrue(client.socket_closed.is_set())
        self.assertFalse(client.connected)
        self.assertIsNone(client._thread)
        self.assertTrue(client._loop.is_closed())
        self.assertEqual(client._command_tasks, set())
        with self.assertRaisesRegex(TqClientError, "已关闭"):
            client.run_command("anything")
        with self.assertRaisesRegex(TqClientError, "已关闭"):
            client.queue_subscription("SHFE.rb2610")

    def test_close_timeout_preserves_thread_and_blocks_second_start(self):
        client = DiffClient("fake", "fake")
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(client.close)
        self.addCleanup(release.set)
        client._run_loop = lambda: (entered.set(), release.wait(1.0))
        client.start()
        self.assertTrue(entered.wait(1.0))
        thread = client._thread
        with self.assertLogs("gateway.tqdiff", level="WARNING"):
            client.close(timeout=0.0)
        self.assertIs(client._thread, thread)
        client.start()
        self.assertIs(client._thread, thread)
        release.set()
        client.close(timeout=1.0)
        self.assertIsNone(client._thread)
        client.close(timeout=0.0)

    def test_blocking_http_executor_keeps_stopping_thread_until_work_finishes(self):
        client = self.start_client()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def blocking_http():
            entered.set()
            release.wait(1.0)

        async def request():
            await client._loop.run_in_executor(None, blocking_http)

        future = asyncio.run_coroutine_threadsafe(request(), client._loop)
        self.assertTrue(entered.wait(1.0))
        thread = client._thread
        with self.assertLogs("gateway.tqdiff", level="WARNING"):
            client.close(timeout=0.01)
        self.assertTrue(future.cancelled())
        self.assertIs(client._thread, thread)
        self.assertTrue(thread.is_alive())
        client.start()
        self.assertIs(client._thread, thread)
        release.set()
        client.close(timeout=1.0)
        self.assertIsNone(client._thread)
        self.assertIsNone(client._executor)

    def test_last_unsubscribe_sends_empty_full_table(self):
        client = DiffClient("fake", "fake")
        client._subscribed.add("SHFE.rb2610")
        packs = []

        async def send(pack):
            packs.append(pack)

        client._send = send
        asyncio.run(client._unsubscribe("SHFE.rb2610"))
        self.assertEqual(packs, [{"aid": "subscribe_quote", "ins_list": ""}])

    def test_failed_send_after_local_add_is_really_resent(self):
        client = DiffClient("fake", "fake")
        packs = []

        async def send(pack):
            packs.append(pack)
            if len(packs) == 1:
                raise TqClientError("Fake first send failed")

        client._send = send

        async def exercise():
            with self.assertRaises(TqClientError):
                await client._subscribe("SHFE.rb2610")
            self.assertEqual(await client._subscribe("SHFE.rb2610"), "SHFE.rb2610")

        asyncio.run(exercise())
        self.assertEqual(len(packs), 2)
        self.assertEqual(packs[0], packs[1])

    def test_queued_sync_retries_with_one_managed_task(self):
        client = DiffClient("fake", "fake")
        client._set_connected(True)
        client.queue_subscription("SHFE.rb2610")
        packs = []
        real_sleep = asyncio.sleep

        async def send(pack):
            packs.append(pack)
            if len(packs) == 1:
                raise TqClientError("Fake send failed")

        async def fast_retry(delay):
            self.assertEqual(delay, 1)
            await real_sleep(0)

        client._send = send

        async def exercise():
            client._ensure_subscription_sync()
            task = client._subscription_sync_task
            client._ensure_subscription_sync()
            self.assertIs(client._subscription_sync_task, task)
            with mock.patch("tqdiff.client.asyncio.sleep", side_effect=fast_retry):
                await task
            await real_sleep(0)
            self.assertEqual(client._background_tasks, set())

        asyncio.run(exercise())
        self.assertEqual(len(packs), 2)

    def test_queue_change_during_send_resends_latest_full_table(self):
        client = DiffClient("fake", "fake")
        client._set_connected(True)
        client.queue_subscription("SHFE.rb2610")
        packs = []

        async def send(pack):
            packs.append(pack)
            if len(packs) == 1:
                client.queue_subscription("DCE.m2609")

        client._send = send
        asyncio.run(client._sync_queued_subscriptions())
        self.assertEqual([pack["ins_list"] for pack in packs],
                         ["SHFE.rb2610", "DCE.m2609,SHFE.rb2610"])

    def test_close_cancels_queued_sync_task(self):
        client = self.start_client()
        sending = threading.Event()
        cancelled = threading.Event()

        async def send(pack):
            sending.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        client._send = send
        client.queue_subscription("SHFE.rb2610")
        self.assertTrue(sending.wait(1.0))
        task = client._subscription_sync_task
        client.close(timeout=1.0)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.cancelled())
        self.assertEqual(client._background_tasks, set())


class ServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_close_orders_stop_and_supports_android_stub(self):
        for connections in (types.SimpleNamespace(), types.SimpleNamespace(close_all=mock.AsyncMock())):
            calls = []
            client = types.SimpleNamespace(start=lambda: calls.append("client.start"),
                                           close=lambda: calls.append("client.close"))
            subscriptions = types.SimpleNamespace(start=lambda: calls.append("worker.start"),
                       request_stop=lambda: calls.append("worker.request_stop"),
                       close=lambda: calls.append("worker.close"))
            service = Services(None, client, None, subscriptions, None, connections)
            service.loop = asyncio.get_running_loop()
            service.start()
            await service.close()
            self.assertEqual(calls, ["worker.start", "client.start", "worker.request_stop",
                                     "client.close", "worker.close"])
            self.assertIsNone(service.loop)
            if hasattr(connections, "close_all"):
                connections.close_all.assert_awaited_once_with()

    async def test_close_all_closes_every_socket_even_after_failure(self):
        manager = ConnectionManager()
        closed = []

        class Socket:
            async def accept(self):
                pass

            async def send_json(self, payload):
                pass

            async def close(self, code):
                closed.append(code)
                raise RuntimeError("Fake socket already closed")

        await manager.connect(Socket(), {"type": "hello"})
        await manager.connect(Socket(), {"type": "hello"})
        await manager.close_all()
        self.assertEqual(closed, [1001, 1001])
        self.assertEqual(manager.client_count(), 0)
        self.assertIsNotNone(manager.idle_seconds())


if __name__ == "__main__":
    unittest.main()

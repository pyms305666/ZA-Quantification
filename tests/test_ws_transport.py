"""传输验收：内存 Fake，无真实连接、账号或行情服务。"""
import asyncio
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from market.cache import QuoteCache
from services import Services
from ws_transport import OutboundConnection, QuoteIngress, TransportClosed, WebSocketHub


class Socket:
    def __init__(self):
        self.sent = []
        self.codes = []
        self.block = None
        self.entered = asyncio.Event()
        self.updated = asyncio.Event()

    async def accept(self):
        pass

    async def send_json(self, payload):
        if self.block is not None and payload['type'] == 'quote':
            self.entered.set()
            await self.block.wait()
        self.sent.append(dict(payload))
        self.updated.set()

    async def close(self, code):
        self.codes.append(code)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = WebSocketHub()

    async def asyncTearDown(self):
        await self.hub.close_all()

    async def test_hello_fifo_control_and_actual_send_timestamp(self):
        socket = Socket()
        await self.hub.connect(socket, {'type': 'hello'})
        self.hub.broadcast({'type': 'quote', 'data': {'last': 1}, 'ts': -1})
        with mock.patch('ws_transport.time.time', return_value=123):
            await self.hub.send_control(socket, {'type': 'pong'})
        self.assertEqual([x['type'] for x in socket.sent], ['hello', 'quote', 'pong'])
        self.assertEqual(socket.sent[1]['ts'], 123)

    async def test_slow_peer_does_not_delay_fast_peer_and_overflow_closes_1013(self):
        slow, fast = Socket(), Socket()
        slow.block = asyncio.Event()
        await self.hub.connect(slow, {'type': 'hello'})
        await self.hub.connect(fast, {'type': 'hello'})
        peer = self.hub._peers[slow]
        self.assertEqual(peer.queue.maxsize, 128)
        self.assertEqual(peer.send_timeout, 2.0)
        for index in range(140):
            fast.updated.clear()
            self.hub.broadcast({'type': 'quote', 'index': index})
            await asyncio.wait_for(fast.updated.wait(), 1)
        self.assertEqual(len(fast.sent), 141)
        await peer.close()
        self.assertEqual(slow.codes, [1013])
        self.assertTrue(peer.sender_task.done())
        self.assertTrue(peer.queue.empty())
        self.assertEqual(self.hub.client_count(), 1)

    async def test_send_timeout_notifies_waiting_control(self):
        socket = Socket()
        socket.block = asyncio.Event()
        await self.hub.connect(socket, {'type': 'hello'})
        peer = self.hub._peers[socket]
        peer.send_timeout = .01
        self.hub.broadcast({'type': 'quote'})
        await socket.entered.wait()
        with self.assertRaises(TransportClosed):
            await self.hub.send_control(socket, {'type': 'pong'})
        await peer.close()
        self.assertEqual(socket.codes, [1013])

    async def test_overflow_explicitly_fails_queued_control(self):
        socket = Socket()
        socket.block = asyncio.Event()
        await self.hub.connect(socket, {'type': 'hello'})
        peer = self.hub._peers[socket]
        peer.enqueue({'type': 'quote'})
        await socket.entered.wait()
        completion = peer.enqueue({'type': 'pong'}, control=True)
        for _ in range(127):
            peer.enqueue({'type': 'quote'})
        with self.assertRaises(TransportClosed):
            peer.enqueue({'type': 'quote'})
        with self.assertRaises(TransportClosed):
            await completion
        await peer.close()

    async def test_shutdown_interrupts_accept_and_cannot_register_late_handshake(self):
        entered = asyncio.Event()
        class Handshake(Socket):
            async def accept(self):
                entered.set()
                await asyncio.Event().wait()
        socket = Handshake()
        task = asyncio.create_task(self.hub.connect(socket, {'type': 'hello'}))
        await entered.wait()
        await self.hub.close_all()
        self.assertTrue(task.cancelled())
        self.assertEqual(socket.sent, [])
        self.assertEqual(socket.codes, [1001])
        self.assertEqual(self.hub.client_count(), 0)
        with self.assertRaises(TransportClosed):
            await self.hub.connect(Socket(), {'type': 'hello'})
        self.hub.reopen()
        await self.hub.connect(Socket(), {'type': 'hello'})
        self.assertEqual(self.hub.client_count(), 1)

    async def test_close_all_awaits_detached_close_task_and_repeated_close(self):
        closing, release = asyncio.Event(), asyncio.Event()
        class SlowClose(Socket):
            async def close(self, code):
                self.codes.append(code)
                closing.set()
                await release.wait()
        socket = SlowClose()
        await self.hub.connect(socket, {'type': 'hello'})
        peer = self.hub._peers[socket]
        for _ in range(129):
            try:
                peer.enqueue({'type': 'quote'})
            except TransportClosed:
                break
        await closing.wait()
        shutdown = asyncio.create_task(self.hub.close_all())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await shutdown
        await self.hub.close_all()
        self.assertTrue(peer._close_task.done())
        self.assertTrue(peer.sender_task.done())
        self.assertEqual(self.hub._retiring, set())
        self.assertEqual(socket.codes, [1013])


class ManualLoop:
    def __init__(self):
        self.callbacks = []

    def call_soon_threadsafe(self, callback):
        self.callbacks.append(callback)

    call_soon = call_soon_threadsafe

    def drain(self):
        while self.callbacks:
            self.callbacks.pop(0)()


class IngressTests(unittest.TestCase):
    def test_burst_is_bounded_single_wake_and_drops_only_old_pending_push(self):
        loop, batches = ManualLoop(), []
        ingress = QuoteIngress(loop, batches.append)
        threads = [threading.Thread(target=lambda: [ingress.submit(i) for i in range(1000)])
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(loop.callbacks), 1)
        self.assertEqual(ingress.wakeup_count, 1)
        self.assertEqual(ingress.pending_count, 1024)
        self.assertEqual(ingress.dropped_count, 2976)
        with self.assertLogs('gateway.ws', level='WARNING'):
            loop.drain()
        self.assertEqual([len(x) for x in batches], [256] * 4)
        self.assertEqual(ingress.pending_count, 0)
        ingress.close()
        self.assertFalse(ingress.submit('closed'))

    def test_every_quote_updates_original_cache_and_stats_despite_overflow(self):
        loop, pushed = ManualLoop(), []
        service = Services(None, None, None, None, QuoteCache(),
                           SimpleNamespace(broadcast=pushed.append))
        service.loop = loop
        service.attach_ingress()
        for index in range(2000):
            quote = SimpleNamespace(symbol='SHFE.rb2610', to_dict=lambda i=index: {'last': i})
            service.on_quote_change(quote)
        self.assertEqual(service.quote_recv_total, 2000)
        self.assertEqual(service.cache.get('SHFE.rb2610').to_dict(), {'last': 1999})
        self.assertEqual(service.broadcast_ingress.dropped_count, 976)
        with self.assertLogs('gateway.ws', level='WARNING'):
            loop.drain()
        self.assertEqual(len(pushed), 1024)
        self.assertEqual(pushed[0]['data']['last'], 976)
        self.assertEqual(pushed[-1]['data']['last'], 1999)

    def test_closed_loop_discards_push_without_changing_cache(self):
        class ClosedLoop:
            def call_soon_threadsafe(self, callback):
                raise RuntimeError('closed')
        ingress = QuoteIngress(ClosedLoop(), lambda batch: None)
        self.assertFalse(ingress.submit('quote'))
        self.assertTrue(ingress.closed)
        self.assertEqual(ingress.pending_count, 0)


if __name__ == '__main__':
    unittest.main()

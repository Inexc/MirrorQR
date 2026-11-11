"""Local integration checks; no synthetic video and no OS capture claim."""
import asyncio
import json
from pathlib import Path
import sys
import socket
import threading
import unittest
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aiohttp import ClientSession
from src.backend.service import BackendService
from src.capture.factory import create_track

ROOT = Path(__file__).resolve().parents[2]


def token(snapshot):
    return parse_qs(urlsplit(snapshot['pairingUrl']).fragment)['token'][0]


def require_free_port(port=8765):
    # A preflight gives a clear failure without stopping another process.
    # The real bind still checks for races after this socket is closed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as exc:
            raise AssertionError(
                f'Integration precondition failed: cannot bind 127.0.0.1:{port}; '
                f'check whether another process owns the port. {exc}'
            ) from exc


class PortPreconditionChecks(unittest.TestCase):
    def test_occupied_port_has_clear_failure(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            with self.assertRaisesRegex(AssertionError, f'precondition failed: cannot bind 127.0.0.1:{port}'):
                require_free_port(port)
        require_free_port(port)


class RuntimeChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if self._testMethodName != 'test_matrix_and_runtime':
            require_free_port()

    async def test_routes_busy_cancel_restart_and_capture_failure(self):
        started = threading.Event()
        cleaned = threading.Event()

        async def picker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.1)
                cleaned.set()

        service = BackendService(picker, ROOT / 'web')
        snap = await asyncio.to_thread(service.start, '127.0.0.1', 8765)
        self.assertEqual(snap['status'], 'waiting')
        original = token(snap)
        base = 'http://127.0.0.1:8765'
        try:
            async with ClientSession() as client:
                for route, content_type in [('/phone', 'text/html'),
                                            ('/assets/phone.css', 'text/css'),
                                            ('/assets/phone.js', 'application/javascript')]:
                    async with client.get(base + route) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.content_type, content_type)
                        await response.read()
                a = await client.ws_connect(base + '/ws')
                await a.send_json({'type': 'auth', 'token': original})
                self.assertEqual((await a.receive_json())['type'], 'ready')
                await a.send_json({'type': 'offer', 'sdp': 'selection-pending'})
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                # A new unredeemed token proves busy does not consume it.
                fresh = service._session.issue_token()
                before = service.snapshot()
                handler = service._server._handler_task
                for _ in range(2):
                    async with client.ws_connect(base + '/ws') as b:
                        await b.send_json({'type': 'auth', 'token': fresh.value})
                        self.assertEqual((await b.receive_json())['code'], 'busy')
                    self.assertEqual(service.snapshot(), before)
                    self.assertIs(service._session.current_token(), fresh)
                    self.assertIs(service._server._handler_task, handler)
                    self.assertFalse(a.closed)
                await asyncio.wait_for(asyncio.to_thread(service.stop), 4)
                self.assertTrue(cleaned.is_set(), 'picker cancellation cleanup must finish')
                self.assertTrue(handler.done())
                await a.close()
            snap2 = await asyncio.to_thread(service.start, '127.0.0.1', 8765)
            self.assertEqual(snap2['status'], 'waiting')
            self.assertNotEqual(token(snap2), original)
        finally:
            await asyncio.to_thread(service.stop)

        # Real platform factory on Windows: produces real capture track.
        if sys.platform == 'win32':
            from aiortc import RTCPeerConnection, RTCConfiguration
            service = BackendService(create_track, ROOT / 'web')
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            peer.addTransceiver('video', direction='recvonly')
            snap = await asyncio.to_thread(service.start, '127.0.0.1', 8765)
            try:
                await peer.setLocalDescription(await peer.createOffer())
                async with ClientSession() as client:
                    async with client.ws_connect(base + '/ws') as ws:
                        await ws.send_json({'type': 'auth', 'token': token(snap)})
                        self.assertEqual((await ws.receive_json())['type'], 'ready')
                        await ws.send_json({'type': 'offer', 'sdp': peer.localDescription.sdp})
                        ans = await ws.receive_json()
                        self.assertEqual(ans['type'], 'answer')
            finally:
                await peer.close()
                await asyncio.to_thread(service.stop)

    async def test_real_peer_cleanup_before_loop_shutdown(self):
        from aiortc import VideoStreamTrack, RTCPeerConnection, RTCConfiguration
        events = []

        class NoFramesTrack(VideoStreamTrack):
            async def recv(self):
                await asyncio.Event().wait()

            def stop(self):
                if self.readyState == 'ended':
                    return
                super().stop()
                events.append('track.stop')
                self.cleanup_task = asyncio.create_task(self.cleanup())

            async def cleanup(self):
                await asyncio.sleep(0.1)
                events.append('cleanup.finished')

        async def factory():
            return NoFramesTrack()

        service = BackendService(factory, ROOT / 'web')
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        peer.addTransceiver('video', direction='recvonly')
        snap = await asyncio.to_thread(service.start, '127.0.0.1', 8765)
        loop = service._loop
        try:
            await peer.setLocalDescription(await peer.createOffer())
            async with ClientSession() as client:
                async with client.ws_connect('http://127.0.0.1:8765/ws') as ws:
                    await ws.send_json({'type': 'auth', 'token': token(snap)})
                    self.assertEqual((await ws.receive_json())['type'], 'ready')
                    await ws.send_json({'type': 'offer', 'sdp': peer.localDescription.sdp})
                    self.assertEqual((await ws.receive_json())['type'], 'answer')
                    await asyncio.wait_for(asyncio.to_thread(service.stop), 4)
                    self.assertEqual(events, ['track.stop', 'cleanup.finished'])
                    self.assertTrue(loop.is_closed())
        finally:
            await peer.close()
            await asyncio.to_thread(service.stop)

    def test_matrix_and_runtime(self):
        import importlib.metadata as metadata
        self.assertGreaterEqual(sys.version_info[:2], (3, 13))
        for line in (ROOT / 'requirements.txt').read_text().splitlines():
            if '==' in line and not line.startswith('#') and ';' not in line:
                name, version = line.split('==')
                self.assertEqual(metadata.version(name), version)
        matrix = json.loads((ROOT / 'docs/qa/matrix.json').read_text(encoding='utf-8'))
        self.assertEqual(matrix['status'], 'not_run')
        self.assertLessEqual(len(matrix['cases']), 8)
        acceptance = (ROOT / 'docs/qa/acceptance.md').read_text(encoding='utf-8')
        for case in matrix['cases']:
            self.assertEqual(case['status'], 'not_run')
            self.assertIn(case['id'], acceptance)
            for key in ('preconditions', 'steps', 'expected'):
                self.assertTrue(case[key])


if __name__ == '__main__':
    unittest.main(verbosity=2)

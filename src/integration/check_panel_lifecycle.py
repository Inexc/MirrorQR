"""Panel concurrency and real aiohttp graceful-exit/rebind checks; no capture."""
import asyncio
from pathlib import Path
import socket
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aiohttp import ClientSession, web
import main
from src.integration.check_web_panel import FakeBackendService, Panel, LAN_ADDRESS


async def concurrency():
    entered, release = threading.Event(), threading.Event()

    class SlowService(FakeBackendService):
        def start(self, *args):
            entered.set()
            assert release.wait(5)
            return super().start(*args)

    service = SlowService()
    async with Panel(service) as panel, ClientSession() as client:
        first = asyncio.create_task(panel.post_json(client, '/api/desktop/start', {'address': LAN_ADDRESS}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            status, state, _ = await asyncio.wait_for(panel.get_json(client, '/api/desktop/state'), 1)
            assert status == 200 and state['status'] == 'starting'
            for route, body in [('/api/desktop/start', {'address': LAN_ADDRESS}), ('/api/desktop/stop', {})]:
                status, _ = await panel.post_json(client, route, body)
                assert status == 409
        finally:
            release.set()
            await first
        assert len(service.start_calls) == 1
        await panel.post_json(client, '/api/desktop/stop', {})
    print('PASS concurrent controls and responsive state')


def graceful_rebind():
    # Use an anonymous port so this test does not stop the user's live panel.
    class Runner:
        @staticmethod
        def run_app(factory, **kwargs):
            async def app_factory():
                app = await factory
                async def startup(app):
                    def exit_cleanly():
                        raise web.GracefulExit()
                    asyncio.get_running_loop().call_later(0.1, exit_cleanly)
                app.on_startup.append(startup)
                return app
            web.run_app(app_factory(), host='127.0.0.1', port=port, print=None)

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    for _ in range(2):
        service = FakeBackendService()
        assert main._run_web(service, Path('web'), Runner, __import__('src.backend.web_panel', fromlist=['create_panel_app']).create_panel_app) == 0
        assert service.stop_calls == 1
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    print('PASS real aiohttp graceful exit, service.stop and repeat bind (anonymous port)')


if __name__ == '__main__':
    asyncio.run(concurrency())
    graceful_rebind()

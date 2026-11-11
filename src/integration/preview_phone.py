"""Loopback-only phone preview with the real Windows capture-refusal path."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.backend.signaling import SignalingServer
from src.backend.session import SessionManager, Token
from src.capture.factory import create_track
import time
from aiohttp import web

class PreviewServer(SignalingServer):
    async def _handle_phone(self, request):
        if request.query.get('fixture'):
            root = Path(__file__).resolve().parents[2]
            html = (root / 'web/phone.html').read_text(encoding='utf-8')
            fixture = (root / 'src/integration/phone_fixture.js').read_text(encoding='utf-8')
            html = html.replace('<script src=', '<script>' + fixture + '</script><script src=')
            return web.Response(text=html, content_type='text/html')
        return await super()._handle_phone(request)

async def main():
    session = SessionManager()
    # Public, local-only test fixture, never a genuine LAN pairing credential.
    session._token = Token('local-preview-only', time.time() + 3600)
    server = PreviewServer(session, create_track, Path(__file__).resolve().parents[2] / 'web')
    await server.start('127.0.0.1', 0)
    port = server._site._server.sockets[0].getsockname()[1]
    print(f'http://127.0.0.1:{port}/phone', flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()

if __name__ == '__main__':
    asyncio.run(main())

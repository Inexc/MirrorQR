"""Самопроверка backend-слоя без запуска сервера и без aiortc.

Запуск: python src/backend/_selfcheck.py
Проверяет: TTL/атомарное погашение токена, слот, snapshot, защиту путей
web_root и коды ошибок протокола. Сеть и WebRTC не поднимаются.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backend import session as sess  # noqa: E402
from src.backend import signaling as sig  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def test_tokens() -> None:
    sm = sess.SessionManager(ttl=120)
    sm.set_server("192.168.1.5", 8765)
    tok = sm.issue_token()
    check("token length", len(tok.value) >= 32, tok.value)
    check(
        "pairing url",
        sm.pairing_url("192.168.1.5", 8765)
        == f"http://192.168.1.5:8765/phone#token={tok.value}",
    )

    # атомарное погашение: второй redeem с тем же токеном — invalid
    sm.redeem(tok.value)
    try:
        sm.redeem(tok.value)
        check("token single use", False)
    except sess.TokenRejected as exc:
        check("token single use", exc.code == "invalid_token", exc.code)

    # истёкший токен
    sm2 = sess.SessionManager(ttl=0.05)
    sm2.set_server("10.0.0.2", 8765)
    t2 = sm2.issue_token()
    time.sleep(0.1)
    try:
        sm2.redeem(t2.value)
        check("expired token", False)
    except sess.TokenRejected as exc:
        check("expired token", exc.code == "token_expired", exc.code)
    check("expired hides url", sm2.snapshot()["pairingUrl"] is None)

    # rotate гасит старый токен
    sm3 = sess.SessionManager()
    sm3.set_server("10.0.0.3", 8765)
    a = sm3.issue_token()
    b = sm3.issue_token()
    try:
        sm3.redeem(a.value)
        check("rotate invalidates", False)
    except sess.TokenRejected as exc:
        check("rotate invalidates", exc.code == "invalid_token")
    sm3.redeem(b.value)

    # слот
    sm4 = sess.SessionManager()
    check("slot first", sm4.try_acquire_client() is True)
    check("slot busy", sm4.try_acquire_client() is False)
    sm4.release_client()
    check("slot released", sm4.try_acquire_client() is True)

    # snapshot контракт
    snap = sm4.snapshot()
    keys = {
        "status",
        "host",
        "port",
        "pairingUrl",
        "tokenExpiresAt",
        "clientConnected",
        "error",
    }
    check("snapshot keys", set(snap) == keys, str(set(snap) ^ keys))
    check(
        "snapshot types",
        isinstance(snap["port"], int) and isinstance(snap["clientConnected"], bool),
    )


def test_paths() -> None:
    from aiohttp import web as aio_web

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "phone.html").write_text("<html></html>", encoding="utf-8")
        (root / "phone.css").write_text("body{}", encoding="utf-8")
        (root / "phone.js").write_text("//", encoding="utf-8")
        (root / "secret.txt").write_text("nope", encoding="utf-8")

        p = sig._safe_web_path(root, "phone.html")
        check("safe path ok", p.is_file())

        for bad in ("../secret.txt", "..\\secret.txt", "sub/../phone.html", "secret.txt"):
            try:
                sig._safe_web_path(root, bad)
                check(f"blocked {bad}", False)
            except aio_web.HTTPNotFound:
                check(f"blocked {bad}", True)
            except Exception as exc:  # noqa: BLE001
                check(f"blocked {bad}", False, type(exc).__name__)

        # симлинк наружу, если ОС позволяет
        outside = Path(tempfile.mkdtemp()) / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = root / "phone2.html"
        try:
            os.symlink(outside, link)
            try:
                sig._safe_web_path(root, "phone2.html")
                check("symlink escape blocked", False)
            except Exception:  # noqa: BLE001
                check("symlink escape blocked", True)
        except OSError:
            check("symlink escape blocked (skipped, no privilege)", True)


class FakeWS:
    """Минимальный двойник aiohttp WebSocketResponse для _handle_ws.

    ``prepare`` определён на самом двойнике, поэтому реальный
    ``WebSocketResponse._pre_start`` не вызывается и ``_loop`` не нужен.
    """

    def __init__(self, messages: list[str], **kwargs: object) -> None:
        self._messages = list(messages)
        self.sent: list[dict] = []
        self.closed = False
        self.kwargs = kwargs

    async def prepare(self, request) -> None:  # noqa: ANN001
        return None

    async def receive(self):  # noqa: ANN202
        from aiohttp import WSMsgType

        class Msg:
            def __init__(self, data: str) -> None:
                self.type = WSMsgType.TEXT
                self.data = data

        if not self._messages:
            # Как настоящий сокет: новых сообщений нет — ждём, пока
            # сработает таймаут вызывающего кода.
            await asyncio.sleep(3600)
        return Msg(self._messages.pop(0))

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, *a, **k) -> None:  # noqa: ANN002
        self.closed = True


async def test_ws_flow() -> None:
    sm = sess.SessionManager()
    sm.set_server("10.0.0.9", 8765)
    token = sm.issue_token()

    async def never_factory():  # noqa: ANN202
        raise AssertionError("track_factory не должен вызываться до offer")

    server = sig.SignalingServer(sm, never_factory, Path("."))

    created: list[FakeWS] = []

    def fake_factory(messages: list[str], **kwargs: object) -> FakeWS:
        ws = FakeWS(messages, **kwargs)
        created.append(ws)
        return ws

    def install(messages: list[str]) -> FakeWS:
        ws = fake_factory(messages)
        server._ws_factory = lambda **kw: ws  # noqa: ARG005
        return ws

    # 1) busy: слот занят, токен не должен сгореть
    sm.try_acquire_client()
    ws = install(['{"type":"auth","token":"%s"}' % token.value])
    await server._handle_ws(None)  # type: ignore[arg-type]
    check("busy sent", bool(ws.sent) and ws.sent[0].get("code") == "busy", str(ws.sent))
    check("busy keeps token", sm.current_token() is not None)
    sm.release_client()

    # 2) invalid token
    ws = install(['{"type":"auth","token":"nope"}'])
    await server._handle_ws(None)  # type: ignore[arg-type]
    check(
        "invalid token",
        bool(ws.sent) and ws.sent[0].get("code") == "invalid_token",
        str(ws.sent),
    )

    # 3) auth timeout
    ws = install([])
    orig_auth = sig.AUTH_TIMEOUT_SECONDS
    sig.AUTH_TIMEOUT_SECONDS = 0.05
    try:
        await server._handle_ws(None)  # type: ignore[arg-type]
    finally:
        sig.AUTH_TIMEOUT_SECONDS = orig_auth
    check(
        "auth timeout",
        bool(ws.sent) and ws.sent[0].get("code") == "auth_timeout",
        str(ws.sent),
    )

    # 4) нет offer -> protocol_error, слот освобождён, токен погашен
    ws = install(['{"type":"auth","token":"%s"}' % token.value])
    orig_rtc = sig.WEBRTC_TIMEOUT_SECONDS
    sig.WEBRTC_TIMEOUT_SECONDS = 0.05
    try:
        await server._handle_ws(None)  # type: ignore[arg-type]
    finally:
        sig.WEBRTC_TIMEOUT_SECONDS = orig_rtc
    check(
        "ready then offer timeout",
        bool(ws.sent)
        and ws.sent[0].get("type") == "ready"
        and ws.sent[-1].get("code") == "protocol_error",
        str(ws.sent),
    )
    check("slot released after fail", sm.client_connected is False)
    check("token consumed", sm.current_token() is None)


def main() -> int:
    test_tokens()
    test_paths()
    asyncio.run(test_ws_flow())
    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

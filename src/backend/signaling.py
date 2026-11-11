"""HTTP-раздача трёх файлов и один WebSocket-сеанс сигналинга.

Слой держит только транспорт: aiohttp для HTTP/WS и aiortc для WebRTC.
Никакого захвата экрана здесь нет — трек приходит из ``track_factory``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiohttp import WSMsgType, web

from .session import (
    AUTH_TIMEOUT_SECONDS,
    STATUS_CONNECTING,
    STATUS_ERROR,
    STATUS_STREAMING,
    STATUS_WAITING,
    WEBRTC_TIMEOUT_SECONDS,
    SessionManager,
    TokenRejected,
)

log = logging.getLogger(__name__)

TrackFactory = Callable[[], Awaitable[Any]]

# Разрешённые файлы в web_root: только эти три маршрута.
ALLOWED_FILES = frozenset({"phone.html", "phone.css", "phone.js"})

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

MAX_SDP_LENGTH = 512 * 1024

#: Сколько ждать завершения WS-обработчика (в нём освобождается захват) при
#: остановке сервера. Не протокольный таймаут: только граница ожидания.
HANDLER_STOP_TIMEOUT = 10.0


class SignalingError(Exception):
    """Ошибка протокола с безопасным кодом и сообщением."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe_web_path(web_root: Path, name: str) -> Path:
    """Возвращает путь к файлу внутри web_root или бросает HTTPNotFound.

    Разрешены только имена из ``ALLOWED_FILES``: имя не строится из URL,
    поэтому обход каталога через ``..``/симлинк невозможен. Дополнительно
    проверяем, что результирующий путь лежит внутри web_root.
    """
    if name not in ALLOWED_FILES:
        raise web.HTTPNotFound()
    root = web_root.resolve()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise web.HTTPNotFound() from None
    if not candidate.is_file():
        raise web.HTTPNotFound()
    return candidate


class SignalingServer:
    """HTTP + WebSocket сервер для одной телефонной сессии."""

    def __init__(
        self,
        session: SessionManager,
        track_factory: TrackFactory,
        web_root: Path,
    ) -> None:
        self.session = session
        self.track_factory = track_factory
        self.web_root = Path(web_root)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._active_ws: Optional[web.WebSocketResponse] = None
        self._handler_task: Optional[asyncio.Task] = None
        self._closing = False
        self._capture_task: Optional[asyncio.Task] = None
        # Точка подмены для самопроверки; в норме — настоящий WebSocketResponse.
        self._ws_factory: Callable[..., Any] = web.WebSocketResponse

    # --- жизненный цикл -------------------------------------------------
    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_get("/phone", self._handle_phone)
        app.router.add_get("/assets/phone.css", self._handle_css)
        app.router.add_get("/assets/phone.js", self._handle_js)
        app.router.add_get("/ws", self._handle_ws)
        self._app = app
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

    async def stop(self) -> None:
        self._closing = True
        # Cancel only source selection, never the handler's resource cleanup.
        capture = self._capture_task
        if capture is not None and not capture.done():
            capture.cancel()
        ws = self._active_ws
        if ws is not None and not ws.closed:
            try:
                await self._send_ended(ws, 'stopped')
                await ws.close(code=1001, message=b"server shutdown")
            except Exception:  # noqa: BLE001 - закрытие не должно мешать
                pass
        self._active_ws = None
        # Дожидаемся обработчика WS до cleanup: именно он в finally вызывает
        # peer.close(), track.stop() и ждёт cleanup_task портала. Задачу НЕ
        # отменяем: отмена прервала бы await в finally и оборвала бы
        # освобождение захвата. Закрытый сокет сам завершает цикл чтения.
        task = self._handler_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=HANDLER_STOP_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("обработчик WS не завершился за %.1f с", HANDLER_STOP_TIMEOUT)
            except Exception:  # noqa: BLE001 - ошибка обработчика не мешает остановке
                pass
        self._handler_task = None
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None

    # --- HTTP -----------------------------------------------------------
    async def _handle_phone(self, request: web.Request) -> web.StreamResponse:
        path = _safe_web_path(self.web_root, "phone.html")
        return web.FileResponse(path, headers={"Content-Type": CONTENT_TYPES[".html"]})

    async def _handle_css(self, request: web.Request) -> web.StreamResponse:
        path = _safe_web_path(self.web_root, "phone.css")
        return web.FileResponse(path, headers={"Content-Type": CONTENT_TYPES[".css"]})

    async def _handle_js(self, request: web.Request) -> web.StreamResponse:
        path = _safe_web_path(self.web_root, "phone.js")
        return web.FileResponse(path, headers={"Content-Type": CONTENT_TYPES[".js"]})

    # --- WebSocket ------------------------------------------------------
    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        # Задача-владелец слота: stop() ждёт именно её, чтобы очистка
        # peer/track/portal завершилась до закрытия loop. Второй (busy)
        # обработчик не должен перезаписывать эту ссылку.
        try:
            current = asyncio.current_task()
        except RuntimeError:  # нет running loop (двойник в самопроверке)
            current = None
        ws = self._ws_factory(heartbeat=20.0, max_msg_size=MAX_SDP_LENGTH)
        await ws.prepare(request)
        peer: Optional[Any] = None
        track: Optional[Any] = None
        webrtc_timer: Optional[asyncio.Task] = None
        slot_acquired = False
        try:
            raw_token = await self._wait_for_auth(ws)
            # Слот проверяем до погашения токена: второй клиент получает busy,
            # не сжигая новый токен и не прерывая первого.
            if not self.session.try_acquire_client():
                await self._send_error(ws, "busy", "К компьютеру уже подключён телефон.")
                return ws
            slot_acquired = True
            # Только владелец слота регистрируется как задача для stop().
            self._handler_task = current
            self.session.redeem(raw_token)
            self._active_ws = ws
            self.session.set_status(STATUS_CONNECTING)
            session_id = os.urandom(8).hex()
            await self._send_json(ws, {"type": "ready", "sessionId": session_id})

            offer = await self._wait_for_offer(ws)
            sdp = offer.get("sdp")
            if not isinstance(sdp, str) or not sdp:
                raise SignalingError("protocol_error", "Некорректный offer.")

            try:
                self._capture_task = asyncio.create_task(self.track_factory())
                track = await asyncio.wait_for(self._capture_task, timeout=125.0)
            except Exception:  # noqa: BLE001 - детали не уходят клиенту
                log.exception("track_factory failed")
                self.session.set_status(STATUS_ERROR, "Захват экрана недоступен.")
                await self._send_error(ws, "capture_unavailable", "Не удалось начать захват экрана.")
                await self._send_ended(ws, "capture_failed")
                return ws
            finally:
                self._capture_task = None

            from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
            from aiortc.codecs import h264, vpx

            # Высокий битрейт для чёткого текста и плавного движения рабочего стола в LAN
            vpx.DEFAULT_BITRATE = 4_000_000
            vpx.MAX_BITRATE = 12_000_000
            h264.DEFAULT_BITRATE = 4_000_000
            h264.MAX_BITRATE = 12_000_000

            peer = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
            peer.addTrack(track)

            @peer.on("connectionstatechange")
            async def _on_state() -> None:  # noqa: ANN202
                state = peer.connectionState
                if state == "connected":
                    self.session.set_status(STATUS_STREAMING)
                elif state in ("failed", "closed"):
                    self.session.set_status(STATUS_WAITING)
                    await self._close_ws(ws)

            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            # non-trickle ICE: ждём завершения сбора кандидатов.
            gathered = await self._wait_ice_gathering(peer, WEBRTC_TIMEOUT_SECONDS)
            if not gathered:
                raise SignalingError("protocol_error", "Не удалось завершить установку соединения.")
            local = peer.localDescription
            if local is None or local.sdp is None:
                raise SignalingError("protocol_error", "Не удалось завершить установку соединения.")

            import re
            ans_sdp = re.sub(
                r"(m=video[^\r\n]+(?:\r\n|\n))",
                r"\g<1>b=AS:8000\r\nb=TIAS:8000000\r\n",
                local.sdp,
            )
            await self._send_json(ws, {"type": "answer", "sdp": ans_sdp})

            webrtc_timer = asyncio.create_task(self._webrtc_timeout(ws, peer))

            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type == WSMsgType.ERROR:
                        break
                    continue
                try:
                    data = json.loads(msg.data)
                except (TypeError, ValueError):
                    await self._send_error(ws, "protocol_error", "Некорректное сообщение.")
                    continue
                if not isinstance(data, dict):
                    await self._send_error(ws, "protocol_error", "Некорректное сообщение.")
                    continue
                mtype = data.get("type")
                if mtype == "stop":
                    await self._send_ended(ws, "stopped")
                    break
                if mtype == "ping":
                    await self._send_json(ws, {"type": "pong"})
                    continue
                if mtype == "input":
                    from src.backend.remote_input import handle_input

                    handle_input(data)
                    continue
                if mtype == "candidate":
                    # trickle ICE не поддерживается: кандидаты только в SDP.
                    await self._send_error(ws, "protocol_error", "Отправка кандидатов не поддерживается.")
                    continue
                await self._send_error(ws, "protocol_error", "Неизвестное сообщение.")
        except TokenRejected as exc:
            await self._send_error(ws, exc.code, exc.message)
        except SignalingError as exc:
            await self._send_error(ws, exc.code, exc.message)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("signaling failure")
            await self._send_error(ws, "protocol_error", "Внутренняя ошибка сигналинга.")
        finally:
            if webrtc_timer is not None:
                webrtc_timer.cancel()
            if peer is not None:
                try:
                    await peer.close()
                except Exception:  # noqa: BLE001
                    pass
            if track is not None:
                try:
                    track.stop()
                    cleanup = getattr(track, 'cleanup_task', None)
                    if cleanup is not None:
                        await asyncio.wait_for(asyncio.shield(cleanup), timeout=5.0)
                except asyncio.TimeoutError:
                    log.warning("очистка захвата не завершилась за 5 с")
                except Exception:  # noqa: BLE001
                    log.exception("ошибка освобождения захвата")
            if self._active_ws is ws:
                self._active_ws = None
            if slot_acquired and self._handler_task is current:
                self._handler_task = None
            if slot_acquired:
                self.session.release_client()
            if slot_acquired and not self._closing and self.session.snapshot()['status'] != STATUS_ERROR:
                self.session.set_status(STATUS_WAITING)
            await self._close_ws(ws)
        return ws

    # --- шаги handshake -------------------------------------------------
    async def _wait_for_auth(self, ws: web.WebSocketResponse) -> str:
        """Читает первое сообщение и возвращает сырой токен (без погашения)."""
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=AUTH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise SignalingError("auth_timeout", "Истекло время ожидания авторизации.") from None
        if msg.type != WSMsgType.TEXT:
            raise SignalingError("protocol_error", "Ожидалась авторизация.")
        try:
            data = json.loads(msg.data)
        except (TypeError, ValueError):
            raise SignalingError("protocol_error", "Некорректное сообщение авторизации.") from None
        if not isinstance(data, dict) or data.get("type") != "auth":
            raise SignalingError("protocol_error", "Ожидалась авторизация.")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise SignalingError("invalid_token", "Неверный токен подключения.")
        return token

    async def _wait_for_offer(self, ws: web.WebSocketResponse) -> dict:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=WEBRTC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise SignalingError("protocol_error", "Не получен offer.") from None
        if msg.type != WSMsgType.TEXT:
            raise SignalingError("protocol_error", "Ожидался offer.")
        try:
            data = json.loads(msg.data)
        except (TypeError, ValueError):
            raise SignalingError("protocol_error", "Некорректный offer.") from None
        if not isinstance(data, dict) or data.get("type") != "offer":
            raise SignalingError("protocol_error", "Ожидался offer.")
        return data

    async def _wait_ice_gathering(self, peer: Any, timeout: float) -> bool:
        if peer.iceGatheringState == "complete":
            return True
        done = asyncio.Event()

        @peer.on("icegatheringstatechange")
        def _on_state() -> None:  # noqa: ANN202
            if peer.iceGatheringState == "complete":
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _webrtc_timeout(self, ws: web.WebSocketResponse, peer: Any) -> None:
        try:
            await asyncio.sleep(WEBRTC_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        if peer.connectionState not in ("connected", "completed"):
            await self._send_ended(ws, "disconnected")
            await self._close_ws(ws)

    # --- отправка -------------------------------------------------------
    async def _send_json(self, ws: web.WebSocketResponse, payload: dict) -> None:
        if ws.closed:
            return
        try:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))
        except (ConnectionResetError, RuntimeError):
            pass

    async def _send_error(self, ws: web.WebSocketResponse, code: str, message: str) -> None:
        await self._send_json(ws, {"type": "error", "code": code, "message": message})

    async def _send_ended(self, ws: web.WebSocketResponse, reason: str) -> None:
        await self._send_json(ws, {"type": "ended", "reason": reason})

    async def _close_ws(self, ws: web.WebSocketResponse) -> None:
        if ws.closed:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

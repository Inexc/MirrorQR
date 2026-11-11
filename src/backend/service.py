"""Публичный фасад backend: сетевой сервер в отдельном потоке.

``BackendService`` — единственная точка, с которой работает desktop-слой.
Все методы синхронные и потокобезопасные: внутри держится собственный
asyncio-loop, который живёт в отдельном потоке. Вызовы из Qt-потока не
блокируются на сетевых операциях дольше, чем нужно на передачу управления
в loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .session import (
    STATUS_ERROR,
    STATUS_STOPPED,
    STATUS_WAITING,
    SessionManager,
)
from .signaling import SignalingServer

log = logging.getLogger(__name__)

TrackFactory = Callable[[], Awaitable[Any]]

DEFAULT_PORT = 8765
LOOP_STOP_TIMEOUT = 10.0


class BackendService:
    """HTTP/WS-сервер + WebRTC-транспорт для одной телефонной сессии."""

    def __init__(self, track_factory: TrackFactory, web_root: str | Path) -> None:
        if not callable(track_factory):
            raise TypeError("track_factory должен быть вызываемым")
        self._track_factory = track_factory
        self._web_root = Path(web_root)
        self._session = SessionManager()
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[SignalingServer] = None
        self._running = False
        self._last_error: Optional[str] = None

    # --- публичный API ---------------------------------------------------
    def start(self, host: str, port: int = DEFAULT_PORT) -> dict:
        """Поднимает сервер. Захват экрана не вызывается до авторизации."""
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host должен быть непустой строкой")
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError("port должен быть в диапазоне 1..65535")

        with self._lock:
            if self._running:
                if self._session.host == host and self._session.port == port:
                    return self._session.snapshot()
                raise RuntimeError("Сервер уже запущен на другом адресе.")

            loop = asyncio.new_event_loop()
            self._loop = loop
            self._server = SignalingServer(self._session, self._track_factory, self._web_root)
            self._last_error = None
            self._session.set_server(host, port)
            self._session.set_status(STATUS_WAITING)

            thread = threading.Thread(target=self._run_loop, name="backend-loop", daemon=True)
            self._thread = thread
            thread.start()

            try:
                self._call_blocking(self._start_server(host, port), timeout=15.0)
            except Exception as exc:  # noqa: BLE001 - сообщаем наружу безопасно
                log.exception("не удалось запустить backend")
                self._last_error = _safe_error(exc)
                self._session.set_status(STATUS_ERROR, "Не удалось запустить сервер.")
                self._shutdown_loop()
                return self._session.snapshot()

            self._running = True
            self._session.issue_token()
            self._session.set_status(STATUS_WAITING)
            return self._session.snapshot()

    def stop(self) -> dict:
        """Останавливает сервер, освобождает слот и токен."""
        with self._lock:
            if not self._running:
                self._session.reset()
                return self._session.snapshot()
            self._running = False
            try:
                self._call_blocking(self._stop_server(), timeout=15.0)
            except Exception:  # noqa: BLE001
                log.exception("ошибка при остановке backend")
            finally:
                self._shutdown_loop()
            self._session.reset()
            return self._session.snapshot()

    def rotate_token(self) -> dict:
        """Выпускает новый QR-токен. Запрещено при активном клиенте."""
        with self._lock:
            if not self._running:
                raise RuntimeError("Сервер не запущен.")
            if self._session.client_connected:
                raise RuntimeError("Нельзя обновить QR во время подключения.")
            self._session.issue_token()
            self._session.set_status(STATUS_WAITING)
            return self._session.snapshot()

    def snapshot(self) -> dict:
        """Текущее состояние без сетевых ожиданий."""
        return self._session.snapshot()

    # --- внутренние корутины ---------------------------------------------
    async def _start_server(self, host: str, port: int) -> None:
        assert self._server is not None
        await self._server.start(host, port)

    async def _stop_server(self) -> None:
        if self._server is not None:
            await self._server.stop()

    # --- поток и loop -----------------------------------------------------
    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:  # noqa: BLE001
                pass
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def _call_blocking(self, coro: Awaitable[Any], timeout: float) -> Any:
        loop = self._loop
        if loop is None:
            raise RuntimeError("Сетевой loop не запущен.")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def _shutdown_loop(self) -> None:
        loop = self._loop
        thread = self._thread
        self._loop = None
        self._thread = None
        self._server = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=LOOP_STOP_TIMEOUT)


def _safe_error(exc: BaseException) -> str:
    """Текст ошибки без токенов и SDP: только тип и короткое сообщение."""
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:200]}"

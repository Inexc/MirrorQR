"""Состояние сессии: одноразовые токены, слот одного клиента, статусы.

Модуль не знает про aiohttp/aiortc: только токены, их атомарное погашение и
общий снимок состояния. Всё, что меняется из event-loop, меняется под
asyncio.Lock; снимок читается из других потоков через обычный Lock.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

TOKEN_TTL_SECONDS = 120
AUTH_TIMEOUT_SECONDS = 5.0
WEBRTC_TIMEOUT_SECONDS = 20.0

STATUS_STOPPED = "stopped"
STATUS_WAITING = "waiting"
STATUS_CONNECTING = "connecting"
STATUS_STREAMING = "streaming"
STATUS_ERROR = "error"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Token:
    value: str
    expires_at: float  # epoch seconds


class TokenRejected(Exception):
    """Отказ авторизации с безопасным кодом (без раскрытия токена)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SessionManager:
    """Хранит действующий токен и признак занятого клиентского слота."""

    def __init__(self, ttl: float = TOKEN_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._token: Optional[Token] = None
        self.client_connected = False
        self.status = STATUS_STOPPED
        self.error: Optional[str] = None
        self.host = ""
        self.port = 0

    # --- токены ---------------------------------------------------------
    def issue_token(self) -> Token:
        """Выпускает новый токен и гасит прежний (rotate)."""
        with self._lock:
            token = Token(secrets.token_urlsafe(32), time.time() + self._ttl)
            self._token = token
            return token

    def current_token(self) -> Optional[Token]:
        with self._lock:
            return self._token

    def redeem(self, raw: str) -> str:
        """Атомарно проверяет и гасит токен. Возвращает его значение.

        Бросает TokenRejected с кодом invalid_token / token_expired.
        """
        if not isinstance(raw, str) or not raw:
            raise TokenRejected("invalid_token", "Неверный токен подключения.")
        with self._lock:
            token = self._token
            if token is None or not secrets.compare_digest(token.value, raw):
                raise TokenRejected("invalid_token", "Неверный токен подключения.")
            if time.time() >= token.expires_at:
                self._token = None
                raise TokenRejected("token_expired", "Срок действия QR истёк. Отсканируйте новый QR.")
            self._token = None
            return token.value

    def token_is_expired(self) -> bool:
        with self._lock:
            token = self._token
            return token is not None and time.time() >= token.expires_at

    # --- слот клиента ---------------------------------------------------
    def try_acquire_client(self) -> bool:
        with self._lock:
            if self.client_connected:
                return False
            self.client_connected = True
            return True

    def release_client(self) -> None:
        with self._lock:
            self.client_connected = False

    # --- состояние ------------------------------------------------------
    def set_status(self, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.error = error

    def set_server(self, host: str, port: int) -> None:
        with self._lock:
            self.host = host
            self.port = port

    def reset(self) -> None:
        with self._lock:
            self._token = None
            self.client_connected = False
            self.status = STATUS_STOPPED
            self.error = None

    def pairing_url(self, host: str, port: int) -> Optional[str]:
        with self._lock:
            token = self._token
            if token is None or time.time() >= token.expires_at:
                return None
            return f"http://{host}:{port}/phone#token={token.value}"

    def snapshot(self) -> dict:
        with self._lock:
            token = self._token
            pairing_url = None
            expires_at = None
            if token is not None and time.time() < token.expires_at:
                pairing_url = f"http://{self.host}:{self.port}/phone#token={token.value}"
                expires_at = to_iso(token.expires_at)
            return {
                "status": self.status,
                "host": self.host,
                "port": self.port,
                "pairingUrl": pairing_url,
                "tokenExpiresAt": expires_at,
                "clientConnected": self.client_connected,
                "error": self.error,
            }


async def wait_for_auth_timeout(seconds: float = AUTH_TIMEOUT_SECONDS) -> None:
    """Пауза окна авторизации; отдельная функция для тестов."""
    await asyncio.sleep(seconds)

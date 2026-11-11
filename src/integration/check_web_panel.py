"""Изолированные проверки локальной веб-панели (``create_panel_app``).

Запуск: python src/integration/check_web_panel.py

Проверки не запускают настоящий захват, Qt, телефонный сервер на 8765 и не
ходят во внешнюю сеть. Приложение панели поднимается на loopback анонимным
портом (``web.TCPSite(..., 0)``), а вместо ``BackendService`` используется
подставной сервис с публичной поверхностью настоящего сервиса
(``start(host, port)``, ``stop()``, ``snapshot()``, ``rotate_token()``).

Контракт панели жёстко закрепляет Host ``127.0.0.1:8766``. Поэтому в
проверках ``PANEL_HOST``/``PANEL_PORT``/``PANEL_ORIGIN``/``PANEL_HOST_HEADER``
подменяются на фактический адрес тестового сокета и восстанавливаются после
каждой проверки; проверка чужого Host при этом остаётся осмысленной.

Скрипт печатает ровно шесть результатов и возвращает 1, если нарушен
контракт. Это не подтверждение живой работы приложения.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aiohttp import ClientSession, web  # noqa: E402

from src.backend import web_panel  # noqa: E402

#: qrcode нужен только для сравнения QR-матрицы. Без него сервер обязан
#: честно ответить 500 qr_unavailable — это и проверяется, а не скрывается.
try:
    import qrcode  # noqa: F401
    import qrcode.image.svg  # noqa: F401
except ImportError:
    QRCODE_AVAILABLE = False
else:
    QRCODE_AVAILABLE = True

#: Ожидаемые ключи состояния в точном порядке контракта.
STATE_KEYS = (
    "status",
    "addresses",
    "selectedAddress",
    "pairingUrl",
    "tokenExpiresAt",
    "error",
)

#: Адрес, который подставной сервис выдаёт в списке доступных.
LAN_ADDRESS = "192.168.1.50"
#: Адрес, которого нет в списке.
FOREIGN_ADDRESS = "10.99.99.99"


# ---------------------------------------------------------------------------
# Подставной сервис
# ---------------------------------------------------------------------------


class FakeBackendService:
    """Двойник ``BackendService`` без сети, потоков и настоящего захвата.

    Возвращает снимок в форме ``SessionManager.snapshot`` и записывает
    вызовы, чтобы проверка могла отличить отклонённый запрос от успешного.
    """

    def __init__(self) -> None:
        self.start_calls: List[tuple] = []
        self.stop_calls = 0
        self.rotate_calls = 0
        self._status = "stopped"
        self._host = ""
        self._port = 0
        self._token: Optional[str] = None
        self._expires: Optional[str] = None
        self._error: Optional[str] = None
        self._counter = 0

    # --- публичный API ----------------------------------------------------
    def start(self, host: str, port: int = 8765) -> dict:
        self.start_calls.append((host, port))
        self._counter += 1
        self._token = f"fake-token-{self._counter}"
        self._host = host
        self._port = port
        self._status = "waiting"
        self._error = None
        self._expires = "2026-11-11T00:00:00Z"
        return self.snapshot()

    def stop(self) -> dict:
        self.stop_calls += 1
        self._token = None
        self._expires = None
        self._status = "stopped"
        self._error = None
        self._host = ""
        self._port = 0
        return self.snapshot()

    def rotate_token(self) -> dict:
        self.rotate_calls += 1
        return self.snapshot()

    def snapshot(self) -> dict:
        pairing_url = None
        if self._token is not None:
            pairing_url = (
                f"http://{LAN_ADDRESS}:{self._port or 8765}/phone#token={self._token}"
            )
        return {
            "status": self._status,
            "host": self._host,
            "port": self._port,
            "pairingUrl": pairing_url,
            "tokenExpiresAt": self._expires if pairing_url else None,
            "clientConnected": False,
            "error": self._error,
        }


# ---------------------------------------------------------------------------
# Окружение: временная статика, тестовый сервер, подмена LAN-адресов
# ---------------------------------------------------------------------------


class Panel:
    """In-process сервер панели на loopback с подменённым ожидаемым Host."""

    def __init__(self, service: FakeBackendService) -> None:
        self.service = service
        self._tmp = tempfile.TemporaryDirectory()
        self._runner: Optional[web.AppRunner] = None
        self._saved_lan = None
        self._saved_host = web_panel.PANEL_HOST
        self._saved_port = web_panel.PANEL_PORT
        self._saved_origin = web_panel.PANEL_ORIGIN
        self._saved_host_header = web_panel.PANEL_HOST_HEADER
        self.base = ""
        self.host_header = ""
        self.origin = ""

    async def __aenter__(self) -> "Panel":
        root = Path(self._tmp.name)
        (root / "desktop.html").write_text(
            "<!doctype html><title>panel</title>", encoding="utf-8"
        )
        (root / "desktop.js").write_text("/* panel */", encoding="utf-8")
        (root / "desktop.css").write_text("/* panel */", encoding="utf-8")

        self._saved_lan = web_panel.list_lan_ipv4
        web_panel.list_lan_ipv4 = lambda: [LAN_ADDRESS]

        # Панель жёстко сверяет Host/Origin с 127.0.0.1:8766. Тестовый сокет
        # занимает свободный порт (0), поэтому константы подменяются на
        # фактический адрес. Один bind на 0 — без гонки за фиксированный порт.
        app = await web_panel.create_panel_app(self.service, root)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        self.host_header = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host_header}"
        web_panel.PANEL_HOST = "127.0.0.1"
        web_panel.PANEL_PORT = port
        web_panel.PANEL_ORIGIN = self.origin
        web_panel.PANEL_HOST_HEADER = self.host_header
        self.base = self.origin
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        web_panel.PANEL_HOST = self._saved_host
        web_panel.PANEL_PORT = self._saved_port
        web_panel.PANEL_ORIGIN = self._saved_origin
        web_panel.PANEL_HOST_HEADER = self._saved_host_header
        if self._saved_lan is not None:
            web_panel.list_lan_ipv4 = self._saved_lan
        self._tmp.cleanup()

    # --- HTTP-помощники ---------------------------------------------------
    # aiohttp ClientSession сам выставляет Host по адресу соединения, поэтому
    # в обычных запросах он совпадает с подменённым PANEL_HOST_HEADER.
    # Чужой Host проверяется через `skip_auto_headers` в отдельном запросе.
    async def get_json(self, client: ClientSession, path: str,
                       headers: Optional[Dict[str, str]] = None) -> tuple[int, dict, Any]:
        async with client.get(self.base + path, headers=headers) as response:
            return response.status, await response.json(), response.headers

    def post_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Origin": self.origin, "Content-Type": "application/json"}
        if extra:
            headers.update(extra)
        return headers

    async def post_json(self, client: ClientSession, path: str, body: Any,
                        headers: Optional[Dict[str, str]] = None) -> tuple[int, dict]:
        async with client.post(
            self.base + path,
            data=json.dumps(body),
            headers=self.post_headers(headers),
        ) as response:
            return response.status, await response.json()


def _qr_matrix(data: str) -> Optional[List[List[bool]]]:
    """Матрица модулей QR для ``data`` (или None, если qrcode недоступен)."""
    if not QRCODE_AVAILABLE:
        return None

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return [list(row) for row in qr.get_matrix()]


_PATH_RE = None


def _svg_matrix(svg: str) -> Optional[List[List[bool]]]:
    """Собрать матрицу модулей из SVG ``SvgPathImage``.

    ``qrcode`` рисует вертикальные полосы по одному модулю: путь вида
    ``M4,4 h1 v1 h-1 z``. Каждый подпуть — один чёрный модуль, поэтому
    матрица строится по координатам ``M x,y`` и ``h``/``v``.
    """
    import re

    global _PATH_RE
    if _PATH_RE is None:
        _PATH_RE = re.compile(r"M\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")
    coords = [(int(float(x)), int(float(y))) for x, y in _PATH_RE.findall(svg)]
    if not coords:
        return None
    # Include the quiet zone on all four sides, not just dark modules.
    import xml.etree.ElementTree as ET
    viewbox = ET.fromstring(svg).attrib['viewBox'].split()
    size = int(float(viewbox[2]))
    matrix = [[False] * size for _ in range(size)]
    for x, y in coords:
        if 0 <= x < size and 0 <= y < size:
            matrix[y][x] = True
    return matrix


def _assert_state_shape(state: dict, failures: List[str]) -> None:
    if tuple(state.keys()) != STATE_KEYS:
        failures.append(f"state keys/order: {tuple(state.keys())!r}")
        return
    if state["status"] not in ("stopped", "starting", "running", "stopping", "error"):
        failures.append(f"state.status: {state['status']!r}")
    addresses = state["addresses"]
    if not isinstance(addresses, list):
        failures.append("state.addresses must be a list")
    else:
        for item in addresses:
            if not isinstance(item, dict) or set(item) != {"address", "label"}:
                failures.append(f"state.addresses item: {item!r}")
                break
            if not isinstance(item["address"], str) or not isinstance(item["label"], str):
                failures.append(f"state.addresses types: {item!r}")
                break
    if state["selectedAddress"] is not None and not isinstance(state["selectedAddress"], str):
        failures.append("state.selectedAddress must be null or string")
    if state["pairingUrl"] is not None and not isinstance(state["pairingUrl"], str):
        failures.append("state.pairingUrl must be null or string")
    if state["tokenExpiresAt"] is not None and not isinstance(state["tokenExpiresAt"], str):
        failures.append("state.tokenExpiresAt must be null or string")
    error = state["error"]
    if error is not None:
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            failures.append(f"state.error: {error!r}")
        elif not all(isinstance(v, str) for v in error.values()):
            failures.append(f"state.error types: {error!r}")


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------


async def check_schema(failures: List[str]) -> None:
    """1. Состояние до запуска: точные ключи, stopped, null-поля."""
    service = FakeBackendService()
    async with Panel(service) as panel:
        async with ClientSession() as client:
            status, state, headers = await panel.get_json(client, "/api/desktop/state")
            if status != 200:
                failures.append(f"state status {status}")
            if headers.get("Cache-Control") != "no-store":
                failures.append("state must be Cache-Control: no-store")
            _assert_state_shape(state, failures)
            if state.get("status") != "stopped":
                failures.append(f"initial status {state.get('status')!r}")
            for key in ("selectedAddress", "pairingUrl", "tokenExpiresAt", "error"):
                if state.get(key) is not None:
                    failures.append(f"initial {key} must be null")
            if service.start_calls:
                failures.append("state request must not call start")


async def check_start_stop(failures: List[str]) -> None:
    """2. start -> running, stop -> stopped; сервис вызван по одному разу."""
    service = FakeBackendService()
    async with Panel(service) as panel:
        async with ClientSession() as client:
            status, state = await panel.post_json(
                client, "/api/desktop/start", {"address": LAN_ADDRESS}
            )
            if status != 200:
                failures.append(f"start status {status}: {state}")
            if state.get("status") != "running":
                failures.append(f"start state.status {state.get('status')!r}")
            if state.get("selectedAddress") != LAN_ADDRESS:
                failures.append(f"start selectedAddress {state.get('selectedAddress')!r}")
            if not state.get("pairingUrl"):
                failures.append("start must expose pairingUrl")
            if service.start_calls != [(LAN_ADDRESS, 8765)]:
                failures.append(f"start calls {service.start_calls!r}")

            status, state = await panel.post_json(client, "/api/desktop/stop", {})
            if status != 200:
                failures.append(f"stop status {status}: {state}")
            if state.get("status") != "stopped":
                failures.append(f"stop state.status {state.get('status')!r}")
            if service.stop_calls != 1:
                failures.append(f"stop calls {service.stop_calls}")


async def check_qr_matches_link(failures: List[str]) -> None:
    """3. QR: 409 до запуска; после запуска SVG той же ссылки, без rotate_token."""
    service = FakeBackendService()
    async with Panel(service) as panel:
        async with ClientSession() as client:
            status, body, _ = await panel.get_json(client, "/api/desktop/qr.svg")
            if status != 409:
                failures.append(f"qr before start status {status}")
            if "error" not in body:
                failures.append("qr before start must return JSON error")

            await panel.post_json(client, "/api/desktop/start", {"address": LAN_ADDRESS})
            _, state, _ = await panel.get_json(client, "/api/desktop/state")
            pairing_url = state.get("pairingUrl") or ""
            if not pairing_url:
                failures.append("pairingUrl missing after start")
                return

            async with client.get(
                panel.base + "/api/desktop/qr.svg",
                headers={"Host": panel.host_header},
            ) as response:
                qr_status = response.status
                if response.headers.get("Cache-Control") != "no-store":
                    failures.append("qr must be Cache-Control: no-store")
                svg = await response.text() if qr_status == 200 else ""

            if service.rotate_calls:
                failures.append("qr must not rotate token")

            # QR кодирует ту же ссылку, что отдаёт состояние: сравниваем
            # матрицу модулей с локально построенной по той же строке.
            # Если qrcode не установлен, сервер честно отвечает 500
            # qr_unavailable — это проверяем, а не выдаём за сбой контракта.
            if not QRCODE_AVAILABLE:
                if qr_status != 500:
                    failures.append(
                        f"qr without qrcode must be 500, got {qr_status}"
                    )
                return
            if qr_status != 200:
                failures.append(f"qr status {qr_status}")
                return
            if "svg" not in response.content_type:
                failures.append(f"qr content-type {response.content_type!r}")
            if "<svg" not in svg:
                failures.append("qr response is not SVG")
            expected = _qr_matrix(pairing_url)
            actual = _svg_matrix(svg)
            if expected is None or actual is None:
                failures.append("qr SVG matrix could not be parsed")
            elif expected != actual:
                failures.append("qr SVG does not encode the pairingUrl")


async def check_foreign_origin_and_host(failures: List[str]) -> None:
    """4. Чужой Origin/Host отклоняется и не вызывает start."""
    service = FakeBackendService()
    async with Panel(service) as panel:
        async with ClientSession() as client:
            status, body = await panel.post_json(
                client,
                "/api/desktop/start",
                {"address": LAN_ADDRESS},
                headers={"Origin": "http://evil.example"},
            )
            if status not in (400, 403):
                failures.append(f"foreign origin status {status}")
            if "error" not in body:
                failures.append("foreign origin must return JSON error")
            if service.start_calls:
                failures.append("foreign origin must not call start")

            # aiohttp сам ставит Host по URL. Чтобы отправить чужой Host,
            # нужен и явный заголовок, и skip_auto_headers.
            foreign = dict(panel.post_headers(), Host="evil.example:8766")
            async with client.post(
                panel.base + "/api/desktop/start",
                data=json.dumps({"address": LAN_ADDRESS}),
                headers=foreign,
                skip_auto_headers=("Host",),
            ) as response:
                status = response.status
            if status not in (400, 403):
                failures.append(f"foreign host status {status}")
            if service.start_calls:
                failures.append("foreign host must not call start")

            async with client.get(
                panel.base + "/api/desktop/state",
                headers={"Host": "evil.example:8766"},
                skip_auto_headers=("Host",),
            ) as response:
                status = response.status
            if status not in (400, 403):
                failures.append(f"foreign host GET status {status}")


async def check_invalid_address(failures: List[str]) -> None:
    """5. Адрес вне списка — 400, start не вызывается, ссылка не меняется."""
    service = FakeBackendService()
    async with Panel(service) as panel:
        async with ClientSession() as client:
            await panel.post_json(client, "/api/desktop/start", {"address": LAN_ADDRESS})
            _, before, _ = await panel.get_json(client, "/api/desktop/state")
            start_calls = list(service.start_calls)

            status, body = await panel.post_json(
                client, "/api/desktop/start", {"address": FOREIGN_ADDRESS}
            )
            if status not in (400, 409):
                failures.append(f"invalid address status {status}")
            if "error" not in body:
                failures.append("invalid address must return JSON error")
            if service.start_calls != start_calls:
                failures.append("invalid address must not call start")

            _, after, _ = await panel.get_json(client, "/api/desktop/state")
            if before.get("pairingUrl") != after.get("pairingUrl"):
                failures.append("invalid address changed pairingUrl")


async def check_no_pairing_after_stop(failures: List[str]) -> None:
    """6. После stop pairingUrl/tokenExpiresAt = null, QR — 409."""
    service = FakeBackendService()
    async with Panel(service) as panel:
        async with ClientSession() as client:
            await panel.post_json(client, "/api/desktop/start", {"address": LAN_ADDRESS})
            _, running, _ = await panel.get_json(client, "/api/desktop/state")
            if not running.get("pairingUrl"):
                failures.append("running state must have pairingUrl")

            await panel.post_json(client, "/api/desktop/stop", {})
            _, stopped, _ = await panel.get_json(client, "/api/desktop/state")
            if stopped.get("pairingUrl") is not None:
                failures.append("pairingUrl must be null after stop")
            if stopped.get("tokenExpiresAt") is not None:
                failures.append("tokenExpiresAt must be null after stop")
            if stopped.get("status") != "stopped":
                failures.append(f"status after stop {stopped.get('status')!r}")

            status, _, _ = await panel.get_json(client, "/api/desktop/qr.svg")
            if status != 409:
                failures.append(f"qr after stop status {status}")


CHECKS = (
    ("schema", check_schema),
    ("start_stop", check_start_stop),
    ("qr_matches_link", check_qr_matches_link),
    ("foreign_origin_host", check_foreign_origin_and_host),
    ("invalid_address", check_invalid_address),
    ("no_pairing_after_stop", check_no_pairing_after_stop),
)


async def _run() -> int:
    failed = 0
    for name, check in CHECKS:
        failures: List[str] = []
        try:
            await check(failures)
        except Exception as exc:  # noqa: BLE001 - отдельная проверка не роняет прогон
            failures.append(f"exception: {type(exc).__name__}: {exc}")
        if failures:
            failed += 1
            print(f"FAIL {name}")
            for message in failures:
                print(f"     - {message}")
        else:
            print(f"ok   {name}")
    print()
    if failed:
        print(f"FAILED: {failed} of {len(CHECKS)} checks")
        return 1
    print(f"ALL {len(CHECKS)} CHECKS PASSED (fake service, no real capture)")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())

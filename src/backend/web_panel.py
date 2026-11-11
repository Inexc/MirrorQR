"""Локальная веб-панель управления backend: отдельный aiohttp-сервер.

Модуль не реализует захват экрана и не подменяет настоящий экран: он
управляет уже существующим :class:`BackendService` (запуск и остановка
сервера телефона на порту 8765) и отдаёт небольшую страницу для управления
с локального компьютера.

Панель слушает только ``127.0.0.1:8766``, принимает только loopback-клиентов
и Host ``127.0.0.1:8766``; POST дополнительно требует Origin
``http://127.0.0.1:8766`` и Content-Type ``application/json``. CORS нет.

Публичный API: ``create_panel_app(service, web_root)`` — асинхронная фабрика
``aiohttp.Application``. Синхронные методы ``BackendService`` вызываются в
пуле потоков, чтобы не блокировать event loop панели; управляющие операции
сериализуются ``asyncio.Lock``.

Статус сервиса (``stopped|waiting|connecting|streaming|error``) сводится к
статусу панели (``stopped|starting|running|stopping|error``); сам сервис,
сигналинг и токены не меняются.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiohttp import web

log = logging.getLogger(__name__)

#: Порт локальной панели (существующий сервер телефона остаётся на 8765).
PANEL_PORT = 8766
#: Локальный адрес панели: только loopback.
PANEL_HOST = "127.0.0.1"
#: Адрес, который панель принимает как Host и Origin.
PANEL_ORIGIN = f"http://{PANEL_HOST}:{PANEL_PORT}"
#: Точное значение заголовка Host для панели.
PANEL_HOST_HEADER = f"{PANEL_HOST}:{PANEL_PORT}"

#: Только эти три файла статики отдаются панелью.
STATIC_FILES = {
    "/desktop": ("desktop.html", "text/html; charset=utf-8"),
    "/assets/desktop.js": ("desktop.js", "application/javascript; charset=utf-8"),
    "/assets/desktop.css": ("desktop.css", "text/css; charset=utf-8"),
}

#: Ключи состояния панели в точном порядке контракта.
STATE_KEYS = (
    "status",
    "addresses",
    "selectedAddress",
    "pairingUrl",
    "tokenExpiresAt",
    "error",
)

#: Статусы сервиса, которые панель показывает как running.
_RUNNING_STATUSES = frozenset({"waiting", "connecting", "streaming"})

#: Предел тела JSON-запроса; запросы крошечные.
MAX_BODY_BYTES = 16 * 1024

class PanelError(Exception):
    """Ошибка панели с HTTP-кодом и безопасным кодом/сообщением."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Адреса
# ---------------------------------------------------------------------------


def _rank(addr: str) -> tuple:
    """Порядок предпочтения LAN-адресов: реальные сети раньше виртуальных.

    Повторяет правила ``src/desktop/qr.py``: WSL/Docker (172.16/12),
    VirtualBox host-only (192.168.56/24) и Docker Desktop (10.0.75/24)
    отодвигаются в конец, чтобы не предлагать телефону невидимый адрес.
    """
    ip = ipaddress.IPv4Address(addr)
    virtual = False
    if ip in ipaddress.IPv4Network("172.16.0.0/12"):
        virtual = True
    elif ip in ipaddress.IPv4Network("192.168.56.0/24"):
        virtual = True
    elif ip in ipaddress.IPv4Network("10.0.75.0/24"):
        virtual = True
    return (1 if virtual else 0, ip)


def _raw_ipv4_addresses() -> List[str]:
    """Все IPv4-адреса машины (без фильтрации), в порядке обнаружения."""
    found: List[str] = []
    try:
        infos = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        )
    except OSError:
        infos = []
    for info in infos:
        addr = info[4][0]
        if addr not in found:
            found.append(addr)

    # На Windows getaddrinfo по имени хоста иногда отдаёт только один адрес.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))  # TEST-NET-1, пакет не уходит
            addr = probe.getsockname()[0]
            if addr not in found:
                found.append(addr)
        finally:
            probe.close()
    except OSError:
        pass
    return found


def list_lan_ipv4() -> List[str]:
    """Приватные IPv4-адреса, пригодные для телефона в той же LAN.

    Дублирует логику ``src/desktop/qr.py``, чтобы не тянуть PySide6 в
    веб-режим: loopback, link-local, multicast и не-приватные адреса
    отбрасываются, виртуальные адаптеры идут в конец.
    """
    result: List[str] = []
    for addr in _raw_ipv4_addresses():
        try:
            ip = ipaddress.IPv4Address(addr)
        except ipaddress.AddressValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast:
            continue
        if not ip.is_private:
            continue
        if addr not in result:
            result.append(addr)
    result.sort(key=_rank)
    return result


def _label_for(addr: str) -> str:
    """Человекочитаемая подпись адреса без обращения к системе."""
    return f"LAN {addr}"


def _addresses() -> List[Dict[str, str]]:
    return [{"address": addr, "label": _label_for(addr)} for addr in list_lan_ipv4()]


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------


def _map_status(service_status: Optional[str], running: bool) -> str:
    """Свести статус сервиса к статусу панели."""
    if not running:
        return "stopped"
    if service_status == "error":
        return "error"
    if service_status in _RUNNING_STATUSES:
        return "running"
    return "running"


def _error_obj(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _short(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:200]}"


class _Panel:
    """Состояние панели и адаптер к ``BackendService``."""

    def __init__(self, service: Any, web_root: Path) -> None:
        self.service = service
        self.web_root = Path(web_root)
        self._control = asyncio.Lock()
        self._selected_address: Optional[str] = None
        self._running = False
        self._pending: Optional[str] = None  # "starting" | "stopping" | None
        self._error: Optional[Dict[str, str]] = None

    # --- синхронные вызовы сервиса (в пуле потоков) ----------------------
    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        # BackendService already bounds its network waits. Do not abandon a
        # live executor call: that would release the control lock too early.
        return await asyncio.shield(loop.run_in_executor(None, lambda: fn(*args)))

    def _snapshot(self) -> Dict[str, Any]:
        try:
            data = self.service.snapshot()
        except Exception as exc:  # noqa: BLE001 - панель не должна падать
            log.exception("snapshot() не удался")
            return {"status": "error", "error": _short(exc)}
        if not isinstance(data, dict):
            return {"status": "error", "error": "Сервис вернул некорректное состояние."}
        return data

    # --- состояние -------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        snap = self._snapshot()
        service_status = snap.get("status")
        running = self._running and service_status != "stopped"
        status = self._pending or ("error" if self._error else _map_status(service_status, running))

        pairing_url = snap.get("pairingUrl")
        expires_at = snap.get("tokenExpiresAt")
        if not running or not isinstance(pairing_url, str) or not pairing_url:
            pairing_url = None
            expires_at = None

        error: Optional[Dict[str, str]] = None
        if self._pending is None:
            if self._error is not None:
                error = self._error
            elif status == "error":
                raw = snap.get("error")
                message = raw if isinstance(raw, str) and raw else "Сервис сообщил об ошибке."
                error = _error_obj("service_error", message)

        selected = self._selected_address
        if not running and selected is None:
            host = snap.get("host")
            if isinstance(host, str) and host:
                selected = host

        return {
            "status": status,
            "addresses": _addresses(),
            "selectedAddress": selected,
            "pairingUrl": pairing_url,
            "tokenExpiresAt": expires_at,
            "error": error,
        }

    # --- управление ------------------------------------------------------
    async def start(self, address: str) -> Dict[str, Any]:
        """Запустить сервис на ``address``; повторный вызов — 409."""
        if self._control.locked():
            raise PanelError(409, "busy", "Другая операция уже выполняется.")
        async with self._control:
            if self._running:
                raise PanelError(409, "already_running", "Сервер уже запущен.")
            if address not in list_lan_ipv4():
                raise PanelError(400, "invalid_address", "Адрес не входит в список доступных LAN IPv4.")
            self._selected_address = address
            self._pending = "starting"
            self._error = None
            try:
                snap = await self._call(self.service.start, address)
            except asyncio.TimeoutError:
                self._pending = None
                self._running = False
                self._error = _error_obj("timeout", "Запуск сервера не завершился вовремя.")
                raise PanelError(500, "timeout", "Запуск сервера не завершился вовремя.") from None
            except Exception as exc:  # noqa: BLE001
                log.exception("start() не удался")
                self._pending = None
                self._running = False
                self._error = _error_obj("start_failed", _short(exc))
                raise PanelError(500, "start_failed", _short(exc)) from None
            finally:
                self._pending = None

            if not isinstance(snap, dict):
                snap = {}
            if snap.get("status") == "error":
                self._running = False
                raw = snap.get("error")
                message = raw if isinstance(raw, str) and raw else "Не удалось запустить сервер."
                self._error = _error_obj("start_failed", message)
                raise PanelError(500, "start_failed", message)
            self._running = True
            return self.state()

    async def stop(self) -> Dict[str, Any]:
        """Остановить сервис; повторный вызов идемпотентен."""
        if self._control.locked():
            raise PanelError(409, "busy", "Другая операция уже выполняется.")
        async with self._control:
            if not self._running:
                self._selected_address = None
                self._error = None
                return self.state()
            self._pending = "stopping"
            try:
                await self._call(self.service.stop)
            except asyncio.TimeoutError:
                self._error = _error_obj("timeout", "Остановка сервера не завершилась вовремя.")
                raise PanelError(500, "timeout", "Остановка сервера не завершилась вовремя.") from None
            except Exception as exc:  # noqa: BLE001
                log.exception("stop() не удался")
                self._error = _error_obj("stop_failed", _short(exc))
                raise PanelError(500, "stop_failed", _short(exc)) from None
            finally:
                self._pending = None
                self._running = False
                self._selected_address = None
            return self.state()

    def require_pairing_url(self) -> str:
        """Действующая pairingUrl для QR или 409, если её нет.

        Токен не создаётся и не обновляется: берётся только значение из
        ``snapshot()``.
        """
        state = self.state()
        if state["status"] != "running":
            raise PanelError(409, "not_running", "Сервер не запущен.")
        url = state.get("pairingUrl")
        if not isinstance(url, str) or not url:
            raise PanelError(409, "no_pairing_url", "Ссылка подключения недоступна или истекла.")
        return url


# ---------------------------------------------------------------------------
# QR
# ---------------------------------------------------------------------------


def _render_qr_svg(data: str) -> str:
    """SVG QR-код той же ссылки через ``qrcode`` (без новых зависимостей)."""
    import qrcode  # локально: отсутствие пакета не должно ломать импорт модуля
    import qrcode.image.svg

    factory = qrcode.image.svg.SvgPathImage
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
        image_factory=factory,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image()
    try:
        return image.to_string(encoding="unicode")
    except TypeError:  # старые версии qrcode
        raw = image.to_string()
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


# ---------------------------------------------------------------------------
# HTTP-помощники
# ---------------------------------------------------------------------------


def _json(body: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(body, ensure_ascii=False),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _error_response(exc: PanelError) -> web.Response:
    return _json({"error": _error_obj(exc.code, exc.message)}, status=exc.status)


def _is_loopback(request: web.Request) -> bool:
    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not peer:
        return False
    host = peer[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_host(request: web.Request) -> None:
    host = (request.headers.get("Host") or "").strip()
    if host != PANEL_HOST_HEADER:
        raise PanelError(403, "bad_host", "Запрос должен адресовать 127.0.0.1:8766.")


def _check_origin(request: web.Request) -> None:
    origin = (request.headers.get("Origin") or "").strip()
    if origin != PANEL_ORIGIN:
        raise PanelError(403, "bad_origin", "Недопустимый Origin.")


def _check_json_content(request: web.Request) -> None:
    ctype = (request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise PanelError(403, "bad_content_type", "Ожидается Content-Type application/json.")


async def _read_json(request: web.Request) -> Dict[str, Any]:
    """Читает JSON-объект; любое нарушение — 400."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise PanelError(400, "invalid_json", "Тело запроса слишком велико.")
    try:
        raw = await request.read()
    except Exception:  # noqa: BLE001
        raise PanelError(400, "invalid_json", "Не удалось прочитать тело запроса.") from None
    if len(raw) > MAX_BODY_BYTES:
        raise PanelError(400, "invalid_json", "Тело запроса слишком велико.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PanelError(400, "invalid_json", "Тело запроса не является JSON.") from None
    if not isinstance(data, dict):
        raise PanelError(400, "invalid_json", "Тело запроса должно быть JSON-объектом.")
    return data


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _guard(handler: Handler) -> Handler:
    """Обёртка: loopback, Host, PanelError -> JSON-ошибка."""

    async def wrapped(request: web.Request) -> web.StreamResponse:
        try:
            if not _is_loopback(request):
                raise PanelError(403, "forbidden", "Панель доступна только с локального компьютера.")
            _check_host(request)
            return await handler(request)
        except PanelError as exc:
            return _error_response(exc)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001 - панель не должна падать
            log.exception("ошибка обработчика панели")
            return _error_response(PanelError(500, "internal", "Внутренняя ошибка панели."))

    return wrapped


def _guard_post(handler: Handler) -> Handler:
    """POST-обёртка: Origin и Content-Type проверяются до чтения тела."""

    async def wrapped(request: web.Request) -> web.StreamResponse:
        try:
            if not _is_loopback(request):
                raise PanelError(403, "forbidden", "Панель доступна только с локального компьютера.")
            _check_host(request)
            _check_origin(request)
            _check_json_content(request)
            return await handler(request)
        except PanelError as exc:
            return _error_response(exc)
        except web.HTTPException:
            raise
        except Exception:  # noqa: BLE001
            log.exception("ошибка обработчика панели")
            return _error_response(PanelError(500, "internal", "Внутренняя ошибка панели."))

    return wrapped


# ---------------------------------------------------------------------------
# Фабрика приложения
# ---------------------------------------------------------------------------


def _safe_static(web_root: Path, name: str) -> Path:
    root = web_root.resolve()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise web.HTTPNotFound() from None
    if not candidate.is_file():
        raise web.HTTPNotFound()
    return candidate


async def create_panel_app(service: Any, web_root: str | Path) -> web.Application:
    """Собрать ``aiohttp.Application`` локальной панели.

    ``service`` — объект с публичными ``start(host, port)``, ``stop()`` и
    ``snapshot()`` (``BackendService``). ``web_root`` — каталог с
    ``desktop.html``, ``desktop.js``, ``desktop.css``.
    """
    panel = _Panel(service, Path(web_root))
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app["panel"] = panel

    def _static_handler(name: str, ctype: str) -> Handler:
        async def handler(request: web.Request) -> web.StreamResponse:
            path = _safe_static(panel.web_root, name)
            return web.FileResponse(
                path,
                headers={
                    "Content-Type": ctype,
                    "Cache-Control": "no-store",
                },
            )

        return handler

    async def _state(request: web.Request) -> web.StreamResponse:
        return _json(panel.state())

    async def _start(request: web.Request) -> web.StreamResponse:
        body = await _read_json(request)
        if set(body) != {"address"}:
            raise PanelError(400, "invalid_body", "Ожидается только поле address.")
        address = body.get("address")
        if not isinstance(address, str) or not address.strip():
            raise PanelError(400, "invalid_address", "Поле address обязательно.")
        return _json(await panel.start(address.strip()))

    async def _stop(request: web.Request) -> web.StreamResponse:
        body = await _read_json(request)
        if body:
            raise PanelError(400, "invalid_body", "Ожидается пустой объект.")
        return _json(await panel.stop())

    async def _qr(request: web.Request) -> web.StreamResponse:
        url = panel.require_pairing_url()
        try:
            svg = await asyncio.get_running_loop().run_in_executor(None, _render_qr_svg, url)
        except ImportError as exc:
            log.exception("qrcode недоступен")
            raise PanelError(500, "qr_unavailable", f"Генерация QR недоступна: {_short(exc)}") from None
        except Exception as exc:  # noqa: BLE001
            log.exception("не удалось сгенерировать QR")
            raise PanelError(500, "qr_failed", _short(exc)) from None
        return web.Response(
            text=svg,
            content_type="image/svg+xml",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def _monitors(request: web.Request) -> web.StreamResponse:
        if sys.platform == "win32":
            from src.capture.windows import get_selected_monitor, list_monitors
            monitors = await asyncio.get_running_loop().run_in_executor(None, list_monitors)
            selected = get_selected_monitor()
        else:
            monitors = [{"id": 0, "name": "Основной экран", "width": 1920, "height": 1080, "is_primary": True}]
            selected = 0
        return _json({"monitors": monitors, "selected": selected})

    async def _select_monitor(request: web.Request) -> web.StreamResponse:
        body = await _read_json(request)
        if set(body) != {"monitor"}:
            raise PanelError(400, "invalid_body", "Ожидается только поле monitor.")
        monitor_val = body.get("monitor")
        if not isinstance(monitor_val, int):
            raise PanelError(400, "invalid_monitor", "Поле monitor должно быть числом.")
        if sys.platform == "win32":
            from src.capture.windows import set_selected_monitor
            set_selected_monitor(monitor_val)
        return _json({"selected": monitor_val})

    for route, (name, ctype) in STATIC_FILES.items():
        app.router.add_get(route, _guard(_static_handler(name, ctype)))
    app.router.add_get("/api/desktop/state", _guard(_state))
    app.router.add_post("/api/desktop/start", _guard_post(_start))
    app.router.add_post("/api/desktop/stop", _guard_post(_stop))
    app.router.add_get("/api/desktop/qr.svg", _guard(_qr))
    app.router.add_get("/api/desktop/monitors", _guard(_monitors))
    app.router.add_post("/api/desktop/select-monitor", _guard_post(_select_monitor))
    return app


__all__ = [
    "PANEL_HOST",
    "PANEL_PORT",
    "PANEL_ORIGIN",
    "PanelError",
    "create_panel_app",
    "list_lan_ipv4",
]

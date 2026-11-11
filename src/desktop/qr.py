"""Локальная генерация QR-кода и выбор LAN IPv4.

Модуль не зависит от backend: он получает готовый pairingUrl из снимка
состояния сервиса, кодирует его целиком (включая ``#token=...``) и
отдаёт изображение в Qt. Внешние сервисы не используются.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import List

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap

#: Порт, который слушает локальный сервер (зафиксирован общим контрактом).
SERVER_PORT = 8765

#: Минимальный размер QR-кода на экране.
QR_MIN_SIZE = 240

def _ipv4_addresses() -> List[str]:
    """Все IPv4-адреса машины, кроме loopback и link-local."""
    found: List[str] = []
    host = socket.gethostname()
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        addr = info[4][0]
        if addr not in found:
            found.append(addr)

    # Дополнительный проход: на Windows getaddrinfo по имени хоста иногда
    # возвращает только один адрес, поэтому учитываем и адрес «наружу».
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
    """Список IPv4-адресов для выбора в интерфейсе.

    Возвращает только приватные адреса, пригодные для телефона в той же
    сети. Если ничего не найдено — возвращает пустой список, и окно
    сообщает об этом пользователю.
    """
    result: List[str] = []
    for addr in _ipv4_addresses():
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
    return result


def _rank(addr: str) -> tuple:
    """Порядок предпочтения адресов: меньше — лучше.

    На Windows виртуальные адаптеры (WSL, Docker, VirtualBox, Hyper-V)
    часто стоят в списке раньше реального Wi-Fi, поэтому их адресные
    диапазоны отодвигаем в конец. Телефон в той же сети такие адреса
    не увидит.
    """
    ip = ipaddress.IPv4Address(addr)
    virtual = False
    if ip in ipaddress.IPv4Network("172.16.0.0/12"):
        # WSL/Docker по умолчанию живут в 172.17–172.31.
        virtual = True
    elif ip in ipaddress.IPv4Network("192.168.56.0/24"):
        # VirtualBox host-only.
        virtual = True
    elif ip in ipaddress.IPv4Network("10.0.75.0/24"):
        # Docker Desktop (Windows).
        virtual = True
    return (1 if virtual else 0, ip)


def preferred_lan_ipv4() -> str | None:
    """Наиболее вероятный адрес для телефона или None.

    ``list_lan_ipv4`` уже отбрасывает loopback, link-local и multicast.
    Предпочитаем адрес из реальной домашней/офисной сети, а не из
    виртуального адаптера.
    """
    addresses = list_lan_ipv4()
    if not addresses:
        return None
    return min(addresses, key=_rank)


def build_pairing_url(host: str, port: int = SERVER_PORT, token: str | None = None) -> str:
    """Собрать адрес вида ``http://<LAN-IP>:<port>/phone#token=<token>``."""
    base = f"http://{host}:{int(port)}/phone"
    if token:
        return f"{base}#token={token}"
    return base


def make_qr_image(data: str, size: int = QR_MIN_SIZE) -> QImage:
    """Сгенерировать QR-код локально и вернуть его как QImage.

    ``data`` кодируется целиком, без обрезки fragment-части. Коррекция
    ошибок — M, чтобы код оставался читаемым на экране монитора.
    """
    if not data:
        raise ValueError("Нет данных для QR-кода")

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    module_count = len(matrix)
    if module_count == 0:
        raise ValueError("Пустой QR-код")

    # Целевой размер — не меньше запрошенного, кратный числу модулей,
    # чтобы не получить «мыло» при масштабировании.
    scale = max(1, -(-size // module_count))
    dimension = module_count * scale

    image = QImage(dimension, dimension, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    black = 0xFF000000
    for row_index, row in enumerate(matrix):
        for col_index, filled in enumerate(row):
            if not filled:
                continue
            for dy in range(scale):
                for dx in range(scale):
                    image.setPixel(
                        col_index * scale + dx,
                        row_index * scale + dy,
                        black,
                    )
    return image


def make_qr_pixmap(data: str, size: int = QR_MIN_SIZE) -> QPixmap:
    """Обёртка над :func:`make_qr_image` для прямого показа в QLabel."""
    return QPixmap.fromImage(make_qr_image(data, size))


def render_qr(data: str, size: int = QR_MIN_SIZE) -> QPixmap:
    """Совместимое имя: QR-код как QPixmap нужного размера."""
    return make_qr_pixmap(data, size)


__all__ = [
    "SERVER_PORT",
    "QR_MIN_SIZE",
    "list_lan_ipv4",
    "preferred_lan_ipv4",
    "build_pairing_url",
    "make_qr_image",
    "make_qr_pixmap",
    "render_qr",
]

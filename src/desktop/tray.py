"""Иконка приложения в системном трее.

Трей — вспомогательный элемент: если система не поддерживает трей или
иконка не создаётся, приложение продолжает работать через окно, а
пользователь получает явное сообщение об этом (см. ``note``).

Меню содержит ровно три действия: «Показать», «Остановить», «Выйти».
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .window import DesktopWindow

logger = logging.getLogger(__name__)

#: Сообщение, если системный трей недоступен.
TRAY_UNAVAILABLE_NOTE = (
    "Системный трей недоступен: приложение работает через окно."
)


def _make_icon() -> QIcon:
    """Простая иконка «экран» без внешних файлов."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1f6feb"))
        painter.drawRoundedRect(6, 12, 52, 34, 6, 6)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(12, 18, 40, 22, 3, 3)
        painter.setBrush(QColor("#1f6feb"))
        painter.drawRoundedRect(26, 48, 12, 6, 2, 2)
    finally:
        painter.end()
    return QIcon(pixmap)


class DesktopTray(QObject):
    """Иконка в трее с тремя действиями.

    Параметры:

    * ``window`` — :class:`DesktopWindow`, который нужно показать;
    * ``service`` — сервис с ``stop()``; вызывается в фоне, чтобы не
      блокировать Qt event loop.
    """

    def __init__(self, window: DesktopWindow, service: Any) -> None:
        super().__init__(window)
        self._window = window
        self._service = service
        self._tray: Optional[QSystemTrayIcon] = None
        self._note = ""

        self._action_show = QAction("Показать", self)
        self._action_show.triggered.connect(self._on_show)
        self._action_stop = QAction("Остановить", self)
        self._action_stop.triggered.connect(self._on_stop)
        self._action_quit = QAction("Выйти", self)
        self._action_quit.triggered.connect(self._on_quit)

        self._menu = QMenu()
        self._menu.addAction(self._action_show)
        self._menu.addAction(self._action_stop)
        self._menu.addSeparator()
        self._menu.addAction(self._action_quit)

        self._create_tray()

    # ------------------------------------------------------------- create

    def _create_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._note = TRAY_UNAVAILABLE_NOTE
            logger.warning("desktop: %s", TRAY_UNAVAILABLE_NOTE)
            self._window.set_tray_note(TRAY_UNAVAILABLE_NOTE)
            return

        try:
            tray = QSystemTrayIcon(_make_icon(), self)
            tray.setToolTip("Экран по QR")
            tray.setContextMenu(self._menu)
            tray.activated.connect(self._on_activated)
            tray.show()
        except Exception as exc:  # noqa: BLE001 - трей не должен ронять окно
            logger.warning("desktop: не удалось создать трей: %s", exc)
            self._note = TRAY_UNAVAILABLE_NOTE
            self._window.set_tray_note(TRAY_UNAVAILABLE_NOTE)
            return

        self._tray = tray

    # -------------------------------------------------------------- state

    @property
    def available(self) -> bool:
        """True, если иконка в трее реально создана и показана."""
        return self._tray is not None

    @property
    def note(self) -> str:
        """Текст предупреждения о недоступном трее (пусто, если всё в порядке)."""
        return self._note

    # ------------------------------------------------------------ actions

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_show()

    def _on_show(self) -> None:
        self._window.show_and_raise()

    def _on_stop(self) -> None:
        # Остановка идёт через обычный путь окна: он уже умеет выполнять
        # вызов сервиса в фоне и обновлять интерфейс.
        self._window.stop_from_tray()

    def _on_quit(self) -> None:
        self._window.request_quit()

    # ------------------------------------------------------------- cleanup

    def hide(self) -> None:
        """Скрыть иконку (вызывается при выходе)."""
        if self._tray is not None:
            self._tray.hide()


__all__ = ["DesktopTray", "TRAY_UNAVAILABLE_NOTE"]

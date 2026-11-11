"""Десктопный интерфейс приложения «Экран по QR».

Модуль экспортирует два публичных класса:

* :class:`DesktopWindow` — единственное окно приложения;
* :class:`DesktopTray` — иконка в системном трее.

``QApplication`` создаётся в ``main.py`` и сюда не передаётся.
"""

from __future__ import annotations

from .tray import DesktopTray
from .window import DesktopWindow

__all__ = ["DesktopWindow", "DesktopTray"]

"""Единственное окно приложения «Экран по QR».

Окно не выполняет сетевых операций в Qt-потоке: ``start``/``stop``/
``rotate_token``/``snapshot`` вызываются через ``QThreadPool``, а результат
возвращается в UI-поток сигналом. Пока запрос выполняется, повторный запуск
не делается, чтобы не плодить параллельные вызовы сервиса.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt, QThreadPool, QTimer, QRunnable, QObject, Signal
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .qr import (
    QR_MIN_SIZE,
    SERVER_PORT,
    list_lan_ipv4,
    make_qr_pixmap,
    preferred_lan_ipv4,
)

logger = logging.getLogger(__name__)

#: Интервал опроса снимка состояния сервиса.
POLL_INTERVAL_MS = 500

#: Русские статусы из общего контракта.
STATUS_TEXT: Dict[str, str] = {
    "stopped": "Остановлено",
    "waiting": "Ожидание телефона",
    "connecting": "Подключение",
    "streaming": "Трансляция",
    "error": "Ошибка",
}

#: Код статуса, при котором сервер запущен.
_RUNNING_STATUSES = {"waiting", "connecting", "streaming"}


class _CallSignals(QObject):
    """Сигналы для возврата результата фонового вызова в UI-поток."""

    done = Signal(object, object)  # (result, error)


class _Call(QRunnable):
    """Один вызов сервиса в пуле потоков."""

    def __init__(self, fn: Callable[[], Any], signals: _CallSignals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:  # noqa: D401 - Qt API
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - ошибка уходит в UI
            logger.warning("desktop: вызов сервиса завершился ошибкой: %s", exc)
            try:
                self._signals.done.emit(None, exc)
            except RuntimeError:
                # Окно уже уничтожено — результат никому не нужен.
                pass
            return
        try:
            self._signals.done.emit(result, None)
        except RuntimeError:
            pass


class DesktopWindow(QMainWindow):
    """Главное окно: выбор адреса, запуск, QR и статус."""

    def __init__(self, service: Any) -> None:
        super().__init__()
        self._service = service
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._busy = False
        self._closed = False
        self._last_snapshot: Optional[Dict[str, Any]] = None
        self._qr_url: Optional[str] = None
        self._qr_pixmap: Optional[QPixmap] = None
        self._tray_unavailable_note = ""

        self.setObjectName("desktopWindow")
        self.setWindowTitle("Экран по QR")
        self.setMinimumWidth(420)
        self._build_ui()
        self._load_networks()
        self._load_monitors()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        self._poll()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("desktopCentral")
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        title = QLabel("Экран по QR", central)
        font = title.font()
        font.setPointSize(max(16, font.pointSize() + 8))
        font.setBold(True)
        title.setFont(font)
        outer.addWidget(title)

        subtitle = QLabel(
            "Отсканируйте код телефоном в той же сети Wi-Fi — приложение "
            "на телефон не устанавливается.",
            central,
        )
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        network_row = QHBoxLayout()
        network_row.setSpacing(8)
        network_label = QLabel("Адрес в сети:", central)
        self.networkSelector = QComboBox(central)
        self.networkSelector.setObjectName("networkSelector")
        self.networkSelector.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        network_row.addWidget(network_label)
        network_row.addWidget(self.networkSelector, 1)
        outer.addLayout(network_row)

        monitor_row = QHBoxLayout()
        monitor_row.setSpacing(8)
        monitor_label = QLabel("Экран для захвата:", central)
        self.monitorSelector = QComboBox(central)
        self.monitorSelector.setObjectName("monitorSelector")
        self.monitorSelector.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        monitor_row.addWidget(monitor_label)
        monitor_row.addWidget(self.monitorSelector, 1)
        outer.addLayout(monitor_row)

        qr_frame = QFrame(central)
        qr_frame.setObjectName("qrFrame")
        qr_frame.setFrameShape(QFrame.Shape.StyledPanel)
        qr_layout = QVBoxLayout(qr_frame)
        qr_layout.setContentsMargins(16, 16, 16, 16)
        qr_layout.setSpacing(8)

        self.qrLabel = QLabel(qr_frame)
        self.qrLabel.setObjectName("qrLabel")
        self.qrLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qrLabel.setMinimumSize(QR_MIN_SIZE, QR_MIN_SIZE)
        self.qrLabel.setText("QR-код появится после запуска")
        qr_layout.addWidget(self.qrLabel, 0, Qt.AlignmentFlag.AlignHCenter)

        self.addressLabel = QLabel("", qr_frame)
        self.addressLabel.setObjectName("addressLabel")
        self.addressLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.addressLabel.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.addressLabel.setWordWrap(True)
        qr_layout.addWidget(self.addressLabel)

        outer.addWidget(qr_frame, 1)

        self.statusLabel = QLabel("Остановлено", central)
        self.statusLabel.setObjectName("statusLabel")
        self.statusLabel.setWordWrap(True)
        outer.addWidget(self.statusLabel)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.startButton = QPushButton("Запустить", central)
        self.startButton.setObjectName("startButton")
        self.startButton.clicked.connect(self._on_start)

        self.stopButton = QPushButton("Остановить", central)
        self.stopButton.setObjectName("stopButton")
        self.stopButton.clicked.connect(self._on_stop)

        self.refreshQrButton = QPushButton("Новый QR", central)
        self.refreshQrButton.setObjectName("refreshQrButton")
        self.refreshQrButton.clicked.connect(self._on_refresh_qr)

        buttons.addWidget(self.startButton)
        buttons.addWidget(self.stopButton)
        buttons.addWidget(self.refreshQrButton)
        outer.addLayout(buttons)

        self._apply_style()
        self._update_controls(None)

    def _apply_style(self) -> None:
        self.resize(420, 560)
        self.setStyleSheet('''
            QMainWindow, QWidget#desktopCentral { background: #0b0d10; }
            QLabel { color: #f2f5f8; }
            QLabel#statusLabel, QLabel#addressLabel { color: #9aa6b4; }
            QFrame#qrFrame { background: #14181e; border: 1px solid #2a323d; border-radius: 12px; }
            QLabel#qrLabel { background: white; color: #14181e; }
            QPushButton, QComboBox { min-height: 32px; padding: 4px 8px;
                background: #1b2129; color: #f2f5f8; border: 1px solid #2a323d; border-radius: 8px; }
            QComboBox QAbstractItemView { background: #1b2129; color: #f2f5f8; }
            QPushButton:hover { background: #232b35; }
            QPushButton:focus, QComboBox:focus { border-color: #3b82f6; }
            QPushButton:disabled { color: #6b7785; }
            QPushButton#startButton { background: #2563eb; color: white; }
            QPushButton#startButton:disabled { background: #1b2129; color: #6b7785; }
        ''')

    # ------------------------------------------------------------- network

    def _load_networks(self) -> None:
        self.networkSelector.clear()
        addresses = list_lan_ipv4()
        if not addresses:
            self.networkSelector.addItem("LAN-адрес не найден", None)
            self.networkSelector.setEnabled(False)
            return
        preferred = preferred_lan_ipv4()
        for addr in addresses:
            self.networkSelector.addItem(addr, addr)
        if preferred:
            index = self.networkSelector.findData(preferred)
            if index >= 0:
                self.networkSelector.setCurrentIndex(index)

    def _selected_host(self) -> Optional[str]:
        data = self.networkSelector.currentData()
        return data if isinstance(data, str) and data else None

    # ------------------------------------------------------------- monitor

    def _load_monitors(self) -> None:
        self.monitorSelector.clear()
        import sys
        if sys.platform == "win32":
            from src.capture.windows import get_selected_monitor, list_monitors
            monitors = list_monitors()
            selected = get_selected_monitor()
            for m in monitors:
                self.monitorSelector.addItem(m.get("name", f"Монитор {m['id']}"), m["id"])
            idx = self.monitorSelector.findData(selected)
            if idx >= 0:
                self.monitorSelector.setCurrentIndex(idx)
            self.monitorSelector.currentIndexChanged.connect(self._on_monitor_changed)
        else:
            self.monitorSelector.addItem("Основной экран", 0)
            self.monitorSelector.setEnabled(False)

    def _on_monitor_changed(self) -> None:
        import sys
        if sys.platform == "win32":
            data = self.monitorSelector.currentData()
            if isinstance(data, int):
                from src.capture.windows import set_selected_monitor
                set_selected_monitor(data)

    # -------------------------------------------------------------- calls

    def _call(self, fn: Callable[[], Any], on_done: Callable[[Any, Optional[Exception]], None]) -> None:
        """Выполнить вызов сервиса в фоне и вернуть результат в UI-поток."""
        signals = _CallSignals(self)
        signals.done.connect(on_done)
        self._pool.start(_Call(fn, signals))

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        self._update_controls(self._last_snapshot)

    # ------------------------------------------------------------ actions

    def _on_start(self) -> None:
        if self._busy:
            return
        host = self._selected_host()
        if not host:
            self.statusLabel.setText("Ошибка: не найден LAN-адрес для запуска")
            return
        self._set_busy(True)
        self.statusLabel.setText("Запуск…")

        def done(result: Any, error: Optional[Exception]) -> None:
            self._set_busy(False)
            if error is not None:
                self.statusLabel.setText(f"Ошибка: {error}")
                return
            if isinstance(result, dict):
                self._apply_snapshot(result)

        self._call(lambda: self._service.start(host, SERVER_PORT), done)

    def _on_stop(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.statusLabel.setText("Остановка…")

        def done(result: Any, error: Optional[Exception]) -> None:
            self._set_busy(False)
            if error is not None:
                self.statusLabel.setText(f"Ошибка: {error}")
                return
            self._clear_qr()

        self._call(lambda: self._service.stop(), done)

    def _on_refresh_qr(self) -> None:
        if self._busy:
            return
        snapshot = self._last_snapshot or {}
        if snapshot.get("clientConnected"):
            self.statusLabel.setText("Трансляция идёт: новый QR недоступен")
            return
        self._set_busy(True)

        def done(result: Any, error: Optional[Exception]) -> None:
            self._set_busy(False)
            if error is not None:
                self.statusLabel.setText(f"Ошибка: {error}")
                return
            if isinstance(result, dict):
                self._apply_snapshot(result)

        self._call(lambda: self._service.rotate_token(), done)

    # ------------------------------------------------------------- polling

    def _poll(self) -> None:
        if self._closed or self._busy:
            return

        def done(result: Any, error: Optional[Exception]) -> None:
            if self._closed:
                return
            if error is not None:
                self.statusLabel.setText(f"Ошибка: {error}")
                return
            if isinstance(result, dict):
                self._apply_snapshot(result)

        self._call(lambda: self._service.snapshot(), done)

    def _apply_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self._last_snapshot = snapshot
        status = str(snapshot.get("status") or "stopped")
        text = STATUS_TEXT.get(status, status)
        error = snapshot.get("error")
        if error:
            self.statusLabel.setText(f"{text}: {error}")
        else:
            self.statusLabel.setText(text)

        self._update_qr(snapshot)
        self._update_address(snapshot)
        self._update_controls(snapshot)

    def _update_qr(self, snapshot: Dict[str, Any]) -> None:
        url = snapshot.get("pairingUrl")
        if not isinstance(url, str) or not url:
            self._clear_qr()
            return
        if url == self._qr_url and self._qr_pixmap is not None:
            return
        try:
            pixmap = make_qr_pixmap(url, QR_MIN_SIZE)
        except Exception as exc:  # noqa: BLE001 - QR не должен ронять окно
            logger.warning("desktop: не удалось построить QR: %s", exc)
            self._clear_qr()
            self.qrLabel.setText("Не удалось построить QR-код")
            return
        self._qr_url = url
        self._qr_pixmap = pixmap
        self.qrLabel.setPixmap(pixmap)
        self.qrLabel.setToolTip("Наведите камеру телефона на код")

    def _clear_qr(self) -> None:
        self._qr_url = None
        self._qr_pixmap = None
        self.qrLabel.clear()
        self.qrLabel.setText("QR-код недоступен")
        self.qrLabel.setToolTip("")

    def _update_address(self, snapshot: Dict[str, Any]) -> None:
        url = snapshot.get("pairingUrl")
        if isinstance(url, str) and url:
            # Секрет не показываем в подписи — только адрес и порт.
            display = url.split("#", 1)[0]
            self.addressLabel.setText(display)
            return
        host = snapshot.get("host")
        port = snapshot.get("port")
        if host and port:
            self.addressLabel.setText(f"http://{host}:{port}/phone")
        else:
            self.addressLabel.setText("")

    def _update_controls(self, snapshot: Optional[Dict[str, Any]]) -> None:
        status = str((snapshot or {}).get("status") or "stopped")
        running = status in _RUNNING_STATUSES
        client = bool((snapshot or {}).get("clientConnected"))
        not_busy = not self._busy

        self.startButton.setEnabled(not_busy and not running and self._selected_host() is not None)
        self.stopButton.setEnabled(not_busy and (running or status == "error"))
        self.refreshQrButton.setEnabled(not_busy and running and not client)
        self.networkSelector.setEnabled(not_busy and not running)
        self.monitorSelector.setEnabled(not_busy and not running)

    # --------------------------------------------------------------- tray

    def set_tray_note(self, note: str) -> None:
        """Показывает в окне предупреждение о недоступном трее."""
        self._tray_unavailable_note = note
        if note:
            self.statusBar().showMessage(note)

    def show_and_raise(self) -> None:
        """Показать окно и вывести его на передний план."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------- tray delegates

    def stop_from_tray(self) -> None:
        """Остановить трансляцию по команде из трея (неблокирующе)."""
        self._on_stop()

    def request_quit(self) -> None:
        """Выход из приложения: остановить сервис и завершить QApplication."""
        self.shutdown()
        try:
            self._service.stop()
        except Exception as exc:  # noqa: BLE001 - выход не должен падать
            logger.warning("desktop: ошибка при остановке сервиса: %s", exc)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # -------------------------------------------------------------- close

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._tray_unavailable_note:
            self.request_quit()
            event.accept()
        else:
            self.hide()
            event.ignore()

    def shutdown(self) -> None:
        """Остановить опрос и дождаться фоновых вызовов перед выходом."""
        self._closed = True
        self._poll_timer.stop()
        self._pool.waitForDone(2000)

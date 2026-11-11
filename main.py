"""Entry point for the Qt desktop or the loopback-only web control panel."""
from pathlib import Path
import sys


def _run_web(service, web_root, web, create_panel_app):
    try:
        # run_app drains requests and closes its listener before returning.
        # BackendService owns a separate loop; stop it only after that cleanup.
        web.run_app(
            create_panel_app(service, web_root),
            host='127.0.0.1',
            port=8766,
            print=lambda _: print(
                'Панель управления: http://127.0.0.1:8766/desktop\n'
                'Для завершения нажмите Ctrl+C.',
                flush=True,
            ),
        )
        return 0
    except OSError as exc:
        print('Не удалось запустить локальную панель: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        # Also covers bind failure and cancellation during application startup.
        service.stop()


def main():
    if sys.version_info < (3, 13):
        print('Требуется Python 3.13 или новее. Текущая версия: ' + sys.version.split()[0], file=sys.stderr)
        return 1
    web_mode = '--web' in sys.argv[1:]
    try:
        from src.backend.service import BackendService
        from src.capture.factory import create_track
        if web_mode:
            from aiohttp import web
            from src.backend.web_panel import create_panel_app
        else:
            from PySide6.QtWidgets import QApplication
            from src.desktop.window import DesktopWindow
            from src.desktop.tray import DesktopTray
    except ImportError as exc:
        print('Отсутствует компонент приложения: ' + str(exc), file=sys.stderr)
        print('Установите зависимости из requirements.txt; для Linux также нужны системные пакеты.', file=sys.stderr)
        return 1
    web_root = Path(__file__).resolve().parent / 'web'
    if web_mode:
        service = BackendService(create_track, web_root)
        return _run_web(service, web_root, web, create_panel_app)

    app = QApplication(sys.argv)
    app.setApplicationName('Экран по QR')
    service = BackendService(create_track, web_root)
    window = None
    tray = None

    def shutdown():
        # Stop GUI polling before shutting down the backend event loop.
        try:
            if window is not None:
                window.shutdown()
        finally:
            try:
                service.stop()
            finally:
                if tray is not None:
                    tray.hide()

    try:
        window = DesktopWindow(service)
        tray = DesktopTray(window, service)
        app.aboutToQuit.connect(shutdown)
        app.setQuitOnLastWindowClosed(not tray.available)
        if not tray.available:
            window.set_tray_note(tray.note)
        window.show()
        return app.exec()
    finally:
        # Also clean up if GUI construction or the event loop raises.
        # Repeated stop/shutdown calls are intentional and idempotent.
        shutdown()


if __name__ == '__main__':
    raise SystemExit(main())

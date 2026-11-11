"""Read-only metadata/source probe; no installs, capture or server startup."""
import importlib.util
import json
import platform
import sys
import urllib.request

URLS = {
    'windows-capture metadata': 'https://pypi.org/pypi/windows-capture/json',
    'windows-capture source': 'https://raw.githubusercontent.com/NiiightmareXD/windows-capture/main/windows-capture-python/windows_capture/__init__.py',
    'portal ScreenCast API': 'https://raw.githubusercontent.com/flatpak/xdg-desktop-portal/main/data/org.freedesktop.portal.ScreenCast.xml',
}


def main():
    print('Python:', sys.version)
    print('OS:', platform.platform())
    for name in ('aiortc', 'av', 'PySide6', 'windows_capture', 'winrt', 'gi'):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        print('Installed:', name, bool(spec))
    for label, url in URLS.items():
        print('\nSOURCE:', label, url)
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                text = response.read().decode('utf-8')
            if url.endswith('/json'):
                data = json.loads(text)
                print('version:', data['info']['version'])
                print('requires_python:', data['info']['requires_python'])
                print('requires_dist:', data['info']['requires_dist'])
                print('wheels:', [x['filename'] for x in data['urls']])
            else:
                print(text[:55000])
        except Exception as exc:
            print(type(exc).__name__, str(exc))


if __name__ == '__main__':
    main()

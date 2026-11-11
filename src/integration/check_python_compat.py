"""Environment diagnostic: CPython 3.13+ and the declared dependencies.

Read-only: imports only, no QApplication, no servers, no capture, no subprocess.
A successful run does NOT prove video transfer or OS support; status stays not_run.

Usage: python src/integration/check_python_compat.py
Exit code 0 only when every applicable check passes.
"""
import importlib
import importlib.metadata as metadata
import platform
import sys

MIN_VERSION = (3, 13)

# (component label, real import name, package name for metadata)
COMMON_DEPS = [
    ('PySide6', 'PySide6', 'PySide6'),
    ('aiohttp', 'aiohttp', 'aiohttp'),
    ('aiortc', 'aiortc', 'aiortc'),
    ('av', 'av', 'av'),
    ('qrcode', 'qrcode', 'qrcode'),
    ('psutil', 'psutil', 'psutil'),
]
LINUX_DEPS = [
    # dbus-next is installed only on Linux; its import name differs.
    ('dbus-next', 'dbus_next', 'dbus-next'),
]
WINDOWS_DEPS = [
    ('mss', 'mss', 'mss'),
    ('dxcam', 'dxcam', 'dxcam'),
]

def installed_version(distribution):
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def check_import(label, module_name, distribution):
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        reason = str(exc) or type(exc).__name__
        print('FAIL ' + label + ': import ' + module_name + ' failed: ' + reason)
        return False
    except Exception as exc:  # noqa: BLE001 - surface any startup error, never crash
        print('FAIL ' + label + ': import ' + module_name + ' raised ' + type(exc).__name__ + ': ' + str(exc))
        return False
    try:
        version = installed_version(distribution)
    except Exception as exc:  # noqa: BLE001 - report metadata failures like import failures
        print('FAIL ' + label + ': version metadata raised ' + type(exc).__name__ + ': ' + str(exc))
        return False
    if version is None:
        print('OK ' + label + ' (version metadata unavailable)')
    else:
        print('OK ' + label + ' ' + version)
    return True


def main():
    version = platform.python_version()
    print('PYTHON ' + version)
    implementation = platform.python_implementation()
    if implementation != 'CPython':
        print('FAIL Python: expected CPython, got ' + implementation)
        print('COMPATIBILITY FAILED')
        return 1
    # Refuse before importing any dependency.
    if sys.version_info < MIN_VERSION:
        print(
            'FAIL Python: unsupported version ' + version
            + ', Python 3.13 or newer (CPython) is required'
        )
        print('COMPATIBILITY FAILED')
        return 1
    print('OK Python ' + version + ' (' + implementation + ', ' + sys.platform + ')')

    selected = list(COMMON_DEPS)
    if sys.platform.startswith('linux'):
        selected += LINUX_DEPS
    else:
        print('OK dbus-next: skipped, Linux-only dependency (sys_platform == "linux")')
    if sys.platform == 'win32':
        selected += WINDOWS_DEPS

    ok = True
    for label, module_name, distribution in selected:
        if not check_import(label, module_name, distribution):
            ok = False

    if ok:
        print('COMPATIBILITY OK')
        return 0
    print('COMPATIBILITY FAILED')
    return 1


if __name__ == '__main__':
    sys.exit(main())

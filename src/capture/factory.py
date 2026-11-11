"""One fresh capture per authorized client; no fallback test image."""
import sys


async def create_track():
    if sys.platform == 'linux':
        from .linux import create_linux_track
        return await create_linux_track()
    if sys.platform == 'win32':
        from .windows import create_windows_track
        return await create_windows_track()
    raise RuntimeError('Эта ОС не поддерживается: требуются Windows WGC или Linux ScreenCast portal.')

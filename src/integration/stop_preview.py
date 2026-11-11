"""Stop only this workspace's test preview launchers."""
from pathlib import Path
import psutil
root = Path(__file__).resolve().parents[2]
for process in psutil.process_iter(['pid', 'cmdline', 'cwd']):
    try:
        args = process.info['cmdline'] or []
        cwd = process.info['cwd']
        if ('src/integration/preview_phone.py' in args
                and cwd is not None
                and Path(cwd).resolve() == root):
            process.terminate()
            process.wait(timeout=5)
            print('Stopped project preview', process.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

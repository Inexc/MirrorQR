"""Windows desktop capture via dxcam (DirectX GPU) with mss (GDI CPU) fallback."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
import av

log = logging.getLogger(__name__)

_selected_monitor_id: int = 0


def ensure_desktop() -> None:
    """Ensure the calling thread is attached to the interactive 'default' desktop."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:  # noqa: BLE001
        pass


def list_monitors() -> List[Dict[str, Any]]:
    """Enumerate available displays/monitors on Windows."""
    ensure_desktop()
    result: List[Dict[str, Any]] = []

    try:
        import mss

        with mss.MSS() as sct:
            for idx, mon in enumerate(sct.monitors[1:]):
                is_prim = bool(mon.get("is_primary", False))
                w = int(mon.get("width", 0))
                h = int(mon.get("height", 0))
                label = f"Монитор {idx + 1} ({w}x{h})" + (" - Основной" if is_prim else "")
                result.append({
                    "id": idx,
                    "name": label,
                    "width": w,
                    "height": h,
                    "is_primary": is_prim,
                })
    except Exception:  # noqa: BLE001
        log.exception("mss enumeration failed, using Win32 fallback")

    if not result:
        # Fallback to primary screen metrics if mss failed
        try:
            user32 = ctypes.windll.user32
            w = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN
            h = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN
            result.append({
                "id": 0,
                "name": f"Монитор 1 ({w}x{h}) - Основной",
                "width": w,
                "height": h,
                "is_primary": True,
            })
        except Exception:  # noqa: BLE001
            result.append({
                "id": 0,
                "name": "Основной монитор",
                "width": 1920,
                "height": 1080,
                "is_primary": True,
            })

    return result


def get_selected_monitor() -> int:
    global _selected_monitor_id
    return _selected_monitor_id


def set_selected_monitor(monitor_id: int) -> None:
    global _selected_monitor_id
    if not isinstance(monitor_id, int):
        try:
            monitor_id = int(monitor_id)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid monitor_id: {monitor_id}") from None
    _selected_monitor_id = monitor_id


class WindowsCaptureTrack(VideoStreamTrack):
    """aiortc VideoStreamTrack capturing a Windows desktop via dxcam or mss."""

    def __init__(self, monitor_id: int = 0) -> None:
        super().__init__()
        self.monitor_id = monitor_id
        self.backend_name = "unknown"
        self._camera: Optional[Any] = None
        self._sct: Optional[Any] = None
        self._mon: Optional[Dict[str, Any]] = None
        self._last_frame: Optional[Any] = None

        self._init_capture()

    def _init_capture(self) -> None:
        ensure_desktop()
        # 1. Attempt dxcam GPU capture
        try:
            import dxcam

            cam = dxcam.create(
                device_idx=0,
                output_idx=self.monitor_id,
                output_color="BGRA",
                processor_backend="numpy",
            )
            if not cam.is_capturing:
                cam.start(target_fps=30, video_mode=True)
            self._camera = cam
            self.backend_name = "dxcam"
            log.info("Initialized dxcam capture on monitor %d", self.monitor_id)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("dxcam initialization failed, falling back to mss: %s", exc)

        # 2. Fallback to mss CPU capture
        try:
            import mss

            sct = mss.MSS()
            mon_count = len(sct.monitors) - 1
            if mon_count < 1:
                raise RuntimeError("No monitors detected by mss")
            mon_idx = min(max(0, self.monitor_id), mon_count - 1)
            self._mon = sct.monitors[mon_idx + 1]
            self._sct = sct
            self.backend_name = "mss"
            log.info("Initialized mss fallback capture on monitor %d", self.monitor_id)
        except Exception as exc:
            log.exception("mss fallback capture initialization failed")
            raise RuntimeError(f"Не удалось инициализировать захват экрана Windows: {exc}") from exc

    async def recv(self) -> av.VideoFrame:
        if self.readyState != "live":
            raise MediaStreamError

        pts, time_base = await self.next_timestamp()

        if self.backend_name == "dxcam" and self._camera is not None:
            frame_arr = self._camera.get_latest_frame()
            if frame_arr is None:
                if self._last_frame is not None:
                    frame_arr = self._last_frame
                else:
                    await asyncio.sleep(0.01)
                    frame_arr = self._camera.get_latest_frame()

            if frame_arr is not None:
                self._last_frame = frame_arr
                frame = av.VideoFrame.from_ndarray(frame_arr, format="bgra")
            else:
                w = self._camera.width or 1920
                h = self._camera.height or 1080
                frame = av.VideoFrame(width=w, height=h, format="bgra")
        elif self.backend_name == "mss" and self._sct is not None and self._mon is not None:
            def _grab_call():
                ensure_desktop()
                return self._sct.grab(self._mon)

            img = await asyncio.to_thread(_grab_call)
            frame = av.VideoFrame(img.width, img.height, "bgra")
            frame.planes[0].update(img.raw)
        else:
            raise MediaStreamError

        frame.pts = pts
        frame.time_base = time_base
        return frame

    def stop(self) -> None:
        if self.readyState == "ended":
            return
        super().stop()
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:  # noqa: BLE001
                pass
            self._camera = None
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None


async def create_windows_track() -> WindowsCaptureTrack:
    monitor_id = get_selected_monitor()
    return WindowsCaptureTrack(monitor_id=monitor_id)

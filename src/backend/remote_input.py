"""Remote mouse and keyboard input handling for mobile client."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import sys
import time
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Win32 mouse event flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
WHEEL_DELTA = 120


def _get_target_monitor_bounds() -> Tuple[int, int, int, int]:
    """Return (left, top, width, height) of the currently selected capture monitor."""
    if sys.platform != "win32":
        return (0, 0, 1920, 1080)

    try:
        from src.capture.windows import ensure_desktop, get_selected_monitor
        ensure_desktop()
        mon_id = get_selected_monitor()
        import mss
        with mss.MSS() as sct:
            if 0 <= mon_id < len(sct.monitors) - 1:
                m = sct.monitors[mon_id + 1]
                return (int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]))
    except Exception:  # noqa: BLE001
        pass

    try:
        user32 = ctypes.windll.user32
        w = int(user32.GetSystemMetrics(0))
        h = int(user32.GetSystemMetrics(1))
        return (0, 0, w, h)
    except Exception:  # noqa: BLE001
        return (0, 0, 1920, 1080)


def handle_input(data: Dict[str, Any]) -> None:
    """Process an input message from the phone WebSocket client.

    Expected format:
      {"type": "input", "action": "move"|"click"|"down"|"up"|"dblclick"|"right_click"|"wheel",
       "x": float, "y": float, "button": "left"|"right"|"middle", "delta": int}
    """
    if sys.platform != "win32":
        return

    try:
        from src.capture.windows import ensure_desktop
        ensure_desktop()
    except Exception:  # noqa: BLE001
        pass

    action = str(data.get("action", "")).strip().lower()
    if not action:
        return

    try:
        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001
        return

    # Map normalized coordinates (0.0..1.0) to screen pixel coordinates
    left, top, width, height = _get_target_monitor_bounds()
    raw_x = data.get("x")
    raw_y = data.get("y")

    target_x: Optional[int] = None
    target_y: Optional[int] = None
    if raw_x is not None and raw_y is not None:
        try:
            fx = float(raw_x)
            fy = float(raw_y)
            fx = max(0.0, min(1.0, fx))
            fy = max(0.0, min(1.0, fy))
            target_x = int(left + fx * width)
            target_y = int(top + fy * height)
            target_x = max(left, min(left + width - 1, target_x))
            target_y = max(top, min(top + height - 1, target_y))
        except (ValueError, TypeError):
            pass

    button = str(data.get("button", "left")).strip().lower()

    try:
        if target_x is not None and target_y is not None:
            user32.SetCursorPos(target_x, target_y)

        if action == "move":
            return

        if action == "click":
            if button == "right":
                user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            elif button == "middle":
                user32.mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
            else:
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        elif action == "dblclick":
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        elif action == "right_click":
            user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

        elif action == "down":
            if button == "right":
                user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            elif button == "middle":
                user32.mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
            else:
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

        elif action == "up":
            if button == "right":
                user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            elif button == "middle":
                user32.mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
            else:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        elif action == "wheel":
            delta = data.get("delta", 0)
            fdelta = float(delta)
            if abs(fdelta) <= 10:
                wheel_val = int(fdelta * WHEEL_DELTA)
            else:
                wheel_val = int(fdelta)
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_val, 0)
    except Exception:  # noqa: BLE001
        log.exception("Error executing input action: %s", action)

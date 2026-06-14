"""Manual test helper for opt_feature_notifications.

Flashes a chosen window's taskbar button (the same "needs attention" signal
the overlay listens for), so you can watch the workspace indicator appear.

Usage (run from the repo root):
  py .\tests\flash_test.py             # lists visible windows + their desktop number
  py .\tests\flash_test.py <hwnd>      # flash that window for ~5s
  py .\tests\flash_test.py <hwnd> stop # stop flashing that window
"""
import sys
import ctypes
from ctypes import wintypes

import win32gui

try:
    from pyvda import AppView
except Exception:
    AppView = None

FLASHW_STOP = 0
FLASHW_TRAY = 0x00000002
FLASHW_TIMERNOFG = 0x0000000C


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def desktop_number(hwnd):
    if AppView is None:
        return "?"
    try:
        return AppView(hwnd=hwnd).desktop.number
    except Exception:
        return "-"


def list_windows():
    rows = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                rows.append((hwnd, desktop_number(hwnd), title))
        return True

    win32gui.EnumWindows(cb, None)
    print(f"{'HWND':>8}  {'WS':>3}  TITLE")
    for hwnd, ws, title in rows:
        print(f"{hwnd:>8}  {ws!s:>3}  {title[:60]}")
    print("\nFlash one with:  py .\\tests\\flash_test.py <hwnd>")


def flash(hwnd, stop=False):
    flags = FLASHW_STOP if stop else (FLASHW_TRAY | FLASHW_TIMERNOFG)
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, flags, 0 if stop else 6, 0)
    ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    title = win32gui.GetWindowText(hwnd)
    action = "stopped flashing" if stop else "flashing"
    print(f"{action} {hwnd} (workspace {desktop_number(hwnd)}): {title[:60]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        list_windows()
    else:
        target = int(sys.argv[1], 0)
        flash(target, stop=(len(sys.argv) > 2 and sys.argv[2].lower() == "stop"))

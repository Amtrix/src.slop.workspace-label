"""Throwaway probe: list audio sessions, peaks, and the desktop they map to."""
import win32gui
import win32process
import psutil
from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
from pyvda import AppView


def pid_to_windows(pid):
    found = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        _, wpid = win32process.GetWindowThreadProcessId(hwnd)
        if wpid == pid:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


def ancestor_pids(pid):
    chain = [pid]
    try:
        proc = psutil.Process(pid)
        for _ in range(6):
            parent = proc.parent()
            if parent is None:
                break
            chain.append(parent.pid)
            proc = parent
    except Exception:
        pass
    return chain


def desktop_of_window(hwnd):
    try:
        return AppView(hwnd=hwnd).desktop.number
    except Exception:
        return None


sessions = AudioUtilities.GetAllSessions()
print(f"sessions: {len(sessions)}")
for s in sessions:
    if not s.Process:
        continue
    try:
        meter = s._ctl.QueryInterface(IAudioMeterInformation)
        peak = meter.GetPeakValue()
    except Exception as e:
        peak = -1
    try:
        state = s._ctl.GetState()
    except Exception:
        state = -1
    name = s.Process.name()
    pid = s.Process.pid
    # find a window for this pid, else walk up parents
    desktops = set()
    for p in ancestor_pids(pid):
        for hwnd in pid_to_windows(p):
            d = desktop_of_window(hwnd)
            if d:
                desktops.add(d)
        if desktops:
            break
    print(f"  {name:30s} pid={pid:6d} state={state} peak={peak:.4f} desktops={sorted(desktops)}")

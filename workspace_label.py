import json
import os
import queue
import sys
import threading
import time
import ctypes
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter import font as tkfont
import win32api
import win32gui
import win32con
import win32process
from pyvda import AppView, VirtualDesktop, get_virtual_desktops

# Optional audio backend for the "music highlight" feature. Imported lazily so
# the app still runs (feature simply stays off) if these packages are missing.
try:
    import psutil
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    _AUDIO_AVAILABLE = True
except Exception:
    _AUDIO_AVAILABLE = False

APP_NAME = "Desktop Labeller"
ICON_FILE = "desktop_labeller.ico"
DEFAULT_NAMES = ["Work", "Web", "Chat", "Media"]
DEFAULT_FONT_RGBA = [85, 68, 34, 1.0]
# Background painted behind each (non-active) workspace label. This must be a
# color other than COLOR_BG, otherwise -transparentcolor makes it click-through.
# Defaults to a near-black surface that is effectively invisible yet clickable.
DEFAULT_SURFACE_RGBA = [2, 2, 2, 1.0]
DEFAULT_SIZE_SCALE = 1.0
# Optional toolbar features. Each maps a config key to its default settings.
DEFAULT_MOVE_WINDOW_LABEL = "Move Window"
# While move mode is armed, the button text walks through these two phases.
MOVE_SELECT_WINDOW_LABEL = "Select window."
MOVE_SELECT_WORKSPACE_LABEL = "Select workspace."
DEFAULT_PIN_WINDOW_LABEL = "Pin Window"
DEFAULT_UNPIN_WINDOW_LABEL = "Unpin Window"
# Glyph shown for a shortcut that does not specify (or fails to load) an icon.
PLACEHOLDER_SHORTCUT_ICON = "\U0001F517"  # link symbol
# Optional per-workspace notification indicator (taskbar-flash "needs attention").
DEFAULT_NOTIFICATION_INDICATOR = "\u25CF"  # filled circle
DEFAULT_NOTIFICATION_COLOR = "#FF3333"
# Optional per-workspace "music playing here" indicator (opt_feature_musichighlight).
# A small monochrome music note (not the color speaker emoji) so it blends with
# the overlay's minimal amber palette. It only appends a glyph and never changes
# the label's text colour.
DEFAULT_MUSIC_INDICATOR = "\u266A"  # eighth note ♪
# How often the audio sessions are polled while the music feature is enabled.
MUSIC_POLL_MS = 1000
# Peak amplitude (0..1) above which an Active audio session counts as "playing";
# ignores the near-silent noise floor of paused/idle sessions.
MUSIC_PEAK_THRESHOLD = 0.0005
# Poll cycles a desktop keeps its speaker after audio drops, so the glyph does
# not flicker during brief silent gaps (e.g. between tracks).
MUSIC_GRACE_CYCLES = 2
# Optional countdown-timer component (opt_component_feature_timer).
TIMER_POLL_MS = 1000               # how often the live countdown digits refresh
TIMER_REMOVE_GLYPH = "\u2715"      # ✕ per-row remove button
# Per-row media-control icons drawn in the Segoe MDL2 Assets symbol font
# (shipped on Windows 10/11), whose private-use codepoints are proper media
# glyphs rather than text characters that fall back to tofu boxes.
TIMER_ICON_FONT = "Segoe MDL2 Assets"
TIMER_PAUSE_GLYPH = "\uE769"       # Pause (running timer)
TIMER_RESUME_GLYPH = "\uE768"      # Play / resume (paused timer)
TIMER_RESTART_GLYPH = "\uE72C"     # Refresh / restart to full duration
TIMER_ADD_LABEL = "ADD"
TIMER_CLEAR_LABEL = "CLEAR"
TIMER_EXPIRED_COLOR = "#FF3333"    # text colour once a timer hits 00:00:00
DEFAULT_TIMER_DURATION = "00:05:00"  # pre-filled duration in the ADD dialog
TIMER_FLASH_MS = 550               # blink interval for an expired-timer label
TIMER_FLASH_FG = "#FFFFFF"         # text colour during the "on" flash phase
LEGACY_LOCAL_CONFIG_FILE = Path("desktops.txt")
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
LEGACY_APPDATA_CONFIG_FILE = CONFIG_DIR / "desktops.txt"
CONFIG_FILE = CONFIG_DIR / "desktops.json"
# Countdown timers persist here so they keep counting (in real time) across
# restarts, independent of the user-edited desktops.json config.
TIMERS_FILE = CONFIG_DIR / "timers.json"
STARTUP_VISIBLE_MS = 2500
# Optional "hide while idle" feature (opt_feature_hide_when_idle): default
# seconds of no keyboard/mouse input before the whole overlay is withdrawn.
DEFAULT_IDLE_HIDE_SECONDS = 30.0
# Poll cadence (ms) while the overlay is hidden for idle, so it reappears
# promptly once the user touches the keyboard or mouse again.
IDLE_HIDDEN_POLL_MS = 500
TRAY_UID = 1
WM_TRAYICON = win32con.WM_USER + 20
# Custom message we post to the tray window (from any thread) to ask its
# dedicated message-pump thread to remove the icon and quit cleanly.
WM_TRAY_QUIT = win32con.WM_USER + 21
# Shell-hook notification codes (wparam of the registered SHELLHOOK message).
HSHELL_WINDOWACTIVATED = 4
HSHELL_RUDEAPPACTIVATED = 0x8004
HSHELL_FLASH = 0x8006
MENU_OPEN_CONFIG = 1001
MENU_SHOW_OVERLAY = 1002
MENU_EXIT = 1003


class LASTINPUTINFO(ctypes.Structure):
    """ctypes mirror of the Win32 LASTINPUTINFO struct for GetLastInputInfo."""
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def system_idle_seconds():
    """Seconds since the last system-wide keyboard or mouse input.

    Uses GetLastInputInfo, which reports the tick count of the most recent
    input event across the whole session. Returns 0.0 on any failure so the
    caller simply treats the user as active.
    """
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint
        now = ctypes.windll.kernel32.GetTickCount()
        # Both are unsigned 32-bit DWORDs; mask the difference so a GetTickCount
        # wrap (~49.7 days of uptime) never yields a spurious negative idle.
        elapsed_ms = (now - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except Exception:
        return 0.0


# Overlay Color Palette
COLOR_BG = "#010101"
# Near-black background that is NOT the -transparentcolor key, so the whole
# label rectangle (including padding) stays clickable while remaining nearly
# invisible. The keyed COLOR_BG is transparent to Windows hit-testing.
COLOR_HIT_BG = "#020202"
COLOR_TEXT_DIM = "#554422"    # Very dark, low-intensity amber (safe for OLED)
COLOR_TEXT_ACTIVE = "#FFB300" # Muted gold for the active workspace highlight
COLOR_ACTIVE_BG = "#221100"   # Extremely dark brown highlight box background
COLOR_BAD_CONFIG = "#FF3333"
COLOR_BAD_BG = "#220000"
SHORTCUT_SEPARATOR_COLOR = "#554422"  # Dim amber divider between shortcut entries


def blend_hex(fg, bg, opacity):
    """Blends hex colour ``fg`` over ``bg`` at the given opacity (0.0-1.0).

    Used to fake per-widget transparency: Tk's ``-alpha`` attribute only dims
    the whole window, so the timer digits are instead composited toward their
    own background to honour ``time_opacity`` without fading the rest of the
    overlay. ``opacity`` 1.0 returns ``fg`` unchanged; 0.0 returns ``bg``.
    """
    try:
        opacity = max(0.0, min(1.0, float(opacity)))
    except (TypeError, ValueError):
        return fg
    if opacity >= 1.0:
        return fg
    fr, fg_, fb = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
    br, bg_, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    r = round(fr * opacity + br * (1.0 - opacity))
    g = round(fg_ * opacity + bg_ * (1.0 - opacity))
    b = round(fb * opacity + bb * (1.0 - opacity))
    return f"#{r:02X}{g:02X}{b:02X}"


class OverlayPanel:
    """Per-monitor overlay window plus its own widget references."""

    def __init__(self, win, monitor_rect):
        self.win = win
        self.monitor_rect = monitor_rect  # (left, top, right, bottom)
        # Full normalized config rendered on this monitor (set before build).
        self.config = None
        self.font_color = COLOR_TEXT_DIM
        self.surface_color = COLOR_HIT_BG
        self.size_scale = DEFAULT_SIZE_SCALE
        self.features = {}
        self.notification_settings = None
        self.music_settings = None
        self.pin_labels = {
            "label_pin": DEFAULT_PIN_WINDOW_LABEL,
            "label_unpin": DEFAULT_UNPIN_WINDOW_LABEL,
        }
        # Idle text for this panel's "move window" toolbar button.
        self.move_label = DEFAULT_MOVE_WINDOW_LABEL
        self.label_widgets = {}
        self.label_base_text = {}
        # Per-desktop-index text color override (1-based idx -> hex), empty
        # when every label uses the panel's default font color.
        self.name_colors = {}
        self.marked_desktops = set()
        self.music_marked = set()
        self.move_button = None
        self.pin_button = None
        self.highlighted_desktop_num = None
        # Live toolbar clock label (opt_toolbar_feature_date_and_time), or None.
        self.datetime_label = None
        self.toolbar_frame = None
        self.shortcut_icons = []
        # Cached shortcut frame + config so the grid can be rebuilt on desktop
        # change when entries are filtered by the "workspaces" property.
        self.shortcuts_frame = None
        self.shortcuts_config = None
        # Cached countdown-timer box frame + config (same rebuild pattern as the
        # shortcut grid). timer_value_labels maps a timer id -> its live digits
        # Label, so only those small widgets are reconfigured each second.
        self.timer_frame = None
        self.timer_config = None
        self.timer_value_labels = {}
        self.hwnd = None

    def get_hwnd(self):
        if self.hwnd:
            return self.hwnd
        try:
            self.hwnd = int(self.win.wm_frame(), 16)
        except Exception:
            try:
                self.hwnd = win32gui.FindWindow(None, self.win.title())
            except Exception:
                self.hwnd = None
        return self.hwnd


class WorkspaceOverlay:
    def __init__(self):
        self.root = tk.Tk()

        # Per-monitor overlay windows. panels[0] always wraps self.root.
        self.panels = [OverlayPanel(self.root, None)]
        # Signature of the raw monitor layout, so the update loop can detect a
        # monitor hot-plug/unplug and rebuild the windows.
        self.raw_monitor_signature = None

        # Optional "move window" toolbar feature state (global across monitors).
        self.move_mode = False
        # While move mode is armed, the window the user picked to move (None
        # until they focus one), plus the window that was focused when move
        # mode was armed (used to detect that pick).
        self.move_selected_hwnd = None
        self.move_baseline_hwnd = None
        # Last external (non-overlay) foreground window; the pin/move actions
        # and the pin button's displayed state both track this window.
        self.tracked_hwnd = None
        self.tracked_pinned = None
        # Last foreground window seen while pinned to background; used to avoid
        # re-issuing SetWindowPos (and its recomposite/jitter) every tick.
        self.last_foreground_hwnd = None
        # Per-workspace notification feature state. Enabled if ANY rendered
        # config turns it on; the indicator/colour itself is per panel.
        self.notifications_enabled = False
        self.notified_desktops = set()
        # Per-workspace "music playing" feature state. Enabled if ANY rendered
        # config turns it on AND the audio backend imported successfully.
        self.music_enabled = False
        self.playing_desktops = set()
        # desktop number -> remaining grace cycles (anti-flicker hysteresis).
        self.music_grace = {}
        # Countdown timers (opt_component_feature_timer), shared across panels
        # and persisted to disk so they keep counting in real time. Each entry:
        # {"id": int, "name": str, "end_epoch": float}.
        self.timers = self.load_timers()
        self.next_timer_id = (
            max((t["id"] for t in self.timers), default=0) + 1
        )
        # Desktop numbers whose label is currently flashing because a timer
        # expired; cleared per desktop when the user clicks that label. Global
        # (survives panel rebuilds) and applied to every monitor's labels.
        self.flashing_desktops = set()
        self.flash_on = False
        self.flash_after_id = None
        self.last_active_desktop = None
        self.wm_shellhook = None
        self.config_mtime = None
        self.displayed_desktop_count = 0
        self.workspace_font_color = COLOR_TEXT_DIM
        self.workspace_surface_color = COLOR_HIT_BG
        self.workspace_size_scale = DEFAULT_SIZE_SCALE
        self.pin_to_background = False
        # Pending "return to background" timer id, so repeated overlay shows do
        # not stack multiple scheduled callbacks.
        self.background_after_id = None
        # True while the overlay is hidden because a fullscreen app (e.g. a
        # game) owns the screen; lets us avoid touching the window/compositor.
        self.hidden_for_fullscreen = False
        # Optional "hide while idle" feature: when enabled the whole overlay is
        # withdrawn after idle_hide_seconds of no input, and restored on the
        # next keypress/mouse move. Enabled if ANY rendered config declares it.
        self.idle_hide_enabled = False
        self.idle_hide_seconds = DEFAULT_IDLE_HIDE_SECONDS
        self.hidden_for_idle = False
        # Tracks whether the panel windows are currently withdrawn, so the
        # update loop only issues withdraw/deiconify on an actual state change.
        self.windows_withdrawn = False
        self.tray_hwnd = None
        self.tray_wndclass = None
        self.tray_class_atom = None
        self.tray_icon = None
        self.tray_installed = False
        self.tray_thread = None
        # Actions requested from the tray thread are marshalled back to the Tk
        # thread through this queue (Tk is not thread-safe).
        self.tray_action_queue = queue.Queue()
        # Sent by the shell whenever the taskbar is (re)created, e.g. after an
        # Explorer crash/restart. We must re-add our notification icon then,
        # otherwise it silently disappears and clicks "do nothing".
        try:
            self.wm_taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        except Exception:
            self.wm_taskbar_created = None

        # Ensure default configuration template exists
        self.ensure_config_exists()

        # Create/position the per-monitor windows from the config, then build
        # the interactive content into each of them.
        self.build_workspace_list()

        # 2. Apply Windows stack styling (Pins it to desktop background)
        self.root.after(10, self.apply_window_styles)
        self.root.after(20, self.start_tray_thread)
        self.root.after(50, self.drain_tray_actions)
        self.schedule_background_mode()
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

        # 3. Start monitoring active desktop state
        self.update_loop()
        self.track_desktop_loop()
        self.music_loop()
        self.timer_loop()

    def style_window(self, win):
        """Applies the borderless, transparent, top-most overlay styling."""
        win.overrideredirect(True)
        win.configure(bg=COLOR_BG)
        try:
            win.attributes("-transparentcolor", COLOR_BG)
        except tk.TclError:
            pass
        win.attributes("-topmost", True)
        icon_path = self.resource_path(ICON_FILE)
        if os.path.exists(icon_path):
            try:
                win.iconbitmap(icon_path)
            except Exception:
                pass

    def position_window(self, win, monitor_rect, offset=(0, 0)):
        """Positions a window at the top edge of its monitor, plus offset.

        ``offset`` is an ``(x, y)`` pixel shift from the monitor's top-left
        corner, configurable per ``desktop`` block via the ``offset`` key.
        """
        left, top = (monitor_rect[0], monitor_rect[1]) if monitor_rect else (0, 0)
        offset_x, offset_y = offset
        win.geometry(f"+{left + offset_x}+{top + offset_y}")

    def get_monitor_rects(self):
        """Returns monitor rectangles ordered left-to-right, top-to-bottom."""
        rects = []
        try:
            for monitor in win32api.EnumDisplayMonitors():
                info = win32api.GetMonitorInfo(monitor[0])
                rects.append(tuple(info["Monitor"]))
        except Exception:
            rects = []
        if not rects:
            try:
                rects = [(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())]
            except Exception:
                rects = [(0, 0, 1920, 1080)]
        rects.sort(key=lambda r: (r[0], r[1]))
        return rects

    def parse_desktop_key(self, key):
        """Parses a ``desktop`` scope key into a render target.

        Returns:
          * ``"all"`` for a bare ``desktop`` (or ``desktop:`` with no valid
            indices), meaning render on every monitor.
          * a list of 1-based indices for ``desktop:1,2,3``.
          * ``None`` when the key is not a ``desktop`` scope key at all.
        """
        if not isinstance(key, str) or key.split(":", 1)[0] != "desktop":
            return None
        if ":" not in key:
            return "all"
        indices = []
        for part in key.split(":", 1)[1].split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value >= 1:
                indices.append(value)
        return indices or "all"

    def resolve_render_targets(self):
        """Returns an ordered list of ``(monitor_rect, config)`` to render.

        Two config layouts are supported:
          * Flat: the whole object is one config rendered on every monitor.
          * Scoped: any top-level ``desktop:i,j`` key maps a full config onto
            the given 1-based monitor indices, and only listed monitors render.
            When the same index appears in several keys, the later key wins.
        """
        self.ensure_config_exists()
        rects = self.get_monitor_rects()
        primary = rects[0]

        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8")
            top = json.loads(self.strip_jsonc_comments(raw))
            if not isinstance(top, dict):
                raise ValueError()
        except Exception:
            return [(primary, self.bad_config())]

        scoped = {}  # 1-based monitor index -> parsed config
        has_scoped_key = False
        for key, value in top.items():
            indices = self.parse_desktop_key(key)
            if indices is None:
                continue
            has_scoped_key = True
            parsed = self.parse_single_config(value)
            target_indices = range(1, len(rects) + 1) if indices == "all" else indices
            for index in target_indices:
                scoped[index] = parsed

        if not has_scoped_key:
            # Flat config: render the same thing on every monitor.
            config = self.parse_single_config(top)
            return [(rect, config) for rect in rects]

        targets = [
            (rects[index - 1], scoped[index])
            for index in sorted(scoped)
            if 1 <= index <= len(rects)
        ]
        if not targets:
            # Scoped keys present but none map to a connected monitor; show the
            # first defined block on the primary monitor so the overlay (and its
            # config gear) stays reachable.
            first = scoped[sorted(scoped)[0]]
            return [(primary, first)]
        return targets

    def sync_panels(self, targets):
        """(Re)creates per-monitor windows to match the ``(rect, config)`` targets."""
        # Tear down any extra windows beyond the primary.
        for panel in self.panels[1:]:
            try:
                panel.win.destroy()
            except Exception:
                pass
        self.panels = self.panels[:1]

        # Primary panel reuses self.root.
        rect0, config0 = targets[0]
        self.style_window(self.root)
        self.panels[0].monitor_rect = rect0
        self.panels[0].config = config0
        self.panels[0].hwnd = None
        self.position_window(self.root, rect0, config0.get("offset", (0, 0)))

        # Secondary monitors get their own Toplevel windows.
        for rect, config in targets[1:]:
            win = tk.Toplevel(self.root)
            self.style_window(win)
            self.position_window(win, rect, config.get("offset", (0, 0)))
            panel = OverlayPanel(win, rect)
            panel.config = config
            self.panels.append(panel)

        self.raw_monitor_signature = tuple(self.get_monitor_rects())

    def panel_hwnds(self):
        """Set of native handles for all current overlay windows."""
        handles = set()
        for panel in self.panels:
            hwnd = panel.get_hwnd()
            if hwnd:
                handles.add(hwnd)
        return handles

    def ensure_config_exists(self):
        if CONFIG_FILE.exists():
            return

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            names = self.read_legacy_names(LEGACY_APPDATA_CONFIG_FILE)
            if not names:
                names = self.read_legacy_names(LEGACY_LOCAL_CONFIG_FILE)
            self.write_config(names or DEFAULT_NAMES)
        except Exception:
            pass

    def read_legacy_names(self, path):
        if not path.exists():
            return []

        try:
            return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:
            return []

    def write_config(self, names):
        config = {
            "names": names,
            "font_rgba": DEFAULT_FONT_RGBA,
            "surface_rgba": DEFAULT_SURFACE_RGBA,
            "size_scale": DEFAULT_SIZE_SCALE,
        }
        CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def get_window_handle(self):
        """Native handle of the primary overlay window (panels[0])."""
        return self.panels[0].get_hwnd()

    def resource_path(self, name):
        """Resolves a bundled resource path for both source and frozen runs."""
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, name)

    def apply_window_icon(self):
        """Sets the overlay window/taskbar icon to the bundled .ico if present."""
        icon_path = self.resource_path(ICON_FILE)
        if not os.path.exists(icon_path):
            return
        try:
            self.root.iconbitmap(icon_path)
        except Exception:
            pass

    def load_tray_icon(self):
        """Loads the custom tray icon, falling back to the default app icon."""
        icon_path = self.resource_path(ICON_FILE)
        if os.path.exists(icon_path):
            try:
                return win32gui.LoadImage(
                    0,
                    icon_path,
                    win32con.IMAGE_ICON,
                    0,
                    0,
                    win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
                )
            except Exception:
                pass
        return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def get_desktop_count(self):
        try:
            return len(get_virtual_desktops())
        except Exception:
            return 4

    def bad_config(self):
        """Normalized config used when an object fails to parse."""
        return {
            "bad_format": True,
            "names": [],
            "name_colors": [],
            "font_color": COLOR_BAD_CONFIG,
            "surface_color": COLOR_HIT_BG,
            "size_scale": DEFAULT_SIZE_SCALE,
            "offset": (0, 0),
            "features": {},
        }

    def parse_offset(self, value):
        """Returns an ``(x, y)`` pixel offset from the monitor's top-left edge.

        Accepts an object with ``X``/``Y`` (case-insensitive) integer keys.
        Missing or invalid values default to ``0``.
        """
        if not isinstance(value, dict):
            return (0, 0)

        def channel(*keys):
            for key in keys:
                raw = value.get(key)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                return int(raw)
            return 0

        return (channel("X", "x"), channel("Y", "y"))

    def parse_workspaces(self, value):
        """Parses a comma-separated string of 1-based desktop numbers.

        Returns a set of ints for ``"1,2,3"`` style values, or ``None`` when the
        value is absent/empty/invalid (meaning the entry shows on every desktop).
        """
        if not isinstance(value, str) or not value.strip():
            return None
        result = set()
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.add(int(part))
            except ValueError:
                continue
        return result or None

    def parse_single_config(self, config):
        """Validates one config object into the normalized render dict."""
        try:
            if not isinstance(config, dict):
                raise ValueError()

            names_raw = config.get("names")
            if not isinstance(names_raw, list):
                raise ValueError()
            # Optional default text color applied to every label, which any
            # individual entry can still override.
            raw_names_color = config.get("names_color")
            default_name_color = (
                self.parse_border_color(raw_names_color)
                if raw_names_color is not None
                else None
            )
            # Each entry is either a plain string, or an object
            # {"name": "...", "font_rgba"/"color": <rgba list or hex>} that
            # overrides the per-label text color.
            names = []
            name_colors = []
            for entry in names_raw:
                if isinstance(entry, str):
                    names.append(entry.strip())
                    name_colors.append(default_name_color)
                elif isinstance(entry, dict):
                    name = entry.get("name", "")
                    if not isinstance(name, str):
                        raise ValueError()
                    names.append(name.strip())
                    raw_color = entry.get("font_rgba", entry.get("color"))
                    name_colors.append(
                        self.parse_border_color(raw_color)
                        if raw_color is not None
                        else default_name_color
                    )
                else:
                    raise ValueError()

            font_rgba = config.get("font_rgba", DEFAULT_FONT_RGBA)
            surface_rgba = config.get("surface_rgba", DEFAULT_SURFACE_RGBA)
            size_scale = self.parse_size_scale(config.get("size_scale", DEFAULT_SIZE_SCALE))
            return {
                "bad_format": False,
                "names": names,
                "name_colors": name_colors,
                "font_color": self.rgba_to_hex(font_rgba),
                "surface_color": self.rgba_to_hex(surface_rgba),
                "size_scale": size_scale,
                "offset": self.parse_offset(config.get("offset")),
                "features": self.parse_optional_features(config),
            }
        except Exception:
            return self.bad_config()

    def strip_jsonc_comments(self, text):
        """Removes `//` line comments so the config can be parsed as JSONC.

        Comments inside string literals are preserved so values such as URLs
        are left untouched.
        """
        result = []
        in_string = False
        escaped = False
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if in_string:
                result.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                index += 1
                continue
            if char == '"':
                in_string = True
                result.append(char)
                index += 1
                continue
            if char == "/" and index + 1 < length and text[index + 1] == "/":
                while index < length and text[index] not in "\r\n":
                    index += 1
                continue
            result.append(char)
            index += 1
        return "".join(result)

    def parse_optional_features(self, config):
        """Reads optional toolbar features declared in the config object."""
        features = {}

        move_window = config.get("opt_toolbar_feature_movewindow")
        if move_window is not None and move_window is not False:
            settings = move_window if isinstance(move_window, dict) else {}
            label = settings.get("label", DEFAULT_MOVE_WINDOW_LABEL)
            if not isinstance(label, str) or not label.strip():
                label = DEFAULT_MOVE_WINDOW_LABEL
            features["movewindow"] = {"label": label}

        pin_window = config.get("opt_toolbar_feature_pinwindow")
        if pin_window is not None and pin_window is not False:
            settings = pin_window if isinstance(pin_window, dict) else {}
            label_pin = settings.get("label_pin", DEFAULT_PIN_WINDOW_LABEL)
            label_unpin = settings.get("label_unpin", DEFAULT_UNPIN_WINDOW_LABEL)
            if not isinstance(label_pin, str) or not label_pin.strip():
                label_pin = DEFAULT_PIN_WINDOW_LABEL
            if not isinstance(label_unpin, str) or not label_unpin.strip():
                label_unpin = DEFAULT_UNPIN_WINDOW_LABEL
            features["pinwindow"] = {"label_pin": label_pin, "label_unpin": label_unpin}

        date_time = config.get("opt_toolbar_feature_date_and_time")
        if date_time is not None and date_time is not False:
            settings = date_time if isinstance(date_time, dict) else {}
            # Optional text colour (hex string or RGBA list); falls back to the
            # panel's font colour when missing or invalid.
            color = self.parse_border_color(settings.get("color"))
            features["datetime"] = {"color": color}

        shortcuts = config.get("opt_component_feature_shortcuts")
        if shortcuts is not None and shortcuts is not False:
            settings = shortcuts if isinstance(shortcuts, dict) else {}

            column_count = settings.get("column_count", 1)
            if (
                isinstance(column_count, bool)
                or not isinstance(column_count, int)
                or column_count < 1
            ):
                column_count = 1

            raw_entries = settings.get("entries", [])
            # Optional default color applied to every shortcut, which any
            # individual entry can still override.
            raw_entries_color = settings.get("entries_color")
            default_entry_color = (
                self.parse_border_color(raw_entries_color)
                if raw_entries_color is not None
                else None
            )
            entries = []
            if isinstance(raw_entries, list):
                for item in raw_entries:
                    if not isinstance(item, dict):
                        continue
                    # An entry may run its commands inside a SINGLE shell window
                    # (one console, executed sequentially) when it declares a
                    # "special_type" of "cmd", "powershell" or "wsl". In that
                    # form "commands" is a list of plain command STRINGS. This
                    # is separate from the default form below where "commands"
                    # is a list of {"path", "arguments"} objects each launched
                    # as its own program.
                    special_type = item.get("special_type")
                    if isinstance(special_type, str) and special_type.strip().lower() in (
                        "cmd",
                        "powershell",
                        "wsl",
                    ):
                        special_type = special_type.strip().lower()
                        raw_commands = item.get("commands")
                        shell_commands = []
                        if isinstance(raw_commands, list):
                            for cmd in raw_commands:
                                if isinstance(cmd, str) and cmd.strip():
                                    shell_commands.append(cmd)
                        if not shell_commands:
                            continue
                        label = item.get("label", "")
                        if not isinstance(label, str) or not label.strip():
                            label = shell_commands[0]
                        icon = item.get("opt_icon")
                        if not isinstance(icon, str) or not icon.strip():
                            icon = None
                        raw_color = item.get("font_rgba", item.get("color"))
                        color = (
                            self.parse_border_color(raw_color)
                            if raw_color is not None
                            else default_entry_color
                        )
                        workspaces = self.parse_workspaces(item.get("workspaces"))
                        entries.append(
                            {
                                "label": label,
                                "commands": [],
                                "special_type": special_type,
                                "shell_commands": shell_commands,
                                "icon": icon,
                                "color": color,
                                "workspaces": workspaces,
                            }
                        )
                        continue
                    # An entry may launch a SINGLE program via top-level
                    # "path"/"arguments", or MULTIPLE programs via a "commands"
                    # list of {"path", "arguments"} objects run in order. When
                    # both are present the top-level path runs first, then the
                    # commands list. Normalize either form into a commands list.
                    commands = []
                    top_path = item.get("path", "")
                    if isinstance(top_path, str) and top_path.strip():
                        top_args = item.get("arguments", "")
                        if not isinstance(top_args, str):
                            top_args = ""
                        commands.append({"path": top_path, "arguments": top_args})
                    raw_commands = item.get("commands")
                    if isinstance(raw_commands, list):
                        for cmd in raw_commands:
                            if not isinstance(cmd, dict):
                                continue
                            cmd_path = cmd.get("path", "")
                            if not isinstance(cmd_path, str) or not cmd_path.strip():
                                continue
                            cmd_args = cmd.get("arguments", "")
                            if not isinstance(cmd_args, str):
                                cmd_args = ""
                            commands.append(
                                {"path": cmd_path, "arguments": cmd_args}
                            )
                    if not commands:
                        continue
                    label = item.get("label", "")
                    if not isinstance(label, str) or not label.strip():
                        label = commands[0]["path"]
                    icon = item.get("opt_icon")
                    if not isinstance(icon, str) or not icon.strip():
                        icon = None
                    raw_color = item.get("font_rgba", item.get("color"))
                    color = (
                        self.parse_border_color(raw_color)
                        if raw_color is not None
                        else default_entry_color
                    )
                    workspaces = self.parse_workspaces(item.get("workspaces"))
                    entries.append(
                        {
                            "label": label,
                            "commands": commands,
                            "special_type": None,
                            "shell_commands": [],
                            "icon": icon,
                            "color": color,
                            "workspaces": workspaces,
                        }
                    )

            if entries:
                features["shortcuts"] = {
                    "column_count": column_count,
                    "entries": entries,
                    "border_width": self.parse_border_width(settings.get("border_width")),
                    "border_color": self.parse_border_color(settings.get("border_color")),
                    "has_workspace_filter": any(
                        entry["workspaces"] is not None for entry in entries
                    ),
                }

        notifications = config.get("opt_feature_notifications")
        if notifications is not None and notifications is not False:
            settings = notifications if isinstance(notifications, dict) else {}
            indicator = settings.get("indicator", DEFAULT_NOTIFICATION_INDICATOR)
            if not isinstance(indicator, str) or not indicator.strip():
                indicator = DEFAULT_NOTIFICATION_INDICATOR
            color = self.parse_border_color(settings.get("color"))
            if not color:
                color = DEFAULT_NOTIFICATION_COLOR
            features["notifications"] = {
                "indicator": indicator.strip(),
                "color": color,
            }

        music = config.get("opt_feature_musichighlight")
        if music is not None and music is not False:
            settings = music if isinstance(music, dict) else {}
            indicator = settings.get("indicator", DEFAULT_MUSIC_INDICATOR)
            if not isinstance(indicator, str) or not indicator.strip():
                indicator = DEFAULT_MUSIC_INDICATOR
            # No colour: the music state only appends a glyph and leaves the
            # label's existing text colour untouched.
            features["musichighlight"] = {
                "indicator": indicator.strip(),
            }

        timer = config.get("opt_component_feature_timer")
        if timer is not None and timer is not False:
            settings = timer if isinstance(timer, dict) else {}
            workspaces = self.parse_workspaces(settings.get("workspaces"))
            # Pre-filled duration for the ADD dialog; falls back to the built-in
            # default if the configured value is missing or not a valid time.
            default_seconds = self.parse_duration(settings.get("default_new_time"))
            default_new_time = (
                self.format_duration(default_seconds)
                if default_seconds is not None
                else DEFAULT_TIMER_DURATION
            )
            raw_opacity = settings.get("time_opacity", 1.0)
            try:
                time_opacity = float(raw_opacity)
                if not (0.0 <= time_opacity <= 1.0):
                    time_opacity = 1.0
            except (TypeError, ValueError):
                time_opacity = 1.0
            features["timer"] = {
                "workspaces": workspaces,
                "has_workspace_filter": workspaces is not None,
                "default_new_time": default_new_time,
                # Fraction 0.0–1.0 applied to the timer's countdown digits to
                # reduce static pixel luminance on OLED displays. The digits are
                # composited toward their background (see build_timer_row); the
                # rest of the overlay is unaffected. 1.0 = fully opaque.
                "time_opacity": time_opacity,
            }

        hide_idle = config.get("opt_feature_hide_when_idle")
        if hide_idle is not None and hide_idle is not False:
            settings = hide_idle if isinstance(hide_idle, dict) else {}
            # Seconds of no input before the overlay hides. Must be positive;
            # falls back to the default when missing or not a valid number.
            raw_idle = settings.get("idle_seconds", DEFAULT_IDLE_HIDE_SECONDS)
            try:
                idle_seconds = float(raw_idle)
                if idle_seconds <= 0:
                    idle_seconds = DEFAULT_IDLE_HIDE_SECONDS
            except (TypeError, ValueError):
                idle_seconds = DEFAULT_IDLE_HIDE_SECONDS
            features["hidewhenidle"] = {"idle_seconds": idle_seconds}

        return features

    def parse_border_width(self, value):
        """Returns a non-negative integer border width, or 0 when unspecified."""
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value if value > 0 else 0

    def parse_border_color(self, value):
        """Returns a hex color from an RGBA list/object or hex string, else None.

        Accepts a hex string (``"#RRGGBB"``), an RGBA list (``[r, g, b, a]``),
        or an RGBA object (``{"r": .., "g": .., "b": .., "a": ..}`` with
        case-insensitive keys and an optional alpha).
        """
        if isinstance(value, dict):
            value = self.rgba_object_to_list(value)
        if isinstance(value, list):
            try:
                return self.rgba_to_hex(value)
            except Exception:
                return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def rgba_object_to_list(self, value):
        """Converts an ``{r, g, b, a}`` color object into an ``[r, g, b, a]``
        list (alpha optional). Returns None when required channels are missing."""
        lowered = {str(key).lower(): channel for key, channel in value.items()}
        try:
            rgba = [lowered["r"], lowered["g"], lowered["b"]]
        except KeyError:
            return None
        if "a" in lowered:
            rgba.append(lowered["a"])
        return rgba

    def rgba_to_hex(self, rgba):
        if not isinstance(rgba, list) or len(rgba) not in (3, 4):
            raise ValueError()

        red, green, blue = [self.clamp_color_channel(value) for value in rgba[:3]]
        alpha = float(rgba[3]) if len(rgba) == 4 else 1.0
        if alpha < 0 or alpha > 255:
            raise ValueError()
        if alpha > 1:
            alpha = alpha / 255

        red = round(red * alpha)
        green = round(green * alpha)
        blue = round(blue * alpha)
        return f"#{red:02X}{green:02X}{blue:02X}"

    def clamp_color_channel(self, value):
        if not isinstance(value, (int, float)):
            raise ValueError()

        value = int(value)
        if value < 0 or value > 255:
            raise ValueError()
        return value

    def parse_size_scale(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError()

        value = float(value)
        if value < 0.5 or value > 3.0:
            raise ValueError()
        return value

    def scaled_size(self, size):
        return max(1, round(size * self.workspace_size_scale))

    def get_all_labels(self, names, total_desktops):
        """Maps configured names safely to total available virtual desktops."""
        labels = {}
        for i in range(1, total_desktops + 1):
            if i <= len(names) and names[i - 1]:
                labels[i] = f" {names[i - 1]} "
            else:
                labels[i] = f" [{i}] "
        return labels

    def build_workspace_list(self):
        """Resolves per-monitor configs and builds the overlay on each monitor."""
        total_desktops = self.get_desktop_count()
        self.displayed_desktop_count = total_desktops

        # (Re)create one window per target monitor, each with its own config.
        targets = self.resolve_render_targets()
        self.sync_panels(targets)

        # Notifications are tracked globally if ANY rendered config enables them;
        # each panel still renders the mark using its own indicator/colour.
        self.notifications_enabled = any(
            panel.config["features"].get("notifications") is not None
            for panel in self.panels
        )
        # Drop any pending marks for desktops that no longer exist.
        self.notified_desktops = {
            n for n in self.notified_desktops if 1 <= n <= total_desktops
        }

        # Music highlight is tracked globally too, but only if the audio backend
        # imported successfully; otherwise the feature stays silently disabled.
        self.music_enabled = _AUDIO_AVAILABLE and any(
            panel.config["features"].get("musichighlight") is not None
            for panel in self.panels
        )
        if not self.music_enabled:
            self.playing_desktops = set()
            self.music_grace = {}

        # Auto-hide while idle is global too: enabled if ANY rendered config
        # declares it. When several configs set different thresholds, use the
        # smallest so the overlay hides as soon as the strictest one wants.
        idle_features = [
            panel.config["features"]["hidewhenidle"]
            for panel in self.panels
            if panel.config["features"].get("hidewhenidle") is not None
        ]
        self.idle_hide_enabled = bool(idle_features)
        self.idle_hide_seconds = (
            min(feature["idle_seconds"] for feature in idle_features)
            if idle_features
            else DEFAULT_IDLE_HIDE_SECONDS
        )

        # Reset global toolbar state, then render each panel from its config.
        self.move_mode = False
        self.move_selected_hwnd = None
        self.move_baseline_hwnd = None
        self.tracked_pinned = None
        for panel in self.panels:
            self.build_panel(panel, total_desktops)

        # Paint any already-pending notification marks onto the fresh labels.
        self.refresh_notifications()

        self.config_mtime = self.get_config_mtime()

    def build_panel(self, panel, total_desktops):
        """Creates clickable labels dynamically for every desktop on a window."""
        config = panel.config
        # Cache this panel's style, and mirror it to the build-context fields
        # used by scaled_size and the toolbar/shortcut builders below.
        panel.font_color = config["font_color"]
        panel.surface_color = config["surface_color"]
        panel.size_scale = config["size_scale"]
        panel.features = config["features"]
        panel.notification_settings = config["features"].get("notifications")
        panel.music_settings = config["features"].get("musichighlight")
        self.workspace_font_color = panel.font_color
        self.workspace_surface_color = panel.surface_color
        self.workspace_size_scale = panel.size_scale

        win = panel.win
        # Keep this panel's window fully opaque. time_opacity is applied only
        # to the timer's countdown digits (see build_timer_row), not the whole
        # window, so the rest of the overlay is never dimmed.
        try:
            win.attributes("-alpha", 1.0)
        except Exception:
            pass

        # Clear existing widgets before rebuilding the config button and labels.
        # Secondary monitor windows are Tk children of the root, so skip any
        # Toplevel here to avoid destroying the other panels.
        for widget in win.winfo_children():
            if isinstance(widget, tk.Toplevel):
                continue
            widget.destroy()
        panel.label_widgets.clear()
        panel.label_base_text.clear()
        panel.name_colors = {}
        panel.marked_desktops = set()
        panel.music_marked = set()
        panel.move_button = None
        panel.pin_button = None
        panel.datetime_label = None
        panel.toolbar_frame = None
        panel.shortcut_icons = []
        panel.shortcuts_frame = None
        panel.shortcuts_config = None
        panel.timer_frame = None
        panel.timer_config = None
        panel.timer_value_labels = {}
        # Force the update loop to repaint the active-desktop highlight after a
        # full rebuild, since all label widgets were just recreated.
        panel.highlighted_desktop_num = None

        # Vertical container of one or more horizontal rows. The gear button
        # and the workspace labels flow left-to-right and wrap onto additional
        # rows so the window never grows wider than the monitor.
        #
        # This is critical, not cosmetic: a layered -transparentcolor window
        # that overflows its monitor (or spills onto an adjacent monitor) fails
        # to composite its lower rows, so the toolbar/shortcut/timer boxes
        # silently vanish on the next recomposite (e.g. the HWND_BOTTOM
        # re-assert when the overlay drops to the background). Keeping the
        # window within the monitor bounds is what keeps those rows visible.
        list_frame = tk.Frame(win, bg=COLOR_BG)
        list_frame.pack(side="top", anchor="w")

        # Usable width for this monitor; fall back to the primary screen width
        # when the monitor rect is unknown.
        if panel.monitor_rect:
            max_row_width = panel.monitor_rect[2] - panel.monitor_rect[0]
        else:
            max_row_width = self.root.winfo_screenwidth()
        # Leave a small margin so the window never quite reaches the edge.
        max_row_width = max(1, max_row_width - self.scaled_size(8))

        gap = self.scaled_size(2)
        row = tk.Frame(list_frame, bg=COLOR_BG)
        row.pack(side="top", anchor="w")
        row_width = 0

        config_lbl = tk.Label(
            row,
            text=" ⚙ CONFIG ",
            font=("Segoe UI", self.scaled_size(12), "bold"),
            bg=COLOR_BAD_BG if config["bad_format"] else COLOR_ACTIVE_BG,
            fg=COLOR_BAD_CONFIG if config["bad_format"] else self.workspace_font_color,
            padx=self.scaled_size(8),
            pady=self.scaled_size(4),
            cursor="hand2"
        )
        config_lbl.pack(side="left", padx=(0, self.scaled_size(6)))
        config_lbl.bind("<Button-1>", lambda event: self.open_config())
        # Measure widths from font metrics rather than winfo_reqwidth(), which
        # returns 1 for freshly-created widgets until an idle pass runs.
        cfg_font = tkfont.Font(
            family="Segoe UI", size=self.scaled_size(12), weight="bold"
        )
        row_width += (
            cfg_font.measure(" ⚙ CONFIG ")
            + self.scaled_size(8) * 2
            + 4
            + self.scaled_size(6)
        )

        if config["bad_format"]:
            bad_config_lbl = tk.Label(
                row,
                text=" JSON: Bad format ",
                font=("Consolas", self.scaled_size(11), "bold"),
                bg=COLOR_BG,
                fg=COLOR_BAD_CONFIG,
                padx=self.scaled_size(6),
                pady=self.scaled_size(4),
            )
            bad_config_lbl.pack(side="left", padx=self.scaled_size(2))
            return

        workspace_maps = self.get_all_labels(config["names"], total_desktops)

        # Map any per-label color overrides onto their 1-based desktop index.
        config_colors = config.get("name_colors", [])
        panel.name_colors = {
            idx: config_colors[idx - 1]
            for idx in workspace_maps
            if idx - 1 < len(config_colors) and config_colors[idx - 1]
        }

        ws_font = tkfont.Font(
            family="Consolas", size=self.scaled_size(11), weight="bold"
        )
        label_pad = self.scaled_size(10) * 2 + 4  # padx both sides + border fudge

        for idx, text in workspace_maps.items():
            # Slightly over-estimate so we wrap a touch early rather than spill
            # over the monitor edge.
            lbl_width = ws_font.measure(text) + label_pad + gap * 2
            if row_width > 0 and row_width + lbl_width > max_row_width:
                row = tk.Frame(list_frame, bg=COLOR_BG)
                row.pack(side="top", anchor="w")
                row_width = 0

            lbl = tk.Label(
                row,
                text=text,
                font=("Consolas", self.scaled_size(11), "bold"),
                bg=self.workspace_surface_color,
                fg=panel.name_colors.get(idx, self.workspace_font_color),
                padx=self.scaled_size(10),
                pady=self.scaled_size(6),
                cursor="hand2"  # Changes mouse cursor to hand pointer on hover
            )
            lbl.pack(side="left", padx=gap)
            # Bind the mouse click event directly to Windows desktop switching API
            lbl.bind("<Button-1>", lambda event, num=idx: self.on_label_click(num))
            panel.label_widgets[idx] = lbl
            panel.label_base_text[idx] = text
            row_width += lbl_width

        # Optional toolbar row populated by features declared in the config.
        self.build_toolbar(panel, config["features"])
        # Optional shortcut launcher grid.
        if "shortcuts" in config["features"]:
            self.build_shortcuts(panel, config["features"]["shortcuts"])
        # Optional countdown-timer box.
        if "timer" in config["features"]:
            self.build_timer(panel, config["features"]["timer"])

    def build_toolbar(self, panel, features):
        """Builds the optional feature toolbar underneath the workspace list."""
        if (
            "movewindow" not in features
            and "pinwindow" not in features
            and "datetime" not in features
        ):
            return

        toolbar = tk.Frame(panel.win, bg=COLOR_BG)
        toolbar.pack(side="top", anchor="w", pady=(self.scaled_size(4), 0))
        panel.toolbar_frame = toolbar

        if "movewindow" in features:
            panel.move_label = features["movewindow"]["label"]
            move_btn = tk.Label(
                toolbar,
                text=f" {panel.move_label} ",
                font=("Segoe UI", self.scaled_size(11), "bold"),
                bg=COLOR_ACTIVE_BG if self.move_mode else COLOR_HIT_BG,
                fg=COLOR_TEXT_ACTIVE if self.move_mode else self.workspace_font_color,
                padx=self.scaled_size(8),
                pady=self.scaled_size(4),
                cursor="hand2",
            )
            move_btn.pack(side="left", padx=self.scaled_size(2))
            move_btn.bind("<Button-1>", lambda event: self.toggle_move_mode())
            panel.move_button = move_btn

        if "pinwindow" in features:
            panel.pin_labels = features["pinwindow"]
            pin_btn = tk.Label(
                toolbar,
                text=f" {panel.pin_labels['label_pin']} ",
                font=("Segoe UI", self.scaled_size(11), "bold"),
                bg=COLOR_HIT_BG,
                fg=self.workspace_font_color,
                padx=self.scaled_size(8),
                pady=self.scaled_size(4),
                cursor="hand2",
            )
            pin_btn.pack(side="left", padx=self.scaled_size(2))
            pin_btn.bind("<Button-1>", lambda event: self.toggle_pin_window())
            panel.pin_button = pin_btn

        if "datetime" in features:
            color = features["datetime"].get("color") or self.workspace_font_color
            datetime_lbl = tk.Label(
                toolbar,
                text=f" {self.format_datetime()} ",
                font=("Segoe UI", self.scaled_size(11), "bold"),
                bg=COLOR_HIT_BG,
                fg=color,
                padx=self.scaled_size(8),
                pady=self.scaled_size(4),
            )
            datetime_lbl.pack(side="left", padx=self.scaled_size(2))
            panel.datetime_label = datetime_lbl

    def format_datetime(self):
        """Returns the current local date/time as e.g. 'Jun 5 - 23:23'."""
        lt = time.localtime()
        return f"{time.strftime('%b', lt)} {lt.tm_mday} - {time.strftime('%H:%M', lt)}"

    def refresh_datetime_labels(self):
        """Updates the live toolbar clock on every panel that shows it."""
        text = f" {self.format_datetime()} "
        for panel in self.panels:
            label = panel.datetime_label
            if label is None:
                continue
            if label.cget("text") != text:
                try:
                    label.configure(text=text)
                except Exception:
                    pass

    def set_move_mode(self, enabled):
        """Arms/disarms move-window mode and refreshes every toolbar button."""
        self.move_mode = enabled
        if enabled:
            # Record the raw foreground window at arm time. Clicking the toolbar
            # button activates our (clickable) overlay, so this is normally one
            # of our own windows; the user's next click moves focus to a real
            # window, which we then capture as the selection (see
            # update_move_selection). Using the RAW foreground here - not the
            # filtered tracked window - is what lets the user pick the very
            # window they already had focused.
            self.move_selected_hwnd = None
            try:
                self.move_baseline_hwnd = win32gui.GetForegroundWindow()
            except Exception:
                self.move_baseline_hwnd = None
        else:
            self.move_selected_hwnd = None
            self.move_baseline_hwnd = None
        self.update_move_button()

    def update_move_button(self):
        """Reflects the current move-mode phase on every move button.

        Idle shows the configured label; once armed it shows "Select window."
        until the user picks a window, then "Select workspace.".
        """
        for panel in self.panels:
            if panel.move_button is None:
                continue
            if not self.move_mode:
                text = panel.move_label
                active = False
            elif self.move_selected_hwnd is None:
                text = MOVE_SELECT_WINDOW_LABEL
                active = True
            else:
                text = MOVE_SELECT_WORKSPACE_LABEL
                active = True
            panel.move_button.configure(
                text=f" {text} ",
                bg=COLOR_ACTIVE_BG if active else COLOR_HIT_BG,
                fg=COLOR_TEXT_ACTIVE if active else panel.font_color,
            )

    def update_move_selection(self):
        """While awaiting a window pick, capture the first real (non-overlay)
        window the user gives focus to once move mode is armed."""
        if not self.move_mode or self.move_selected_hwnd is not None:
            return
        try:
            fg = win32gui.GetForegroundWindow()
        except Exception:
            fg = 0
        # Ignore: nothing focused, still on the arm-time window (our overlay),
        # our own overlay/tray windows, or the desktop/taskbar shell windows.
        if not fg or fg == self.move_baseline_hwnd:
            return
        if fg in self.panel_hwnds() or fg == self.tray_hwnd:
            return
        try:
            cls = win32gui.GetClassName(fg)
        except Exception:
            cls = ""
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return
        self.move_selected_hwnd = fg
        self.update_move_button()

    def toggle_move_mode(self):
        self.set_move_mode(not self.move_mode)

    def rebuild_panel_shortcuts(self, panel):
        """Rebuilds just this panel's shortcut grid for the current desktop.

        Restores the per-panel build context (font color/scale) that
        ``build_shortcuts`` relies on, then re-runs it using the cached config.
        """
        if panel.shortcuts_config is None:
            return
        self.workspace_font_color = panel.font_color
        self.workspace_surface_color = panel.surface_color
        self.workspace_size_scale = panel.size_scale
        self.build_shortcuts(panel, panel.shortcuts_config)

    def build_shortcuts(self, panel, shortcuts):
        """Builds the optional shortcut launcher grid under the toolbar.

        Entries may be limited to specific virtual desktops via their
        ``workspaces`` set; only those matching the current desktop are shown.
        The config is cached so the grid can be cheaply rebuilt when the active
        desktop changes (only happens on a real switch, never per tick).
        """
        panel.shortcut_icons = []
        panel.shortcuts_config = shortcuts

        # Keep a reference to the currently displayed grid but DO NOT destroy it
        # yet. We build the replacement fully (measured + gridded) while the old
        # grid stays on screen, then swap atomically at the end. Destroying it
        # up front - or packing an empty new frame and calling update_idletasks
        # to measure - forces an intermediate paint where the grid is missing or
        # zero-height, which showed up as the linkbar briefly rendering lower
        # (the timer box jumping up to fill the gap, then back down).
        old_frame = panel.shortcuts_frame
        panel.shortcuts_frame = None

        current_desktop = self.last_active_desktop
        visible_entries = [
            entry
            for entry in shortcuts["entries"]
            if entry["workspaces"] is None
            or (current_desktop is not None and current_desktop in entry["workspaces"])
        ]
        if not visible_entries:
            if old_frame is not None:
                try:
                    old_frame.destroy()
                except Exception:
                    pass
            return

        # NOTE: the frame's background must be an opaque (non-keyed) color.
        # Using COLOR_BG (the -transparentcolor key) for the surrounding/gap
        # pixels next to the opaque entries makes the layered window re-composite
        # those transparent edges, which shows up as a ~1px diagonal "wiggle".
        #
        # Built UNPACKED (no geometry manager yet) so it stays invisible while
        # we populate and measure it; it is packed into position only once it is
        # fully laid out, so the user never sees a partially built grid.
        frame = tk.Frame(panel.win, bg=COLOR_HIT_BG)

        # Optional border drawn around the whole shortcut grid.
        border_width = shortcuts.get("border_width", 0)
        border_color = shortcuts.get("border_color")
        if border_width > 0 and border_color:
            frame.configure(
                highlightthickness=self.scaled_size(border_width),
                highlightbackground=border_color,
                highlightcolor=border_color,
            )

        column_count = shortcuts["column_count"]

        # Build every entry's item frame first WITHOUT gridding it, so the grid
        # is never laid out at the (possibly oversized) configured column_count
        # and then reflowed - that intermediate wide layout briefly painted as
        # the component flashing very wide before snapping to its content size.
        grid_items = []
        for entry in visible_entries:
            item = tk.Frame(frame, bg=COLOR_HIT_BG, cursor="hand2")
            grid_items.append(item)

            entry_color = entry.get("color") or self.workspace_font_color

            image = self.load_shortcut_icon(panel, entry["icon"])
            icon_lbl = tk.Label(
                item,
                bg=COLOR_HIT_BG,
                fg=entry_color,
                padx=0,
                pady=self.scaled_size(4),
                cursor="hand2",
            )
            if image is not None:
                icon_lbl.configure(image=image)
            else:
                icon_lbl.configure(
                    text=PLACEHOLDER_SHORTCUT_ICON,
                    font=("Segoe UI Emoji", self.scaled_size(11)),
                )
            icon_lbl.pack(side="left", padx=(self.scaled_size(4), 0))

            text_lbl = tk.Label(
                item,
                text=f"{entry['label']} ",
                font=("Segoe UI", self.scaled_size(11), "bold"),
                bg=COLOR_HIT_BG,
                fg=entry_color,
                padx=0,
                pady=self.scaled_size(4),
                cursor="hand2",
            )
            text_lbl.pack(side="left")

            # Thin vertical separator pinned to the right edge of the cell. The
            # item stretches to the uniform column width (sticky="we"), so a
            # side="right" separator lines up neatly between columns. It is
            # hidden on the last item of each row after the grid is laid out.
            separator = tk.Frame(
                item,
                bg=SHORTCUT_SEPARATOR_COLOR,
                width=self.scaled_size(1),
            )
            separator.pack(
                side="right", fill="y",
                pady=self.scaled_size(3),
            )
            item.separator = separator

            for widget in (item, icon_lbl, text_lbl):
                widget.bind(
                    "<Button-1>", lambda event, en=entry: self.launch_shortcut(en)
                )

        # Clamp the column count so the grid never grows wider than the monitor.
        # The columns are uniform width (= the widest entry) and every CONFIGURED
        # column takes that width even when empty, so a column_count larger than
        # the number of visible entries (or larger than the monitor can hold)
        # pushes the grid past the monitor edge. An oversized layered
        # -transparentcolor window fails to composite, which silently drops the
        # rows above it (the toolbar vanishes), so cap the columns to what
        # actually fits and let the entries wrap onto more rows.
        effective_cols = min(column_count, len(grid_items))
        if panel.monitor_rect and grid_items:
            max_width = (
                panel.monitor_rect[2] - panel.monitor_rect[0] - self.scaled_size(8)
            )
            # reqwidth is computed from each item's packed children even though
            # the items are not yet gridded and the frame is not yet packed, so
            # we can size columns up front. The old grid is still on screen, so
            # this measurement pass does not paint anything new.
            frame.update_idletasks()
            try:
                col_w = max(it.winfo_reqwidth() for it in grid_items)
            except ValueError:
                col_w = 0
            col_w += self.scaled_size(2) * 2  # grid padx on both sides
            if col_w > 0:
                effective_cols = min(effective_cols, max(1, int(max_width // col_w)))

        effective_cols = max(1, effective_cols)
        for col in range(effective_cols):
            frame.grid_columnconfigure(col, weight=1, uniform="shortcuts")

        for index, item in enumerate(grid_items):
            item.grid(
                row=index // effective_cols,
                column=index % effective_cols,
                sticky="we",
                padx=self.scaled_size(2),
                pady=self.scaled_size(2),
            )
            # Drop the separator on the last populated column of each row (and
            # the very last entry) so the grid never ends with a trailing line.
            is_row_end = (index % effective_cols) == effective_cols - 1
            is_last = index == len(grid_items) - 1
            if is_row_end or is_last:
                item.separator.pack_forget()

        # The replacement is fully laid out. Swap it in now: drop the old grid
        # and pack the new one into the correct slot in a single step, with no
        # update_idletasks afterwards, so the next natural paint shows only the
        # finished grid already in its final place (no jump, no empty gap).
        #
        # A plain side="top" pack appends to the BOTTOM of the stack (below the
        # timer box); anchoring "before" the timer keeps shortcuts above it.
        if old_frame is not None:
            try:
                old_frame.destroy()
            except Exception:
                pass
        if panel.timer_frame is not None and panel.timer_frame.winfo_exists():
            frame.pack(
                side="top", anchor="w", pady=(self.scaled_size(4), 0),
                before=panel.timer_frame,
            )
        else:
            frame.pack(side="top", anchor="w", pady=(self.scaled_size(4), 0))
        panel.shortcuts_frame = frame

    def load_shortcut_icon(self, panel, icon_path):
        """Loads a shortcut icon image (PNG/GIF), or None to use a placeholder."""
        if not icon_path:
            return None

        resolved = os.path.expandvars(os.path.expanduser(icon_path))
        if not os.path.isfile(resolved):
            return None

        try:
            image = tk.PhotoImage(file=resolved)
        except Exception:
            return None

        # Shrink oversized icons to roughly the line height of the buttons.
        target = self.scaled_size(18)
        try:
            factor = max(1, image.width() // target)
            if factor > 1:
                image = image.subsample(factor, factor)
        except Exception:
            pass

        panel.shortcut_icons.append(image)
        return image

    def launch_shortcut(self, entry):
        """Launches everything described by a shortcut config entry.

        Two forms are supported:

        * A ``special_type`` entry ("cmd"/"powershell"/"wsl") runs its list of
          command STRINGS sequentially inside a SINGLE console window.
        * Otherwise the entry holds one or more {"path", "arguments"} commands
          which are each launched as their own program, in order.
        """
        special_type = entry.get("special_type")
        if special_type:
            self.launch_special_commands(special_type, entry.get("shell_commands", []))
            return

        for command in entry.get("commands", []):
            path = os.path.expandvars(os.path.expanduser(command["path"]))
            arguments = (
                os.path.expandvars(command["arguments"])
                if command["arguments"]
                else ""
            )
            work_dir = os.path.dirname(path) if os.path.isfile(path) else None
            try:
                win32api.ShellExecute(
                    0,
                    "open",
                    path,
                    arguments or None,
                    work_dir,
                    win32con.SW_SHOWNORMAL,
                )
            except Exception:
                pass

    def launch_special_commands(self, special_type, commands):
        """Runs a list of command strings sequentially in one console window.

        The commands are chained into a single interpreter invocation so they
        share one window and execute one after another. The window is kept open
        afterwards so the output stays visible.
        """
        commands = [
            os.path.expandvars(cmd) for cmd in commands if isinstance(cmd, str) and cmd.strip()
        ]
        if not commands:
            return

        if special_type == "cmd":
            # "&" chains commands sequentially regardless of individual failures;
            # /k keeps the window open so the results remain visible.
            path = "cmd.exe"
            arguments = '/k "{}"'.format(" & ".join(commands))
        elif special_type == "powershell":
            # ";" separates statements; -NoExit keeps the console open.
            path = "powershell.exe"
            arguments = '-NoExit -Command "{}"'.format("; ".join(commands))
        elif special_type == "wsl":
            # Run inside a login shell, then hand control back to an interactive
            # shell so the window stays open after the batch finishes.
            joined = "; ".join(commands)
            path = "wsl.exe"
            arguments = '-e bash -lic "{}; exec bash"'.format(joined)
        else:
            return

        try:
            win32api.ShellExecute(
                0,
                "open",
                path,
                arguments,
                None,
                win32con.SW_SHOWNORMAL,
            )
        except Exception:
            pass

    # ----- Countdown timer component (opt_component_feature_timer) -----------

    def load_timers(self):
        """Loads persisted countdown timers, dropping any already long expired."""
        try:
            with open(TIMERS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return []
        timers = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                end_epoch = item.get("end_epoch")
                tid = item.get("id")
                if not isinstance(name, str):
                    continue
                if not isinstance(end_epoch, (int, float)):
                    continue
                if not isinstance(tid, int):
                    tid = len(timers) + 1
                workspace = item.get("workspace")
                if not isinstance(workspace, int):
                    workspace = None
                # Whether the timer was paused when last saved, and the frozen
                # remaining seconds to restore on resume.
                paused = bool(item.get("paused", False))
                remaining = item.get("remaining")
                if not isinstance(remaining, (int, float)) or remaining < 0:
                    remaining = max(0, end_epoch - time.time()) if paused else 0
                # Original duration for the restart button; fall back to a best
                # effort for legacy timers saved before this field existed.
                duration = item.get("duration")
                if not isinstance(duration, (int, float)) or duration <= 0:
                    duration = (
                        remaining if paused else max(0, end_epoch - time.time())
                    ) or 0
                timers.append(
                    {
                        "id": tid,
                        "name": name,
                        "end_epoch": float(end_epoch),
                        # Original duration the restart button resets to.
                        "duration": float(duration),
                        # Desktop number the timer was created on; only this
                        # workspace's label flashes when the timer expires.
                        "workspace": workspace,
                        # Paused state plus the frozen remaining seconds, so a
                        # paused timer keeps its countdown across restarts.
                        "paused": paused,
                        "remaining": float(remaining),
                        # Whether this timer's expiry has already triggered its
                        # flash, so it does not re-flash every poll or restart.
                        "notified": bool(item.get("notified", False)),
                    }
                )
        return timers

    def save_timers(self):
        """Persists the current timers so they survive an app restart."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(TIMERS_FILE, "w", encoding="utf-8") as handle:
                json.dump(self.timers, handle, indent=2)
        except Exception:
            pass

    def parse_duration(self, text):
        """Parses 'HH:MM:SS' / 'MM:SS' / 'SS' into seconds, or None if invalid."""
        if not isinstance(text, str):
            return None
        text = text.strip()
        if not text:
            return None
        parts = text.split(":")
        if len(parts) > 3:
            return None
        try:
            nums = [int(part) for part in parts]
        except ValueError:
            return None
        if any(num < 0 for num in nums):
            return None
        while len(nums) < 3:
            nums.insert(0, 0)
        hours, minutes, seconds = nums
        total = hours * 3600 + minutes * 60 + seconds
        return total if total > 0 else None

    def format_duration(self, seconds):
        """Formats a (clamped non-negative) second count as HH:MM:SS."""
        seconds = max(0, int(seconds))
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def timer_remaining(self, timer):
        """Whole seconds left on a timer (never negative).

        A paused timer holds the remaining seconds captured when it was paused,
        so its countdown freezes until it is resumed or restarted.
        """
        if timer.get("paused"):
            return max(0, int(round(timer.get("remaining", 0))))
        return max(0, int(round(timer["end_epoch"] - time.time())))

    def rebuild_panel_timer(self, panel):
        """Rebuilds just this panel's timer box for the current desktop."""
        if panel.timer_config is None:
            return
        self.workspace_font_color = panel.font_color
        self.workspace_surface_color = panel.surface_color
        self.workspace_size_scale = panel.size_scale
        self.build_timer(panel, panel.timer_config)

    def rebuild_timers(self):
        """Rebuilds the timer box on every panel that enables the feature.

        Used after a timer is added, removed, or cleared so all monitors stay
        in sync. Restores each panel's build context before rebuilding.
        """
        for panel in self.panels:
            if panel.timer_config is None:
                continue
            self.rebuild_panel_timer(panel)

    def build_timer(self, panel, timer):
        """Builds the optional countdown-timer box under the toolbar/shortcuts.

        The box is only shown when the feature's optional ``workspaces`` filter
        matches the active desktop (same semantics as the shortcut filter). The
        cached config lets the box be rebuilt cheaply on a desktop change or
        when the timer list is mutated.
        """
        panel.timer_value_labels = {}
        panel.timer_config = timer

        # Drop a previously built box (used when rebuilding).
        if panel.timer_frame is not None:
            try:
                panel.timer_frame.destroy()
            except Exception:
                pass
            panel.timer_frame = None

        current_desktop = self.last_active_desktop
        workspaces = timer.get("workspaces")
        if workspaces is not None and (
            current_desktop is None or current_desktop not in workspaces
        ):
            return

        font_color = self.workspace_font_color

        # Opaque (non-keyed) background so the layered window does not wiggle.
        box = tk.Frame(panel.win, bg=COLOR_HIT_BG)
        box.pack(side="top", anchor="w", pady=(self.scaled_size(4), 0))
        box.configure(
            highlightthickness=self.scaled_size(1),
            highlightbackground=font_color,
            highlightcolor=font_color,
        )
        panel.timer_frame = box

        # Left: the list of timer rows. Right: ADD / CLEAR buttons.
        rows = tk.Frame(box, bg=COLOR_HIT_BG)
        rows.pack(side="left", anchor="n", padx=self.scaled_size(2), pady=self.scaled_size(2))

        if not self.timers:
            tk.Label(
                rows,
                text=" no timers ",
                font=("Consolas", self.scaled_size(11)),
                bg=COLOR_HIT_BG,
                fg=font_color,
                padx=self.scaled_size(6),
                pady=self.scaled_size(4),
            ).pack(side="top", anchor="w")
        else:
            for timer_entry in self.timers:
                self.build_timer_row(panel, rows, timer_entry, font_color)

        # Vertical divider between the list and the action buttons.
        tk.Frame(box, bg=font_color, width=self.scaled_size(1)).pack(
            side="left", fill="y", pady=self.scaled_size(2)
        )

        buttons = tk.Frame(box, bg=COLOR_HIT_BG)
        buttons.pack(side="left", anchor="n", padx=self.scaled_size(2), pady=self.scaled_size(2))

        add_btn = tk.Label(
            buttons,
            text=f" {TIMER_ADD_LABEL} ",
            font=("Segoe UI", self.scaled_size(11), "bold"),
            bg=COLOR_HIT_BG,
            fg=font_color,
            padx=self.scaled_size(8),
            pady=self.scaled_size(4),
            cursor="hand2",
        )
        add_btn.pack(side="top", fill="x")
        add_btn.bind("<Button-1>", lambda event, p=panel: self.prompt_add_timer(p))

        tk.Frame(buttons, bg=font_color, height=self.scaled_size(1)).pack(
            side="top", fill="x", pady=self.scaled_size(2)
        )

        clear_btn = tk.Label(
            buttons,
            text=f" {TIMER_CLEAR_LABEL} ",
            font=("Segoe UI", self.scaled_size(11), "bold"),
            bg=COLOR_HIT_BG,
            fg=font_color,
            padx=self.scaled_size(8),
            pady=self.scaled_size(4),
            cursor="hand2",
        )
        clear_btn.pack(side="top", fill="x")
        clear_btn.bind("<Button-1>", lambda event, p=panel: self.prompt_clear_timers(p))

    def build_timer_row(self, panel, parent, timer_entry, font_color):
        """Builds one '[ws] name HH:MM:SS [x]' row inside the timer box."""
        remaining = self.timer_remaining(timer_entry)
        expired = remaining <= 0
        # Dim only the countdown digits toward their background per time_opacity
        # (OLED protection), leaving the name/buttons at full intensity.
        opacity = (panel.timer_config or {}).get("time_opacity", 1.0)
        base_color = TIMER_EXPIRED_COLOR if expired else COLOR_TEXT_ACTIVE
        value_color = blend_hex(base_color, COLOR_HIT_BG, opacity)

        row = tk.Frame(parent, bg=COLOR_HIT_BG)
        row.pack(side="top", anchor="w")

        # Prefix the name with the desktop number the timer was set on.
        workspace = timer_entry.get("workspace")
        prefix = f"[{workspace}] " if workspace else ""
        tk.Label(
            row,
            text=f" {prefix}{timer_entry['name']} ",
            font=("Consolas", self.scaled_size(11), "bold"),
            bg=COLOR_HIT_BG,
            fg=font_color,
            padx=self.scaled_size(4),
            pady=self.scaled_size(3),
        ).pack(side="left")

        value_lbl = tk.Label(
            row,
            text=f" {self.format_duration(remaining)} ",
            font=("Consolas", self.scaled_size(11), "bold"),
            bg=COLOR_HIT_BG,
            fg=value_color,
            padx=self.scaled_size(4),
            pady=self.scaled_size(3),
        )
        value_lbl.pack(side="left")
        panel.timer_value_labels[timer_entry["id"]] = value_lbl

        # Pause/resume toggle: proper media pause icon while running, play icon
        # while paused (drawn in the Segoe MDL2 Assets symbol font).
        paused = bool(timer_entry.get("paused"))
        pause_btn = tk.Label(
            row,
            text=f" {TIMER_RESUME_GLYPH if paused else TIMER_PAUSE_GLYPH} ",
            font=(TIMER_ICON_FONT, self.scaled_size(10)),
            bg=COLOR_HIT_BG,
            fg=font_color,
            padx=self.scaled_size(2),
            pady=self.scaled_size(3),
            cursor="hand2",
        )
        pause_btn.pack(side="left")
        pause_btn.bind(
            "<Button-1>",
            lambda event, tid=timer_entry["id"]: self.toggle_pause_timer(tid),
        )

        # Restart: reset the timer back to its full original duration.
        restart_btn = tk.Label(
            row,
            text=f" {TIMER_RESTART_GLYPH} ",
            font=(TIMER_ICON_FONT, self.scaled_size(10)),
            bg=COLOR_HIT_BG,
            fg=font_color,
            padx=self.scaled_size(2),
            pady=self.scaled_size(3),
            cursor="hand2",
        )
        restart_btn.pack(side="left")
        restart_btn.bind(
            "<Button-1>",
            lambda event, tid=timer_entry["id"]: self.restart_timer(tid),
        )

        remove_btn = tk.Label(
            row,
            text=f" {TIMER_REMOVE_GLYPH} ",
            font=("Segoe UI", self.scaled_size(11), "bold"),
            bg=COLOR_HIT_BG,
            fg=font_color,
            padx=self.scaled_size(4),
            pady=self.scaled_size(3),
            cursor="hand2",
        )
        remove_btn.pack(side="left")
        remove_btn.bind(
            "<Button-1>", lambda event, tid=timer_entry["id"]: self.remove_timer(tid)
        )

    def refresh_timer_values(self):
        """Updates the live countdown digits on every visible timer box.

        Only the small value labels that actually changed are reconfigured, so
        a paused/expired timer (steady 00:00:00) costs nothing and the layered
        window only recomposites the digits that ticked.
        """
        for panel in self.panels:
            if not panel.timer_value_labels:
                continue
            opacity = (panel.timer_config or {}).get("time_opacity", 1.0)
            for timer_entry in self.timers:
                value_lbl = panel.timer_value_labels.get(timer_entry["id"])
                if value_lbl is None:
                    continue
                remaining = self.timer_remaining(timer_entry)
                new_text = f" {self.format_duration(remaining)} "
                base_color = (
                    TIMER_EXPIRED_COLOR if remaining <= 0 else COLOR_TEXT_ACTIVE
                )
                new_color = blend_hex(base_color, COLOR_HIT_BG, opacity)
                if value_lbl.cget("text") != new_text or value_lbl.cget("fg") != new_color:
                    try:
                        value_lbl.configure(text=new_text, fg=new_color)
                    except Exception:
                        pass

    def timer_loop(self):
        """1 Hz refresh of the live countdown digits while a box is visible."""
        try:
            if not self.overlay_hidden:
                self.check_timer_expiry()
                self.refresh_timer_values()
                self.refresh_datetime_labels()
        except Exception:
            pass
        self.root.after(TIMER_POLL_MS, self.timer_loop)

    def check_timer_expiry(self):
        """Starts the label flash when a timer newly reaches 00:00:00.

        Only the workspace the timer was created on flashes, so an expiry alerts
        you exactly where the timer was set.
        """
        newly_expired = False
        for timer in self.timers:
            if self.timer_remaining(timer) <= 0 and not timer.get("notified"):
                timer["notified"] = True
                newly_expired = True
                workspace = timer.get("workspace")
                if workspace:
                    self.flashing_desktops.add(workspace)
        if newly_expired:
            self.save_timers()
            self.start_flashing()

    def start_flashing(self):
        """Begins the flash loop if it is not already running."""
        if self.flash_after_id is None and self.flashing_desktops:
            self.flash_on = False
            self.flash_loop()

    def flash_loop(self):
        """Toggles the flashing labels between normal and an alert highlight."""
        if not self.flashing_desktops:
            self.flash_after_id = None
            self.flash_on = False
            return
        self.flash_on = not self.flash_on
        for panel in self.panels:
            for idx in self.flashing_desktops:
                if idx in panel.label_widgets:
                    self.style_label(panel, idx)
        self.flash_after_id = self.root.after(TIMER_FLASH_MS, self.flash_loop)

    def acknowledge_flash(self, desktop_num):
        """Stops flashing once the user clicks a flashing label.

        Any click on a flashing label acknowledges the expired-timer alert and
        stops the flashing everywhere, even when that desktop is already active.
        """
        if desktop_num not in self.flashing_desktops:
            return
        self.stop_flashing()

    def stop_flashing(self):
        """Clears all flashing labels and repaints them to their normal style."""
        if not self.flashing_desktops:
            return
        cleared = self.flashing_desktops
        self.flashing_desktops = set()
        self.flash_on = False
        for panel in self.panels:
            for idx in cleared:
                if idx in panel.label_widgets:
                    self.style_label(panel, idx)

    def on_label_click(self, desktop_num):
        """Handles a workspace-label click: acknowledge any flash, then switch."""
        self.acknowledge_flash(desktop_num)
        self.switch_desktop(desktop_num)
    def prompt_add_timer(self, panel):
        """Opens a small modal asking for a timer name and duration, then adds it."""
        default_time = DEFAULT_TIMER_DURATION
        if panel.timer_config is not None:
            default_time = panel.timer_config.get("default_new_time", default_time)
        result = self.ask_timer_details(panel.win, default_time)
        if result is None:
            return
        name, seconds = result
        timer_entry = {
            "id": self.next_timer_id,
            "name": name,
            "end_epoch": time.time() + seconds,
            # Original duration in seconds, kept so the row's restart button can
            # reset the timer back to its full length.
            "duration": seconds,
            "workspace": self.last_active_desktop,
            "notified": False,
        }
        self.next_timer_id += 1
        self.timers.append(timer_entry)
        self.save_timers()
        self.rebuild_timers()

    def ask_timer_details(self, parent, default_time=DEFAULT_TIMER_DURATION):
        """Modal dialog returning (name, seconds) or None if cancelled/invalid."""
        dialog = tk.Toplevel(parent)
        dialog.title("Add timer")
        dialog.configure(bg=COLOR_HIT_BG)
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)

        result = {"value": None}

        tk.Label(
            dialog, text="Name:", bg=COLOR_HIT_BG, fg=COLOR_TEXT_ACTIVE,
            font=("Segoe UI", 10),
        ).grid(row=0, column=0, sticky="e", padx=8, pady=(10, 4))
        name_var = tk.StringVar()
        name_entry = tk.Entry(dialog, textvariable=name_var, width=22)
        name_entry.grid(row=0, column=1, padx=8, pady=(10, 4))

        tk.Label(
            dialog, text="Duration (HH:MM:SS):", bg=COLOR_HIT_BG,
            fg=COLOR_TEXT_ACTIVE, font=("Segoe UI", 10),
        ).grid(row=1, column=0, sticky="e", padx=8, pady=4)
        dur_var = tk.StringVar(value=default_time)
        dur_entry = tk.Entry(dialog, textvariable=dur_var, width=22)
        dur_entry.grid(row=1, column=1, padx=8, pady=4)

        error_var = tk.StringVar()
        error_lbl = tk.Label(
            dialog, textvariable=error_var, bg=COLOR_HIT_BG, fg=TIMER_EXPIRED_COLOR,
            font=("Segoe UI", 9),
        )
        error_lbl.grid(row=2, column=0, columnspan=2, padx=8)

        def submit():
            seconds = self.parse_duration(dur_var.get())
            if seconds is None:
                error_var.set("Enter a positive duration like 00:05:00")
                return
            name = name_var.get().strip() or "Timer"
            result["value"] = (name, seconds)
            dialog.destroy()

        def cancel():
            dialog.destroy()

        btn_row = tk.Frame(dialog, bg=COLOR_HIT_BG)
        btn_row.grid(row=3, column=0, columnspan=2, pady=(6, 10))
        tk.Button(btn_row, text="Add", width=8, command=submit).pack(
            side="left", padx=6
        )
        tk.Button(btn_row, text="Cancel", width=8, command=cancel).pack(
            side="left", padx=6
        )

        dialog.bind("<Return>", lambda event: submit())
        dialog.bind("<Escape>", lambda event: cancel())

        name_entry.focus_set()
        dialog.update_idletasks()
        dialog.grab_set()
        parent.wait_window(dialog)
        return result["value"]

    def remove_timer(self, timer_id):
        """Removes a single timer by id, then rebuilds the boxes."""
        before = len(self.timers)
        self.timers = [t for t in self.timers if t["id"] != timer_id]
        if len(self.timers) != before:
            self.save_timers()
            if not self.timers:
                self.stop_flashing()
            self.rebuild_timers()

    def find_timer(self, timer_id):
        """Returns the timer dict with the given id, or None."""
        for timer in self.timers:
            if timer["id"] == timer_id:
                return timer
        return None

    def toggle_pause_timer(self, timer_id):
        """Pauses a running timer or resumes a paused one.

        Pausing freezes the remaining seconds; resuming pushes the end time out
        so the countdown continues from where it left off. The row is rebuilt so
        the button glyph flips between ⏸ and ▶.
        """
        timer = self.find_timer(timer_id)
        if timer is None:
            return
        if timer.get("paused"):
            # Resume: anchor a fresh end time to the frozen remaining seconds.
            timer["end_epoch"] = time.time() + timer.get("remaining", 0)
            timer["paused"] = False
        else:
            # Pause: capture the live remaining seconds and stop counting.
            timer["remaining"] = self.timer_remaining(timer)
            timer["paused"] = True
        self.save_timers()
        self.rebuild_timers()

    def restart_timer(self, timer_id):
        """Resets a timer to its full original duration and resumes counting."""
        timer = self.find_timer(timer_id)
        if timer is None:
            return
        duration = timer.get("duration", 0) or 0
        timer["end_epoch"] = time.time() + duration
        timer["remaining"] = duration
        timer["paused"] = False
        # A restarted timer is live again, so clear its expiry flash state.
        timer["notified"] = False
        workspace = timer.get("workspace")
        if workspace in self.flashing_desktops and not any(
            t.get("workspace") == workspace
            and self.timer_remaining(t) <= 0
            and t.get("notified")
            for t in self.timers
        ):
            # No other expired timer keeps this workspace flashing; clear just
            # this desktop and repaint its label back to normal.
            self.flashing_desktops.discard(workspace)
            for panel in self.panels:
                if workspace in panel.label_widgets:
                    self.style_label(panel, workspace)
            if not self.flashing_desktops:
                self.stop_flashing()
        self.save_timers()
        self.rebuild_timers()

    def prompt_clear_timers(self, panel):
        """Asks for confirmation, then removes all timers."""
        if not self.timers:
            return
        try:
            confirmed = messagebox.askyesno(
                "Clear timers",
                "Remove all timers?",
                parent=panel.win,
            )
        except Exception:
            confirmed = False
        if confirmed:
            self.timers = []
            self.save_timers()
            self.stop_flashing()
            self.rebuild_timers()

    def get_tracked_window(self):
        """Returns the last external (non-overlay) foreground window handle.

        Clicking a toolbar button can briefly steal focus to our overlay, so
        we operate on the most recent window that was NOT one of our own.
        """
        try:
            fg = win32gui.GetForegroundWindow()
        except Exception:
            fg = 0
        if fg and fg not in self.panel_hwnds() and fg != self.tray_hwnd:
            try:
                cls = win32gui.GetClassName(fg)
            except Exception:
                cls = ""
            if cls not in (
                "Progman",
                "WorkerW",
                "Shell_TrayWnd",
                "Shell_SecondaryTrayWnd",
            ):
                self.tracked_hwnd = fg
        return self.tracked_hwnd

    def refresh_pin_button(self):
        """Updates every pin button to mirror the focused window's pinned state."""
        if not any(panel.pin_button is not None for panel in self.panels):
            return

        hwnd = self.get_tracked_window()
        pinned = None
        if hwnd:
            try:
                pinned = AppView(hwnd=hwnd).is_pinned()
            except Exception:
                pinned = None

        if pinned == self.tracked_pinned:
            return
        self.tracked_pinned = pinned

        for panel in self.panels:
            if panel.pin_button is None:
                continue
            if pinned:
                panel.pin_button.configure(
                    text=f" {panel.pin_labels['label_unpin']} ",
                    bg=COLOR_ACTIVE_BG,
                    fg=COLOR_TEXT_ACTIVE,
                )
            else:
                panel.pin_button.configure(
                    text=f" {panel.pin_labels['label_pin']} ",
                    bg=COLOR_HIT_BG,
                    fg=panel.font_color,
                )

    def toggle_pin_window(self):
        """Pins/unpins the focused window so it shows across all workspaces."""
        hwnd = self.get_tracked_window()
        if not hwnd:
            return
        try:
            view = AppView(hwnd=hwnd)
            if view.is_pinned():
                view.unpin()
            else:
                view.pin()
        except Exception:
            pass
        finally:
            self.tracked_pinned = None
            self.refresh_pin_button()


    def get_config_mtime(self):
        try:
            return CONFIG_FILE.stat().st_mtime
        except Exception:
            return None

    def open_config(self):
        """Opens the workspace label config as a text file."""
        self.ensure_config_exists()
        try:
            os.startfile(str(CONFIG_FILE))
        except Exception:
            pass

    def start_tray_thread(self):
        """Run the tray icon on its own thread with a dedicated message pump.

        Relying on Tk's main loop to dispatch the shell's notification messages
        to a separate window proved unreliable: injected (PostMessage) clicks
        were handled, but real shell-delivered clicks were not pumped. A
        dedicated thread that owns the tray window and runs win32gui.PumpMessages
        guarantees the WM_TRAYICON messages are processed.
        """
        if self.tray_thread and self.tray_thread.is_alive():
            return
        self.tray_thread = threading.Thread(
            target=self.tray_thread_main, name="tray-pump", daemon=True
        )
        self.tray_thread.start()

    def tray_thread_main(self):
        try:
            hinst = win32gui.GetModuleHandle(None)

            # A purpose-built WNDCLASS owned by THIS thread; its message pump
            # (PumpMessages, below) dispatches the shell's WM_TRAYICON clicks.
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = "DesktopLabellerTray"
            wc.lpfnWndProc = self.tray_wndproc
            try:
                self.tray_class_atom = win32gui.RegisterClass(wc)
            except win32gui.error:
                # Class already registered earlier in this interpreter session.
                self.tray_class_atom = wc.lpszClassName
            self.tray_wndclass = wc  # keep a reference alive

            self.tray_hwnd = win32gui.CreateWindow(
                self.tray_class_atom,
                APP_NAME,
                0,
                0, 0, 0, 0,
                0, 0, hinst, None,
            )
            if not self.tray_hwnd:
                return

            self.tray_icon = self.load_tray_icon()
            self._add_notify_icon()
            self.tray_installed = True

            # Subscribe to shell-hook messages so we can detect when a window
            # requests attention (taskbar flash) and which workspace it is on.
            try:
                self.wm_shellhook = win32gui.RegisterWindowMessage("SHELLHOOK")
                ctypes.windll.user32.RegisterShellHookWindow(self.tray_hwnd)
            except Exception:
                self.wm_shellhook = None

            # Blocks on this thread until WM_QUIT is posted (see tray_wndproc
            # handling of WM_TRAY_QUIT), pumping all tray messages meanwhile.
            win32gui.PumpMessages()
        except Exception:
            pass

    def _add_notify_icon(self):
        """(Re)register the notification icon for the tray window."""
        notify_data = (
            self.tray_hwnd,
            TRAY_UID,
            win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP,
            WM_TRAYICON,
            self.tray_icon,
            APP_NAME,
        )
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, notify_data)

    def remove_tray_icon(self):
        """Ask the tray thread (the window's owner) to delete the icon & quit.

        Shell_NotifyIcon(NIM_DELETE) and DestroyWindow must run on the thread
        that created the window, so we post a message instead of touching the
        window from the Tk thread.
        """
        if not self.tray_installed:
            return
        self.tray_installed = False
        try:
            if self.tray_hwnd:
                win32gui.PostMessage(self.tray_hwnd, WM_TRAY_QUIT, 0, 0)
        except Exception:
            pass

    def drain_tray_actions(self):
        """Run on the Tk thread: execute UI actions requested by the tray."""
        try:
            while True:
                action = self.tray_action_queue.get_nowait()
                try:
                    action()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            # Drains often so event-driven actions (e.g. the immediate
            # desktop-highlight refresh on window activation) are applied with
            # low latency. This is a non-blocking queue check, so the higher
            # cadence adds no measurable cost when the queue is empty.
            self.root.after(25, self.drain_tray_actions)
        except Exception:
            pass

    def tray_wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY_QUIT:
            # Clean up on the owning thread, then break out of PumpMessages.
            try:
                notify_data = (
                    hwnd,
                    TRAY_UID,
                    win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP,
                    WM_TRAYICON,
                    self.tray_icon,
                    APP_NAME,
                )
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, notify_data)
            except Exception:
                pass
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            win32gui.PostQuitMessage(0)
            return 0
        if self.wm_taskbar_created and msg == self.wm_taskbar_created:
            # Taskbar was recreated (e.g. Explorer restarted): re-add our icon.
            try:
                self._add_notify_icon()
            except Exception:
                pass
            return 0
        if self.wm_shellhook and msg == self.wm_shellhook:
            # A shell-hook event arrived on the tray thread; marshal the work
            # to the Tk thread (Tk is not thread-safe).
            if wparam == HSHELL_FLASH and lparam:
                self.tray_action_queue.put(lambda h=lparam: self.on_window_flash(h))
            elif wparam in (HSHELL_WINDOWACTIVATED, HSHELL_RUDEAPPACTIVATED) and lparam:
                # A window activation usually means a virtual-desktop switch, so
                # refresh the highlight immediately instead of waiting for the
                # backstop poll. Also clears any notification mark for it.
                self.tray_action_queue.put(self.poll_active_desktop)
                self.tray_action_queue.put(lambda h=lparam: self.on_window_activated(h))
            return 0
        if msg == WM_TRAYICON and wparam == TRAY_UID:
            if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                # Marshal Tk work back to the Tk thread.
                self.tray_action_queue.put(self.show_overlay_temporarily)
                return 0
            if lparam == win32con.WM_RBUTTONUP:
                self.show_tray_menu(hwnd)
                return 0

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def show_tray_menu(self, hwnd):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_OPEN_CONFIG, "Open Config")
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_SHOW_OVERLAY, "Show Overlay")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, "Exit")

            cursor_x, cursor_y = win32gui.GetCursorPos()
            # SetForegroundWindow lets the menu dismiss correctly when clicking
            # elsewhere, but it can FAIL and raise when our background process
            # is not permitted to take the foreground. That must never abort the
            # menu itself (the previous behaviour: no menu ever appeared).
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            command = win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN | win32con.TPM_RETURNCMD,
                cursor_x,
                cursor_y,
                0,
                hwnd,
                None,
            )
            # Companion to SetForegroundWindow above; lets the menu close
            # properly on the first click elsewhere.
            try:
                win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
            except Exception:
                pass

            if command == MENU_OPEN_CONFIG:
                self.tray_action_queue.put(self.open_config)
            elif command == MENU_SHOW_OVERLAY:
                self.tray_action_queue.put(self.show_overlay_temporarily)
            elif command == MENU_EXIT:
                self.tray_action_queue.put(self.exit_app)
        except Exception:
            pass
        finally:
            win32gui.DestroyMenu(menu)

    def show_overlay_temporarily(self):
        self.pin_to_background = False
        for panel in self.panels:
            try:
                panel.win.attributes("-topmost", True)
                panel.win.deiconify()
                panel.win.lift()
            except Exception:
                pass
            try:
                win32gui.SetWindowPos(
                    panel.get_hwnd(),
                    win32con.HWND_TOPMOST,
                    0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
            except Exception:
                pass

        self.schedule_background_mode()

    def exit_app(self):
        self.remove_tray_icon()
        # Let the tray thread remove its icon before we tear down, so it does
        # not linger as a "ghost" in the notification area after exit.
        thread = self.tray_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        try:
            self.root.destroy()
        except Exception:
            pass

    def switch_desktop(self, desktop_num):
        """Switches the operating system to the clicked virtual desktop index."""
        if self.move_mode:
            self.move_active_window(desktop_num)
            return
        try:
            VirtualDesktop(desktop_num).go()
        except Exception:
            pass

    def move_active_window(self, desktop_num):
        """Moves the window picked during move mode to the chosen desktop."""
        hwnd = self.move_selected_hwnd
        try:
            if hwnd:
                AppView(hwnd=hwnd).move(VirtualDesktop(desktop_num))
            else:
                AppView.current().move(VirtualDesktop(desktop_num))
        except Exception:
            pass
        finally:
            self.set_move_mode(False)

    def style_label(self, panel, idx):
        """Renders one workspace label from active-highlight, notification and
        music-playing state."""
        lbl = panel.label_widgets.get(idx)
        if lbl is None:
            return
        base = panel.label_base_text.get(idx, lbl.cget("text"))
        is_active = idx == panel.highlighted_desktop_num
        notified = panel.notification_settings is not None and idx in self.notified_desktops
        playing = panel.music_settings is not None and idx in self.playing_desktops
        bg = COLOR_ACTIVE_BG if is_active else panel.surface_color
        # The notification state recolours the whole label; the music state only
        # appends a glyph and never changes the label's text colour.
        if notified:
            fg = panel.notification_settings["color"]
        else:
            fg = panel.name_colors.get(idx) or panel.font_color
        suffix = ""
        if notified:
            suffix += " " + panel.notification_settings["indicator"]
        if playing:
            suffix += " " + panel.music_settings["indicator"]
        if suffix:
            text = f" {base.strip()}{suffix} "
        else:
            text = base
        # An expired-timer label flashes: during the "on" phase it overrides the
        # colours with a bright alert highlight; the "off" phase shows normal.
        if idx in self.flashing_desktops and self.flash_on:
            fg = TIMER_FLASH_FG
            bg = TIMER_EXPIRED_COLOR
        lbl.config(fg=fg, bg=bg, text=text)

    def refresh_notifications(self):
        """Repaints labels whose pending-notification state changed (per panel)."""
        if not self.notifications_enabled:
            return
        for panel in self.panels:
            if panel.notification_settings is None:
                continue
            valid = set(panel.label_widgets.keys())
            want = self.notified_desktops & valid
            for idx in want ^ panel.marked_desktops:
                self.style_label(panel, idx)
            panel.marked_desktops = want

    def refresh_music(self):
        """Repaints labels whose music-playing state changed (per panel)."""
        if not self.music_enabled:
            return
        for panel in self.panels:
            if panel.music_settings is None:
                continue
            valid = set(panel.label_widgets.keys())
            want = self.playing_desktops & valid
            for idx in want ^ panel.music_marked:
                self.style_label(panel, idx)
            panel.music_marked = want

    def music_loop(self):
        """Polls audio sessions and flags desktops whose app is playing sound.

        Runs only while the feature is enabled; otherwise it is a cheap 1 Hz
        heartbeat that re-arms itself so a config reload can turn it back on.
        """
        try:
            if (
                self.music_enabled
                and not self.overlay_hidden
                and self.panels
            ):
                current = self.detect_playing_desktops()
                # Anti-flicker hysteresis: refresh grace for live desktops,
                # decay the rest so the speaker lingers through short gaps.
                for desktop in current:
                    self.music_grace[desktop] = MUSIC_GRACE_CYCLES
                for desktop in list(self.music_grace.keys()):
                    if desktop not in current:
                        self.music_grace[desktop] -= 1
                        if self.music_grace[desktop] <= 0:
                            del self.music_grace[desktop]
                playing = set(self.music_grace.keys())
                if playing != self.playing_desktops:
                    self.playing_desktops = playing
                    self.refresh_music()
        except Exception:
            pass
        self.root.after(MUSIC_POLL_MS, self.music_loop)

    def detect_playing_desktops(self):
        """Set of virtual-desktop numbers whose owning app is emitting audio."""
        if not _AUDIO_AVAILABLE:
            return set()
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            return set()
        playing_pids = set()
        for session in sessions:
            process = session.Process
            if not process:
                continue
            try:
                if session._ctl.GetState() != 1:  # not AudioSessionStateActive
                    continue
                meter = session._ctl.QueryInterface(IAudioMeterInformation)
                if meter.GetPeakValue() <= MUSIC_PEAK_THRESHOLD:
                    continue
            except Exception:
                continue
            playing_pids.add(process.pid)
        if not playing_pids:
            return set()

        # One enumeration pass: top-level visible windows grouped by owning PID.
        # EnumWindows yields windows in z-order (topmost first), so each PID's
        # list is ordered most-recently-active first.
        pid_windows = {}

        def collect(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return True
            pid_windows.setdefault(pid, []).append(hwnd)
            return True

        try:
            win32gui.EnumWindows(collect, None)
        except Exception:
            return set()

        desktops = set()
        for pid in playing_pids:
            # The audio session often belongs to a windowless child (e.g. a
            # browser's audio process); walk up to the nearest ancestor that
            # actually owns visible windows.
            for ancestor in self.ancestor_pids(pid):
                hwnds = pid_windows.get(ancestor)
                if not hwnds:
                    continue
                # An app with windows on several desktops (e.g. a browser whose
                # tabs are spread out) routes all audio through one process, and
                # Windows does not say which window is the source. Mark only the
                # topmost (most-recently-active) window's desktop, the best
                # single guess for where the audio actually plays.
                for hwnd in hwnds:
                    number = self.window_desktop_number(hwnd)
                    if number:
                        desktops.add(number)
                        break
                break
        return desktops

    def ancestor_pids(self, pid):
        """PID and its parent chain (self first), for window-owner lookup."""
        chain = [pid]
        if not _AUDIO_AVAILABLE:
            return chain
        try:
            proc = psutil.Process(pid)
            for _ in range(6):  # bounded walk; avoid pathological loops
                parent = proc.parent()
                if parent is None:
                    break
                chain.append(parent.pid)
                proc = parent
        except Exception:
            pass
        return chain

    def window_desktop_number(self, hwnd):
        """Virtual desktop number a window lives on, or None if it can't map."""
        try:
            return AppView(hwnd=hwnd).desktop.number
        except Exception:
            return None

    def on_window_flash(self, hwnd):
        """Marks the workspace of a window that is requesting attention."""
        if not self.notifications_enabled:
            return
        num = self.window_desktop_number(hwnd)
        if num is None or num in self.notified_desktops:
            return
        self.notified_desktops.add(num)
        self.refresh_notifications()

    def on_window_activated(self, hwnd):
        """Clears a workspace's mark once one of its windows gets focus."""
        if not self.notifications_enabled or not self.notified_desktops:
            return
        num = self.window_desktop_number(hwnd)
        if num is not None and num in self.notified_desktops:
            self.notified_desktops.discard(num)
            self.refresh_notifications()

    def apply_window_styles(self):
        for panel in self.panels:
            hwnd = panel.get_hwnd()
            if not hwnd:
                continue

            # NOTE: WS_EX_TRANSPARENT is removed here so elements can receive mouse clicks.
            # We still use WS_EX_LAYERED to prevent weird rendering anomalies.
            extended_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                extended_style | win32con.WS_EX_LAYERED
            )

            if self.pin_to_background:
                # Send window behind all open operational software
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_BOTTOM,
                    0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
                )

    def enable_background_mode(self):
        self.background_after_id = None
        self.pin_to_background = True
        # Reset so the update loop asserts HWND_BOTTOM once on entry.
        self.last_foreground_hwnd = None
        for panel in self.panels:
            try:
                panel.win.attributes("-topmost", False)
            except Exception:
                pass
        self.apply_window_styles()

    @property
    def overlay_hidden(self):
        """True while the panels are withdrawn (fullscreen app or user idle)."""
        return self.hidden_for_fullscreen or self.hidden_for_idle

    def schedule_background_mode(self):
        """(Re)arm the timer that returns the overlay to the background.

        Cancels any pending timer first so repeated overlay shows never stack
        multiple scheduled callbacks.
        """
        if self.background_after_id is not None:
            try:
                self.root.after_cancel(self.background_after_id)
            except Exception:
                pass
        self.background_after_id = self.root.after(
            STARTUP_VISIBLE_MS, self.enable_background_mode
        )

    def is_fullscreen_app_foreground(self):
        """True when a fullscreen foreground app (e.g. a game) owns the screen.

        Detects a foreground window that covers its entire monitor and is not
        the desktop, shell, or one of our own windows. Used to get the overlay
        completely out of the way while gaming.
        """
        try:
            fg = win32gui.GetForegroundWindow()
            if not fg:
                return False
            if fg in self.panel_hwnds() or fg == self.tray_hwnd:
                return False
            cls = win32gui.GetClassName(fg)
            if cls in (
                "Progman",
                "WorkerW",
                "Shell_TrayWnd",
                "Shell_SecondaryTrayWnd",
            ):
                return False
            # A maximized window overhangs the monitor by its resize border
            # (e.g. -8,-8,+8,+8), which would otherwise look "fullscreen".
            # Exclusive/borderless fullscreen apps are NOT reported maximized,
            # so reject maximized windows to avoid hiding the overlay during
            # ordinary desktop use (maximized browser, editor, etc.).
            if win32gui.GetWindowPlacement(fg)[1] == win32con.SW_SHOWMAXIMIZED:
                return False
            monitor = win32api.MonitorFromWindow(fg, win32con.MONITOR_DEFAULTTONEAREST)
            mon_rect = win32api.GetMonitorInfo(monitor)["Monitor"]
            win_rect = win32gui.GetWindowRect(fg)
            return (
                win_rect[0] <= mon_rect[0]
                and win_rect[1] <= mon_rect[1]
                and win_rect[2] >= mon_rect[2]
                and win_rect[3] >= mon_rect[3]
            )
        except Exception:
            return False

    def refresh_active_desktop(self, current_desktop_num):
        """Repaints the active-desktop highlight on any panel that drifted.

        Guarded on ``panel.highlighted_desktop_num`` so labels are only
        reconfigured on a real desktop change (never per tick), which avoids
        the layered -transparentcolor recomposite "wiggle".
        """
        for panel in self.panels:
            if current_desktop_num == panel.highlighted_desktop_num:
                continue
            previous_desktop_num = panel.highlighted_desktop_num
            panel.highlighted_desktop_num = current_desktop_num
            for idx in panel.label_widgets:
                self.style_label(panel, idx)
            if panel.notification_settings is not None:
                panel.marked_desktops = self.notified_desktops & set(
                    panel.label_widgets.keys()
                )
            else:
                panel.marked_desktops = set()
            # Rebuild the shortcut grid only when entries are scoped to specific
            # desktops, so the visible set tracks the active desktop. Runs on a
            # real switch, not per tick, so it does not trigger the "wiggle".
            #
            # The rebuild destroys the old grid before recreating it, so a
            # transient failure midway would leave the grid gone. Roll the gate
            # back on any error so the next tick retries instead of freezing the
            # panel with no shortcuts until the config is resaved.
            try:
                if (
                    panel.shortcuts_config is not None
                    and panel.shortcuts_config.get("has_workspace_filter")
                ):
                    self.rebuild_panel_shortcuts(panel)
                # Rebuild the timer box only when it is itself desktop-scoped.
                # The shortcut grid repacks "before" the timer box (see
                # build_shortcuts), so it no longer needs the timer rebuilt just
                # to restore stacking order - rebuilding it here would only make
                # the timer flicker on every switch.
                if panel.timer_config is not None and panel.timer_config.get(
                    "has_workspace_filter"
                ):
                    self.rebuild_panel_timer(panel)
            except Exception:
                panel.highlighted_desktop_num = previous_desktop_num
                continue
            # Rebuilding destroys and repacks the lower frames, which on the
            # layered -transparentcolor window can leave the toolbar and box
            # interiors un-painted (they flash, then a recomposite drops the
            # child paints, leaving only frame borders). Flush Tk's pending
            # geometry/redraw work synchronously so the whole panel repaints.
            try:
                panel.win.update_idletasks()
            except Exception:
                pass

    def poll_active_desktop(self):
        """Reads the current desktop number and repaints the highlight if moved.

        Shared by the fast backstop poll and the event-driven activation path.
        Cheap: a single COM read plus a guarded (no-op unless changed) repaint.
        """
        if self.overlay_hidden or not self.panels:
            return
        try:
            current_desktop_num = VirtualDesktop.current().number
        except Exception:
            return
        if current_desktop_num != self.last_active_desktop:
            self.last_active_desktop = current_desktop_num
            self.notified_desktops.discard(current_desktop_num)
        self.refresh_active_desktop(current_desktop_num)

    def track_desktop_loop(self):
        """Backstop poll for desktop-switch responsiveness.

        Switching virtual desktops fires a shell-hook window-activation event
        which already drives an immediate highlight refresh, so this poll only
        needs to catch the rare cases that emit no activation (e.g. switching to
        an empty desktop). It therefore runs slowly to keep COM traffic low.
        """
        try:
            self.poll_active_desktop()
        finally:
            # Always re-arm, so a single failed tick never kills the poll and
            # leaves desktop switches untracked until restart.
            self.root.after(150, self.track_desktop_loop)

    def update_loop(self):
        next_delay = 400
        try:
            # Decide whether the overlay should be hidden this tick, and why.
            # A fullscreen app takes priority; otherwise hide once the user has
            # been idle past the configured threshold (when the feature is on).
            fullscreen = self.is_fullscreen_app_foreground()
            idle = (
                not fullscreen
                and self.idle_hide_enabled
                and system_idle_seconds() >= self.idle_hide_seconds
            )
            self.hidden_for_fullscreen = fullscreen
            self.hidden_for_idle = idle

            if fullscreen or idle:
                # Hide the overlay and stop the periodic SetWindowPos churn so
                # we cannot affect the game/compositor, and poll less often to
                # stay effectively idle until the user returns.
                if not self.windows_withdrawn:
                    self.windows_withdrawn = True
                    for panel in self.panels:
                        try:
                            panel.win.withdraw()
                        except Exception:
                            pass
                # Fullscreen can poll slowly; idle polls a bit faster so the
                # overlay snaps back promptly when the user returns.
                next_delay = 1000 if fullscreen else IDLE_HIDDEN_POLL_MS
            else:
                if self.windows_withdrawn:
                    self.windows_withdrawn = False
                    # Re-assert background z-order on the first tick after
                    # showing, so the restored windows drop behind apps again.
                    self.last_foreground_hwnd = None
                    for panel in self.panels:
                        try:
                            panel.win.deiconify()
                        except Exception:
                            pass

                # Rebuild the windows if a monitor was plugged in/unplugged, or
                # if a desktop was added/removed, or the config file changed.
                current_signature = tuple(self.get_monitor_rects())
                current_total = self.get_desktop_count()
                current_config_mtime = self.get_config_mtime()
                if (
                    current_signature != self.raw_monitor_signature
                    or current_total != self.displayed_desktop_count
                    or current_config_mtime != self.config_mtime
                ):
                    self.build_workspace_list()
                    # A rebuild recreates each window through style_window(),
                    # which unconditionally re-applies "-topmost True". If we
                    # were already pinned to the background, re-assert background
                    # mode so the freshly rebuilt windows drop behind other apps
                    # again - otherwise a config save / monitor or desktop change
                    # would silently leave the overlay stuck on top of every
                    # window until the next foreground switch.
                    if self.pin_to_background:
                        self.enable_background_mode()

                current_desktop_num = VirtualDesktop.current().number

                # Arriving at a desktop (a real switch, not merely sitting on
                # it) clears that workspace's pending notification mark.
                if current_desktop_num != self.last_active_desktop:
                    self.last_active_desktop = current_desktop_num
                    self.notified_desktops.discard(current_desktop_num)

                # Repaint the active-desktop highlight if it drifted from the
                # current desktop. The fast desktop tracker normally does this
                # within ~75ms; this is a backstop after a full rebuild.
                self.refresh_active_desktop(current_desktop_num)

                # Reflect the focused window's pinned state on the pin button.
                self.refresh_pin_button()

                # While move mode is armed, watch for the user picking a window.
                self.update_move_selection()

                if self.pin_to_background:
                    # Enforce background positioning order, but only when the
                    # foreground window actually changed. Re-issuing SetWindowPos
                    # every tick forces the layered -transparentcolor window to
                    # recomposite, which renders as a diagonal "wiggle". A window
                    # can only come above us when focus changes, so that is the
                    # only time we need to re-assert HWND_BOTTOM.
                    try:
                        current_fg = win32gui.GetForegroundWindow()
                    except Exception:
                        current_fg = None
                    if current_fg != self.last_foreground_hwnd:
                        self.last_foreground_hwnd = current_fg
                        for panel in self.panels:
                            hwnd = panel.get_hwnd()
                            if not hwnd:
                                continue
                            win32gui.SetWindowPos(
                                hwnd,
                                win32con.HWND_BOTTOM,
                                0, 0, 0, 0,
                                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
                            )
        except Exception:
            pass

        self.root.after(next_delay, self.update_loop)

if __name__ == "__main__":
    overlay = WorkspaceOverlay()
    overlay.root.mainloop()

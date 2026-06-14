import json
import os
import queue
import sys
import threading
import ctypes
from pathlib import Path
import tkinter as tk
import win32api
import win32gui
import win32con
from pyvda import AppView, VirtualDesktop, get_virtual_desktops

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
DEFAULT_PIN_WINDOW_LABEL = "Pin Window"
DEFAULT_UNPIN_WINDOW_LABEL = "Unpin Window"
# Glyph shown for a shortcut that does not specify (or fails to load) an icon.
PLACEHOLDER_SHORTCUT_ICON = "\U0001F517"  # link symbol
# Optional per-workspace notification indicator (taskbar-flash "needs attention").
DEFAULT_NOTIFICATION_INDICATOR = "\u25CF"  # filled circle
DEFAULT_NOTIFICATION_COLOR = "#FF3333"
LEGACY_LOCAL_CONFIG_FILE = Path("desktops.txt")
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
LEGACY_APPDATA_CONFIG_FILE = CONFIG_DIR / "desktops.txt"
CONFIG_FILE = CONFIG_DIR / "desktops.json"
STARTUP_VISIBLE_MS = 2500
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

class OverlayPanel:
    """Per-monitor overlay window plus its own widget references."""

    def __init__(self, win, monitor_rect):
        self.win = win
        self.monitor_rect = monitor_rect  # (left, top, right, bottom)
        self.label_widgets = {}
        self.label_base_text = {}
        self.marked_desktops = set()
        self.move_button = None
        self.pin_button = None
        self.highlighted_desktop_num = None
        self.shortcut_icons = []
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
        # Optional "pin window" toolbar feature labels.
        self.pin_labels = {"label_pin": DEFAULT_PIN_WINDOW_LABEL, "label_unpin": DEFAULT_UNPIN_WINDOW_LABEL}
        # Last external (non-overlay) foreground window; the pin/move actions
        # and the pin button's displayed state both track this window.
        self.tracked_hwnd = None
        self.tracked_pinned = None
        # Last foreground window seen while pinned to background; used to avoid
        # re-issuing SetWindowPos (and its recomposite/jitter) every tick.
        self.last_foreground_hwnd = None
        # Optional per-workspace notification feature state.
        self.notification_settings = None
        self.notified_desktops = set()
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

    def position_window(self, win, monitor_rect):
        """Positions a window near the top-left of its target monitor."""
        left, top = (monitor_rect[0], monitor_rect[1]) if monitor_rect else (0, 0)
        win.geometry(f"+{left + 20}+{top + 20}")

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

    def parse_desktops(self, config):
        """Parses the optional `desktops` monitor-index list (1-based)."""
        value = config.get("desktops")
        if not isinstance(value, list):
            return None
        indices = [v for v in value if isinstance(v, int) and not isinstance(v, bool)]
        return indices or None

    def target_monitor_rects(self, desktops):
        """Resolves which monitor rectangles to render on (all, or a subset)."""
        rects = self.get_monitor_rects()
        if not desktops:
            return rects
        chosen = [rects[i - 1] for i in desktops if 1 <= i <= len(rects)]
        return chosen or rects

    def sync_panels(self, rects):
        """(Re)creates per-monitor windows so they match the target rects."""
        # Tear down any extra windows beyond the primary.
        for panel in self.panels[1:]:
            try:
                panel.win.destroy()
            except Exception:
                pass
        self.panels = self.panels[:1]

        # Primary panel reuses self.root.
        self.style_window(self.root)
        self.panels[0].monitor_rect = rects[0]
        self.panels[0].hwnd = None
        self.position_window(self.root, rects[0])

        # Secondary monitors get their own Toplevel windows.
        for rect in rects[1:]:
            win = tk.Toplevel(self.root)
            self.style_window(win)
            self.position_window(win, rect)
            self.panels.append(OverlayPanel(win, rect))

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

    def get_workspace_config(self):
        self.ensure_config_exists()

        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8")
            config = json.loads(self.strip_jsonc_comments(raw))
            if not isinstance(config, dict):
                raise ValueError()

            names = config.get("names")
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError()

            font_rgba = config.get("font_rgba", DEFAULT_FONT_RGBA)
            surface_rgba = config.get("surface_rgba", DEFAULT_SURFACE_RGBA)
            size_scale = self.parse_size_scale(config.get("size_scale", DEFAULT_SIZE_SCALE))
            return {
                "bad_format": False,
                "names": [name.strip() for name in names],
                "font_color": self.rgba_to_hex(font_rgba),
                "surface_color": self.rgba_to_hex(surface_rgba),
                "size_scale": size_scale,
                "features": self.parse_optional_features(config),
                "desktops": self.parse_desktops(config),
            }
        except Exception:
            return {
                "bad_format": True,
                "names": [],
                "font_color": COLOR_BAD_CONFIG,
                "surface_color": COLOR_HIT_BG,
                "size_scale": DEFAULT_SIZE_SCALE,
                "features": {},
                "desktops": None,
            }

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

        move_window = config.get("opt_feature_movewindow")
        if move_window is not None and move_window is not False:
            settings = move_window if isinstance(move_window, dict) else {}
            label = settings.get("label", DEFAULT_MOVE_WINDOW_LABEL)
            if not isinstance(label, str) or not label.strip():
                label = DEFAULT_MOVE_WINDOW_LABEL
            features["movewindow"] = {"label": label}

        pin_window = config.get("opt_feature_pinwindow")
        if pin_window is not None and pin_window is not False:
            settings = pin_window if isinstance(pin_window, dict) else {}
            label_pin = settings.get("label_pin", DEFAULT_PIN_WINDOW_LABEL)
            label_unpin = settings.get("label_unpin", DEFAULT_UNPIN_WINDOW_LABEL)
            if not isinstance(label_pin, str) or not label_pin.strip():
                label_pin = DEFAULT_PIN_WINDOW_LABEL
            if not isinstance(label_unpin, str) or not label_unpin.strip():
                label_unpin = DEFAULT_UNPIN_WINDOW_LABEL
            features["pinwindow"] = {"label_pin": label_pin, "label_unpin": label_unpin}

        shortcuts = config.get("opt_feature_shortcuts")
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
            entries = []
            if isinstance(raw_entries, list):
                for item in raw_entries:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path", "")
                    if not isinstance(path, str) or not path.strip():
                        continue
                    label = item.get("label", "")
                    if not isinstance(label, str) or not label.strip():
                        label = path
                    arguments = item.get("arguments", "")
                    if not isinstance(arguments, str):
                        arguments = ""
                    icon = item.get("opt_icon")
                    if not isinstance(icon, str) or not icon.strip():
                        icon = None
                    entries.append(
                        {
                            "label": label,
                            "path": path,
                            "arguments": arguments,
                            "icon": icon,
                        }
                    )

            if entries:
                features["shortcuts"] = {
                    "column_count": column_count,
                    "entries": entries,
                    "border_width": self.parse_border_width(settings.get("border_width")),
                    "border_color": self.parse_border_color(settings.get("border_color")),
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

        return features

    def parse_border_width(self, value):
        """Returns a non-negative integer border width, or 0 when unspecified."""
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value if value > 0 else 0

    def parse_border_color(self, value):
        """Returns a hex border color from an RGBA list or hex string, else None."""
        if isinstance(value, list):
            try:
                return self.rgba_to_hex(value)
            except Exception:
                return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

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
        """Resolves target monitors and builds the overlay on each of them."""
        total_desktops = self.get_desktop_count()
        config = self.get_workspace_config()
        self.displayed_desktop_count = total_desktops
        self.workspace_font_color = config["font_color"]
        self.workspace_surface_color = config["surface_color"]
        self.workspace_size_scale = config["size_scale"]

        # (Re)create one window per selected monitor (all monitors by default).
        rects = self.target_monitor_rects(config.get("desktops"))
        self.sync_panels(rects)

        # Optional per-workspace notification indicator settings.
        self.notification_settings = config["features"].get("notifications")
        # Drop any pending marks for desktops that no longer exist.
        self.notified_desktops = {
            n for n in self.notified_desktops if 1 <= n <= total_desktops
        }

        # Reset global toolbar state, then render the same content per monitor.
        self.move_mode = False
        self.tracked_pinned = None
        for panel in self.panels:
            self.build_panel(panel, config, total_desktops)

        # Paint any already-pending notification marks onto the fresh labels.
        self.refresh_notifications()

        self.config_mtime = self.get_config_mtime()

    def build_panel(self, panel, config, total_desktops):
        """Creates clickable labels dynamically for every desktop on a window."""
        win = panel.win
        # Clear existing widgets before rebuilding the config button and labels.
        # Secondary monitor windows are Tk children of the root, so skip any
        # Toplevel here to avoid destroying the other panels.
        for widget in win.winfo_children():
            if isinstance(widget, tk.Toplevel):
                continue
            widget.destroy()
        panel.label_widgets.clear()
        panel.label_base_text.clear()
        panel.marked_desktops = set()
        panel.move_button = None
        panel.pin_button = None
        panel.shortcut_icons = []
        # Force the update loop to repaint the active-desktop highlight after a
        # full rebuild, since all label widgets were just recreated.
        panel.highlighted_desktop_num = None

        # Row holding the gear button and the workspace labels.
        list_frame = tk.Frame(win, bg=COLOR_BG)
        list_frame.pack(side="top", anchor="w")

        config_lbl = tk.Label(
            list_frame,
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

        if config["bad_format"]:
            bad_config_lbl = tk.Label(
                list_frame,
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

        # Grid/Pack them horizontally (change to side="top" if you prefer vertical stack)
        for idx, text in workspace_maps.items():
            lbl = tk.Label(
                list_frame,
                text=text,
                font=("Consolas", self.scaled_size(11), "bold"),
                bg=self.workspace_surface_color,
                fg=self.workspace_font_color,
                padx=self.scaled_size(10),
                pady=self.scaled_size(6),
                cursor="hand2" # Changes mouse cursor to hand pointer on hover
            )
            lbl.pack(side="left", padx=self.scaled_size(2))

            # Bind the mouse click event directly to Windows desktop switching API
            lbl.bind("<Button-1>", lambda event, num=idx: self.switch_desktop(num))
            panel.label_widgets[idx] = lbl
            panel.label_base_text[idx] = text

        # Optional toolbar row populated by features declared in the config.
        self.build_toolbar(panel, config["features"])
        # Optional shortcut launcher grid.
        if "shortcuts" in config["features"]:
            self.build_shortcuts(panel, config["features"]["shortcuts"])

    def build_toolbar(self, panel, features):
        """Builds the optional feature toolbar underneath the workspace list."""
        if "movewindow" not in features and "pinwindow" not in features:
            return

        toolbar = tk.Frame(panel.win, bg=COLOR_BG)
        toolbar.pack(side="top", anchor="w", pady=(self.scaled_size(4), 0))

        if "movewindow" in features:
            move_btn = tk.Label(
                toolbar,
                text=f" {features['movewindow']['label']} ",
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
            self.pin_labels = features["pinwindow"]
            pin_btn = tk.Label(
                toolbar,
                text=f" {self.pin_labels['label_pin']} ",
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

    def set_move_mode(self, enabled):
        """Toggles move-window mode and reflects it on every toolbar button."""
        self.move_mode = enabled
        for panel in self.panels:
            if panel.move_button is not None:
                panel.move_button.configure(
                    bg=COLOR_ACTIVE_BG if enabled else COLOR_HIT_BG,
                    fg=COLOR_TEXT_ACTIVE if enabled else self.workspace_font_color,
                )

    def toggle_move_mode(self):
        self.set_move_mode(not self.move_mode)

    def build_shortcuts(self, panel, shortcuts):
        """Builds the optional shortcut launcher grid under the toolbar."""
        panel.shortcut_icons = []

        # NOTE: the frame's background must be an opaque (non-keyed) color.
        # Using COLOR_BG (the -transparentcolor key) for the surrounding/gap
        # pixels next to the opaque entries makes the layered window re-composite
        # those transparent edges, which shows up as a ~1px diagonal "wiggle".
        frame = tk.Frame(panel.win, bg=COLOR_HIT_BG)
        frame.pack(side="top", anchor="w", pady=(self.scaled_size(4), 0))

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
        for col in range(column_count):
            frame.grid_columnconfigure(col, weight=1, uniform="shortcuts")

        for index, entry in enumerate(shortcuts["entries"]):
            row = index // column_count
            col = index % column_count

            item = tk.Frame(frame, bg=COLOR_HIT_BG, cursor="hand2")
            item.grid(
                row=row,
                column=col,
                sticky="we",
                padx=self.scaled_size(2),
                pady=self.scaled_size(2),
            )

            image = self.load_shortcut_icon(panel, entry["icon"])
            icon_lbl = tk.Label(
                item,
                bg=COLOR_HIT_BG,
                fg=self.workspace_font_color,
                padx=self.scaled_size(4),
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
            icon_lbl.pack(side="left")

            text_lbl = tk.Label(
                item,
                text=f" {entry['label']} ",
                font=("Segoe UI", self.scaled_size(11), "bold"),
                bg=COLOR_HIT_BG,
                fg=self.workspace_font_color,
                padx=self.scaled_size(4),
                pady=self.scaled_size(4),
                cursor="hand2",
            )
            text_lbl.pack(side="left")

            for widget in (item, icon_lbl, text_lbl):
                widget.bind(
                    "<Button-1>", lambda event, en=entry: self.launch_shortcut(en)
                )

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
        """Launches the program/file described by a shortcut config entry."""
        path = os.path.expandvars(os.path.expanduser(entry["path"]))
        arguments = os.path.expandvars(entry["arguments"]) if entry["arguments"] else ""
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
                    text=f" {self.pin_labels['label_unpin']} ",
                    bg=COLOR_ACTIVE_BG,
                    fg=COLOR_TEXT_ACTIVE,
                )
            else:
                panel.pin_button.configure(
                    text=f" {self.pin_labels['label_pin']} ",
                    bg=COLOR_HIT_BG,
                    fg=self.workspace_font_color,
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
            self.root.after(100, self.drain_tray_actions)
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
        """Moves the currently focused window to the chosen virtual desktop."""
        try:
            AppView.current().move(VirtualDesktop(desktop_num))
        except Exception:
            pass
        finally:
            self.set_move_mode(False)

    def style_label(self, panel, idx):
        """Renders one workspace label from active-highlight + notification state."""
        lbl = panel.label_widgets.get(idx)
        if lbl is None:
            return
        base = panel.label_base_text.get(idx, lbl.cget("text"))
        is_active = idx == panel.highlighted_desktop_num
        notified = self.notification_settings is not None and idx in self.notified_desktops
        bg = COLOR_ACTIVE_BG if is_active else self.workspace_surface_color
        if notified:
            fg = self.notification_settings["color"]
            text = f" {base.strip()} {self.notification_settings['indicator']} "
        else:
            fg = self.workspace_font_color
            text = base
        lbl.config(fg=fg, bg=bg, text=text)

    def refresh_notifications(self):
        """Repaints labels whose pending-notification state changed (per panel)."""
        if self.notification_settings is None:
            return
        for panel in self.panels:
            valid = set(panel.label_widgets.keys())
            want = self.notified_desktops & valid
            for idx in want ^ panel.marked_desktops:
                self.style_label(panel, idx)
            panel.marked_desktops = want

    def window_desktop_number(self, hwnd):
        """Virtual desktop number a window lives on, or None if it can't map."""
        try:
            return AppView(hwnd=hwnd).desktop.number
        except Exception:
            return None

    def on_window_flash(self, hwnd):
        """Marks the workspace of a window that is requesting attention."""
        if self.notification_settings is None:
            return
        num = self.window_desktop_number(hwnd)
        if num is None or num in self.notified_desktops:
            return
        self.notified_desktops.add(num)
        self.refresh_notifications()

    def on_window_activated(self, hwnd):
        """Clears a workspace's mark once one of its windows gets focus."""
        if self.notification_settings is None or not self.notified_desktops:
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

    def update_loop(self):
        next_delay = 400
        try:
            if self.is_fullscreen_app_foreground():
                # A fullscreen app owns the screen. Hide the overlay and stop
                # the periodic SetWindowPos churn so we cannot affect the
                # game/compositor, and poll less often to stay effectively
                # idle until the user returns to the desktop.
                if not self.hidden_for_fullscreen:
                    self.hidden_for_fullscreen = True
                    for panel in self.panels:
                        try:
                            panel.win.withdraw()
                        except Exception:
                            pass
                next_delay = 1000
            else:
                if self.hidden_for_fullscreen:
                    self.hidden_for_fullscreen = False
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

                current_desktop_num = VirtualDesktop.current().number

                # Arriving at a desktop (a real switch, not merely sitting on
                # it) clears that workspace's pending notification mark.
                if current_desktop_num != self.last_active_desktop:
                    self.last_active_desktop = current_desktop_num
                    self.notified_desktops.discard(current_desktop_num)

                # Refresh visual highlights only when the active desktop changed.
                # Reconfiguring labels every tick forces the layered
                # -transparentcolor window to recomposite, which renders as a
                # visible diagonal "wiggle" of the text/border.
                for panel in self.panels:
                    if current_desktop_num != panel.highlighted_desktop_num:
                        panel.highlighted_desktop_num = current_desktop_num
                        for idx in panel.label_widgets:
                            self.style_label(panel, idx)
                        panel.marked_desktops = self.notified_desktops & set(
                            panel.label_widgets.keys()
                        )

                # Reflect the focused window's pinned state on the pin button.
                self.refresh_pin_button()

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

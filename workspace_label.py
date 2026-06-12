import json
import os
import queue
import sys
import threading
from pathlib import Path
import tkinter as tk
import win32api
import win32gui
import win32con
from pyvda import VirtualDesktop, get_virtual_desktops

APP_NAME = "Desktop Labeller"
ICON_FILE = "desktop_labeller.ico"
DEFAULT_NAMES = ["Work", "Web", "Chat", "Media"]
DEFAULT_FONT_RGBA = [85, 68, 34, 1.0]
# Background painted behind each (non-active) workspace label. This must be a
# color other than COLOR_BG, otherwise -transparentcolor makes it click-through.
# Defaults to a near-black surface that is effectively invisible yet clickable.
DEFAULT_SURFACE_RGBA = [2, 2, 2, 1.0]
DEFAULT_SIZE_SCALE = 1.0
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

class WorkspaceOverlay:
    def __init__(self):
        self.root = tk.Tk()

        # 1. Setup borderless overlay window
        self.root.overrideredirect(True)
        # Positioned top-left (X:20, Y:20). Size adjusts automatically to list length
        self.root.geometry("+20+20")
        self.root.configure(bg=COLOR_BG)
        try:
            self.root.attributes("-transparentcolor", COLOR_BG)
        except tk.TclError:
            pass
        self.root.attributes("-topmost", True)

        # Dictionary to keep track of UI label widgets
        self.label_widgets = {}
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
        self.hwnd = None
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

        # Apply the custom window icon (also used for the taskbar)
        self.apply_window_icon()

        # Build the initial interactive layout list
        self.build_workspace_list()

        # 2. Apply Windows stack styling (Pins it to desktop background)
        self.root.after(10, self.apply_window_styles)
        self.root.after(20, self.start_tray_thread)
        self.root.after(50, self.drain_tray_actions)
        self.schedule_background_mode()
        self.root.protocol("WM_DELETE_WINDOW", self.exit_app)

        # 3. Start monitoring active desktop state
        self.update_loop()

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
        if self.hwnd:
            return self.hwnd

        try:
            self.hwnd = int(self.root.wm_frame(), 16)
        except Exception:
            self.hwnd = win32gui.FindWindow(None, self.root.title())
        return self.hwnd

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
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
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
            }
        except Exception:
            return {
                "bad_format": True,
                "names": [],
                "font_color": COLOR_BAD_CONFIG,
                "surface_color": COLOR_HIT_BG,
                "size_scale": DEFAULT_SIZE_SCALE,
            }

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
        """Creates clickable labels dynamically for every desktop."""
        # Clear existing widgets before rebuilding the config button and labels
        for widget in self.root.winfo_children():
            widget.destroy()
        self.label_widgets.clear()

        total_desktops = self.get_desktop_count()
        config = self.get_workspace_config()
        self.displayed_desktop_count = total_desktops
        self.workspace_font_color = config["font_color"]
        self.workspace_surface_color = config["surface_color"]
        self.workspace_size_scale = config["size_scale"]

        config_lbl = tk.Label(
            self.root,
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
                self.root,
                text=" JSON: Bad format ",
                font=("Consolas", self.scaled_size(11), "bold"),
                bg=COLOR_BG,
                fg=COLOR_BAD_CONFIG,
                padx=self.scaled_size(6),
                pady=self.scaled_size(4),
            )
            bad_config_lbl.pack(side="left", padx=self.scaled_size(2))
            self.config_mtime = self.get_config_mtime()
            return

        workspace_maps = self.get_all_labels(config["names"], total_desktops)

        # Grid/Pack them horizontally (change to side="top" if you prefer vertical stack)
        for idx, text in workspace_maps.items():
            lbl = tk.Label(
                self.root,
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
            self.label_widgets[idx] = lbl

        self.config_mtime = self.get_config_mtime()

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
        self.root.attributes("-topmost", True)
        self.root.deiconify()
        self.root.lift()

        try:
            win32gui.SetWindowPos(
                self.get_window_handle(),
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
        try:
            VirtualDesktop(desktop_num).go()
        except Exception:
            pass

    def apply_window_styles(self):
        hwnd = self.get_window_handle()

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
        self.root.attributes("-topmost", False)
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
            if fg in (self.hwnd, self.tray_hwnd):
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
                    try:
                        self.root.withdraw()
                    except Exception:
                        pass
                next_delay = 1000
            else:
                if self.hidden_for_fullscreen:
                    self.hidden_for_fullscreen = False
                    try:
                        self.root.deiconify()
                    except Exception:
                        pass

                # Dynamically verify if a new desktop was added/removed by user
                current_total = self.get_desktop_count()
                current_config_mtime = self.get_config_mtime()
                if current_total != self.displayed_desktop_count or current_config_mtime != self.config_mtime:
                    self.build_workspace_list()

                current_desktop_num = VirtualDesktop.current().number

                # Refresh visual highlights
                for idx, lbl in self.label_widgets.items():
                    if idx == current_desktop_num:
                        lbl.config(fg=self.workspace_font_color, bg=COLOR_ACTIVE_BG)
                    else:
                        lbl.config(fg=self.workspace_font_color, bg=self.workspace_surface_color)

                if self.pin_to_background:
                    # Continuously enforce background positioning order
                    hwnd = int(self.root.wm_frame(), 16)
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

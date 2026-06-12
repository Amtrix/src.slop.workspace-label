import json
import os
from pathlib import Path
import tkinter as tk
import win32gui
import win32con
from pyvda import VirtualDesktop, get_virtual_desktops

APP_NAME = "Desktop Labeller"
DEFAULT_NAMES = ["Work", "Web", "Chat", "Media"]
DEFAULT_FONT_RGBA = [85, 68, 34, 1.0]
DEFAULT_SIZE_SCALE = 1.0
LEGACY_LOCAL_CONFIG_FILE = Path("desktops.txt")
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
LEGACY_APPDATA_CONFIG_FILE = CONFIG_DIR / "desktops.txt"
CONFIG_FILE = CONFIG_DIR / "desktops.json"
STARTUP_VISIBLE_MS = 2500
TRAY_UID = 1
WM_TRAYICON = win32con.WM_USER + 20
MENU_OPEN_CONFIG = 1001
MENU_SHOW_OVERLAY = 1002
MENU_EXIT = 1003

# Overlay Color Palette
COLOR_BG = "#010101"
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
        self.workspace_size_scale = DEFAULT_SIZE_SCALE
        self.pin_to_background = False
        self.hwnd = None
        self.old_wndproc = None
        self.tray_wndproc_callback = self.tray_wndproc
        self.tray_icon = None
        self.tray_installed = False

        # Ensure default configuration template exists
        self.ensure_config_exists()

        # Build the initial interactive layout list
        self.build_workspace_list()

        # 2. Apply Windows stack styling (Pins it to desktop background)
        self.root.after(10, self.apply_window_styles)
        self.root.after(20, self.install_tray_icon)
        self.root.after(STARTUP_VISIBLE_MS, self.enable_background_mode)
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
            size_scale = self.parse_size_scale(config.get("size_scale", DEFAULT_SIZE_SCALE))
            return {
                "bad_format": False,
                "names": [name.strip() for name in names],
                "font_color": self.rgba_to_hex(font_rgba),
                "size_scale": size_scale,
            }
        except Exception:
            return {
                "bad_format": True,
                "names": [],
                "font_color": COLOR_BAD_CONFIG,
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
                bg=COLOR_BG,
                fg=self.workspace_font_color,
                padx=self.scaled_size(6),
                pady=self.scaled_size(4),
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

    def install_tray_icon(self):
        if self.tray_installed:
            return

        try:
            hwnd = self.get_window_handle()
            if not hwnd:
                return

            if self.old_wndproc is None:
                self.old_wndproc = win32gui.SetWindowLong(hwnd, win32con.GWL_WNDPROC, self.tray_wndproc_callback)

            self.tray_icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            notify_data = (
                hwnd,
                TRAY_UID,
                win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP,
                WM_TRAYICON,
                self.tray_icon,
                APP_NAME,
            )
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, notify_data)
            self.tray_installed = True
        except Exception:
            pass

    def remove_tray_icon(self):
        if not self.tray_installed:
            return

        try:
            notify_data = (
                self.get_window_handle(),
                TRAY_UID,
                win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP,
                WM_TRAYICON,
                self.tray_icon,
                APP_NAME,
            )
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, notify_data)
        except Exception:
            pass
        self.tray_installed = False

    def tray_wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON and wparam == TRAY_UID:
            if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                self.show_overlay_temporarily()
                return 0
            if lparam == win32con.WM_RBUTTONUP:
                self.show_tray_menu(hwnd)
                return 0

        if self.old_wndproc:
            return win32gui.CallWindowProc(self.old_wndproc, hwnd, msg, wparam, lparam)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def show_tray_menu(self, hwnd):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_OPEN_CONFIG, "Open Config")
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_SHOW_OVERLAY, "Show Overlay")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, None)
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, "Exit")

            cursor_x, cursor_y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            command = win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN | win32con.TPM_RETURNCMD,
                cursor_x,
                cursor_y,
                0,
                hwnd,
                None,
            )

            if command == MENU_OPEN_CONFIG:
                self.open_config()
            elif command == MENU_SHOW_OVERLAY:
                self.show_overlay_temporarily()
            elif command == MENU_EXIT:
                self.exit_app()
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

        self.root.after(STARTUP_VISIBLE_MS, self.enable_background_mode)

    def exit_app(self):
        self.remove_tray_icon()
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
        self.pin_to_background = True
        self.root.attributes("-topmost", False)
        self.apply_window_styles()

    def update_loop(self):
        try:
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
                    lbl.config(fg=self.workspace_font_color, bg=COLOR_BG)

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

        self.root.after(400, self.update_loop)

if __name__ == "__main__":
    overlay = WorkspaceOverlay()
    overlay.root.mainloop()

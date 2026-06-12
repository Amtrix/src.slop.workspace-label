import os
from pathlib import Path
import tkinter as tk
import win32gui
import win32con
from pyvda import VirtualDesktop, get_virtual_desktops

APP_NAME = "Desktop Labeller"
DEFAULT_CONFIG_TEXT = "Work\nWeb\nChat\nMedia\n"
LEGACY_CONFIG_FILE = Path("desktops.txt")
CONFIG_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME / "desktops.txt"
STARTUP_VISIBLE_MS = 2500

# OLED Dimmed Color Palette
COLOR_BG = "#000000"          # Pure black (OLED pixels completely turned off)
COLOR_TEXT_DIM = "#554422"    # Very dark, low-intensity amber (safe for OLED)
COLOR_TEXT_ACTIVE = "#FFB300" # Muted gold for the active workspace highlight
COLOR_ACTIVE_BG = "#221100"   # Extremely dark brown highlight box background

class WorkspaceOverlay:
    def __init__(self):
        self.root = tk.Tk()

        # 1. Setup borderless overlay window
        self.root.overrideredirect(True)
        # Positioned top-left (X:20, Y:20). Size adjusts automatically to list length
        self.root.geometry("+20+20")
        self.root.configure(bg=COLOR_BG)
        self.root.attributes("-topmost", True)

        # Dictionary to keep track of UI label widgets
        self.label_widgets = {}
        self.config_mtime = None
        self.pin_to_background = False

        # Ensure default configuration template exists
        self.ensure_config_exists()

        # Build the initial interactive layout list
        self.build_workspace_list()

        # 2. Apply Windows stack styling (Pins it to desktop background)
        self.root.after(10, self.apply_window_styles)
        self.root.after(STARTUP_VISIBLE_MS, self.enable_background_mode)

        # 3. Start monitoring active desktop state
        self.update_loop()

    def ensure_config_exists(self):
        if CONFIG_FILE.exists():
            return

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LEGACY_CONFIG_FILE.exists():
                CONFIG_FILE.write_text(LEGACY_CONFIG_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                CONFIG_FILE.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        except Exception:
            pass

    def get_all_labels(self):
        """Reads config and maps it safely to total available virtual desktops."""
        try:
            total_desktops = len(get_virtual_desktops())
        except Exception:
            total_desktops = 4 # Safe fallback

        lines = []
        if CONFIG_FILE.exists():
            try:
                lines = [line.strip() for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines()]
            except Exception:
                pass

        labels = {}
        for i in range(1, total_desktops + 1):
            if i <= len(lines) and lines[i - 1]:
                labels[i] = f" {lines[i - 1]} "
            else:
                labels[i] = f" [{i}] "
        return labels

    def build_workspace_list(self):
        """Creates clickable labels dynamically for every desktop."""
        # Clear existing widgets before rebuilding the config button and labels
        for widget in self.root.winfo_children():
            widget.destroy()
        self.label_widgets.clear()

        workspace_maps = self.get_all_labels()

        config_lbl = tk.Label(
            self.root,
            text=" ⚙ CONFIG ",
            font=("Segoe UI", 12, "bold"),
            bg=COLOR_ACTIVE_BG,
            fg=COLOR_TEXT_ACTIVE,
            padx=8,
            pady=4,
            cursor="hand2"
        )
        config_lbl.pack(side="left", padx=(0, 6))
        config_lbl.bind("<Button-1>", lambda event: self.open_config())

        # Grid/Pack them horizontally (change to side="top" if you prefer vertical stack)
        for idx, text in workspace_maps.items():
            lbl = tk.Label(
                self.root,
                text=text,
                font=("Consolas", 11, "bold"),
                bg=COLOR_BG,
                fg=COLOR_TEXT_DIM,
                padx=6,
                pady=4,
                cursor="hand2" # Changes mouse cursor to hand pointer on hover
            )
            lbl.pack(side="left", padx=2)

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

    def switch_desktop(self, desktop_num):
        """Switches the operating system to the clicked virtual desktop index."""
        try:
            VirtualDesktop(desktop_num).go()
        except Exception:
            pass

    def apply_window_styles(self):
        hwnd = win32gui.FindWindow(None, self.root.title())
        if not hwnd:
            hwnd = int(self.root.wm_frame(), 16)

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
            current_total = len(get_virtual_desktops())
            current_config_mtime = self.get_config_mtime()
            if current_total != len(self.label_widgets) or current_config_mtime != self.config_mtime:
                self.build_workspace_list()

            current_desktop_num = VirtualDesktop.current().number

            # Refresh visual highlights
            for idx, lbl in self.label_widgets.items():
                if idx == current_desktop_num:
                    lbl.config(fg=COLOR_TEXT_ACTIVE, bg=COLOR_ACTIVE_BG)
                else:
                    lbl.config(fg=COLOR_TEXT_DIM, bg=COLOR_BG)

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

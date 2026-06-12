# Desktop Labeller

Desktop Labeller is a small Windows utility that displays clickable labels for your Windows virtual desktops. It places a low-profile Tkinter overlay near the top-left of the desktop, highlights the active desktop, and lets you switch desktops by clicking a label.

## Requirements

- Windows 10 or Windows 11 with Virtual Desktops enabled
- Python 3.10 or newer
- PowerShell 5.1 or newer
- Inno Setup 6 if you want to build the Windows installer
- Python packages:
  - `pyvda`
  - `pywin32`
  - `pyinstaller` if you want to build the executable

## Install Dependencies

If Python is not installed yet, install Python 3.10 or newer first.

Using Windows Package Manager:

```powershell
winget install Python.Python.3.12
```

Or download the Windows installer from the official Python website. During setup, enable **Add python.exe to PATH** before clicking **Install Now**.

After installation, open a new PowerShell window and verify Python is available:

```powershell
python --version
```

If that opens the Microsoft Store instead of showing a Python version, turn off the Python app execution aliases in Windows Settings under **Apps > Advanced app settings > App execution aliases**, then open a new PowerShell window and try again.

From the project folder, install the runtime dependencies:

```powershell
pip install pyvda pywin32
```

If you want to build the standalone executable, also install PyInstaller:

```powershell
pip install pyinstaller
```

To build the Windows installer, install Inno Setup 6 from the official Inno Setup site and make sure `ISCC.exe` is available either on your `PATH` or in the default Inno Setup install folder.

## Run From Source

Start the overlay directly with Python:

```powershell
python .\workspace_label.py
```

On first launch, the app creates a per-user config file at:

```powershell
$env:LOCALAPPDATA\Desktop Labeller\desktops.json
```

The JSON config controls the displayed workspace names, text color, and overlay scale:

```json
{
  "names": [
    "[1] first workspace",
    "[2] second workspace",
    "[3] third workspace",
    "[4] fourth workspace",
    "[5] fifth workspace",
    "[6] sixth workspace",
    "[7] seventh workspace"
  ],
  "font_rgba": [
    135,
    118,
    84,
    1.0
  ],
  "size_scale": 1.3
}
```

![Desktop Labeller example overlay and JSON config](example.png)

If you have more virtual desktops than names, the app falls back to numbered labels such as `[8]`. The `font_rgba` value uses red, green, blue, and alpha values. The `size_scale` value accepts `0.5` through `3.0`, where `1.0` is the default size.

If an older local `desktops.txt` exists beside the script, the app copies those names into the AppData JSON config the first time it creates the new config file.

Click the gear at the far left of the overlay to open the config file in your default text editor. Saved changes are picked up automatically while the overlay is running.

## Build the Executable

Build the bundled app with the included PyInstaller spec file:

```powershell
pyinstaller .\workspace_label.spec
```

The generated executable is written under `dist\workspace_label\workspace_label.exe`.

## Build the Windows Installer

Build the PyInstaller app bundle and compile the installer with:

```powershell
.\build_installer.ps1
```

The generated installer is written to:

```text
installer-output\Desktop-Labeller-Setup.exe
```

If you have already rebuilt the PyInstaller output and only want to recompile the installer, run:

```powershell
.\build_installer.ps1 -SkipPyInstaller
```

The installer uses Inno Setup and installs Desktop Labeller into the current user's local programs folder. During installation, you can optionally create a desktop shortcut or start the app automatically when you sign in.

## Control the Built App

After building, use the PowerShell controller script:

```powershell
.\controller.ps1 -Start
.\controller.ps1 -Stop
.\controller.ps1 -Restart
```

The controller looks for `workspace_label.exe` in the project folder, `dist\`, or `dist\workspace_label\`.

## Optional Windows Service

The controller can also register and remove a Windows service:

```powershell
.\controller.ps1 -RegisterService
.\controller.ps1 -RemoveService
```

Run PowerShell as Administrator for these commands. Because Windows services run outside the normal interactive desktop session, direct startup with `-Start` is usually the better option for an overlay that needs to appear on your desktop.

## Customization

- Edit `$env:LOCALAPPDATA\Desktop Labeller\desktops.json` to rename the virtual desktop labels.
- Click the gear at the far left of the overlay to open the labels config directly.
- Change `font_rgba` in the JSON config to set the label text color.
- Change `size_scale` in the JSON config to resize the overlay.
- Change the `root.geometry("+20+20")` value in `workspace_label.py` to move the overlay.
- Change `lbl.pack(side="left", padx=2)` to `side="top"` in `workspace_label.py` if you prefer a vertical list.

## Troubleshooting

- If the overlay does not appear, confirm that the process is running and that Windows virtual desktops are available.
- If labels are missing, check `$env:LOCALAPPDATA\Desktop Labeller\desktops.json` and restart the app.
- If the overlay shows `JSON: Bad format`, fix the JSON syntax in the config file and save it.
- If desktop switching does not work, reinstall `pyvda` and make sure the app is running on Windows.
- If controller commands fail to find the executable, rebuild with PyInstaller or place `workspace_label.exe` beside `controller.ps1`.
- If installer creation fails with `ISCC.exe was not found`, install Inno Setup 6 or add its install folder to your `PATH`.

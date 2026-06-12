#define MyAppName "Desktop Labeller"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Desktop Labeller"
#define MyAppExeName "workspace_label.exe"

[Setup]
AppId={{81E51DB5-657A-4A60-9BC6-50A0D46BA05A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer-output
OutputBaseFilename=Desktop-Labeller-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
SetupIconFile=desktop_labeller.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start Desktop Labeller when I sign in"; GroupDescription: "Startup options:"; Flags: unchecked

[Files]
Source: "dist\workspace_label\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "controller.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Desktop Labeller"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Stop Desktop Labeller"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\controller.ps1"" -Stop"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall Desktop Labeller"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Desktop Labeller"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\Desktop Labeller"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Desktop Labeller"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\controller.ps1"" -Stop"; WorkingDir: "{app}"; Flags: runhidden; RunOnceId: "StopDesktopLabeller"

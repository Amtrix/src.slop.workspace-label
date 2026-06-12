<#
.SYNOPSIS
    Builds the Desktop Labeller Windows installer.
.DESCRIPTION
    Rebuilds the PyInstaller bundle, then compiles installer.iss with Inno Setup.
    Install Inno Setup 6 from https://jrsoftware.org/isdl.php before running this script.
#>
param (
    [Parameter(Mandatory = $false)][switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$SpecPath = Join-Path $ProjectDir "workspace_label.spec"
$InstallerScript = Join-Path $ProjectDir "installer.iss"
$BundledExe = Join-Path $ProjectDir "dist\workspace_label\workspace_label.exe"
$InstallerOutput = Join-Path $ProjectDir "installer-output\Desktop-Labeller-Setup.exe"

function Get-InnoSetupCompiler {
    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @()
    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $programFiles = [Environment]::GetFolderPath("ProgramFiles")

    if ($programFilesX86) {
        $candidates += (Join-Path $programFilesX86 "Inno Setup 6\ISCC.exe")
    }
    if ($programFiles) {
        $candidates += (Join-Path $programFiles "Inno Setup 6\ISCC.exe")
    }

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Invoke-PyInstallerBuild {
    param (
        [Parameter(Mandatory = $true)][string]$SpecPath
    )

    $pyInstaller = Get-Command "pyinstaller" -ErrorAction SilentlyContinue
    if ($pyInstaller) {
        & $pyInstaller.Source "--noconfirm" $SpecPath
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    $pythonCommands = @()
    $pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $pythonCommands += @{ Exe = $pyLauncher.Source; Args = @("-m", "PyInstaller", "--noconfirm", $SpecPath) }
    }

    $pyLauncherPaths = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"),
        (Join-Path $env:WINDIR "py.exe")
    )

    foreach ($pyLauncherPath in $pyLauncherPaths) {
        if (Test-Path $pyLauncherPath) {
            $pythonCommands += @{ Exe = $pyLauncherPath; Args = @("-m", "PyInstaller", "--noconfirm", $SpecPath) }
        }
    }

    $pythonInstallRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $pythonInstallRoot) {
        Get-ChildItem $pythonInstallRoot -Directory -Filter "Python*" |
        Sort-Object Name -Descending |
        ForEach-Object {
            $pythonExe = Join-Path $_.FullName "python.exe"
            if (Test-Path $pythonExe) {
                $pythonCommands += @{ Exe = $pythonExe; Args = @("-m", "PyInstaller", "--noconfirm", $SpecPath) }
            }
        }
    }

    foreach ($pythonCommand in $pythonCommands) {
        & $pythonCommand.Exe @($pythonCommand.Args)
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    throw "PyInstaller was not found or failed to run. Install it with: py -m pip install pyinstaller"
}

Push-Location $ProjectDir
try {
    if (-not $SkipPyInstaller) {
        # Stop any running instance so PyInstaller can replace the locked bundle (e.g. pywintypes313.dll).
        $running = Get-Process "workspace_label" -ErrorAction SilentlyContinue
        if ($running) {
            Write-Host "[*] Stopping running Desktop Labeller instance..." -ForegroundColor Yellow
            $running | Stop-Process -Force
            Start-Sleep -Milliseconds 500
        }

        Write-Host "[*] Building PyInstaller bundle..." -ForegroundColor Cyan
        Invoke-PyInstallerBuild -SpecPath $SpecPath
    }

    if (-not (Test-Path $BundledExe)) {
        throw "Expected executable was not found at '$BundledExe'. Run PyInstaller before building the installer."
    }

    $innoCompiler = Get-InnoSetupCompiler
    if (-not $innoCompiler) {
        throw "Inno Setup compiler ISCC.exe was not found. Install Inno Setup 6, then rerun this script."
    }

    Write-Host "[*] Compiling installer with Inno Setup..." -ForegroundColor Cyan
    & $innoCompiler $InstallerScript

    if (Test-Path $InstallerOutput) {
        Write-Host "[+] Installer created: $InstallerOutput" -ForegroundColor Green
    }
    else {
        Write-Host "[+] Inno Setup completed. Check the installer-output folder for the generated installer." -ForegroundColor Green
    }
}
finally {
    Pop-Location
}

<#
.SYNOPSIS
    Controls the Workspace Label background utility.
.DESCRIPTION
    Allows Starting, Stopping, Restarting, and registering/unregistering
    the workspace application as a native Windows Background Service.
.EXAMPLE
    .\workspace-ctl.ps1 -Stop
#>
param (
    [Parameter(Mandatory = $false)][switch]$Start,
    [Parameter(Mandatory = $false)][switch]$Stop,
    [Parameter(Mandatory = $false)][switch]$Restart,
    [Parameter(Mandatory = $false)][switch]$RegisterService,
    [Parameter(Mandatory = $false)][switch]$RemoveService
)

# --- CONFIGURATION ---
$ExeName = "workspace_label.exe"
$ServiceName = "WindowsWorkspaceLabel"
$ServiceDesc = "Displays active Windows 11 virtual desktop indices in the background."
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --- AUTOMATIC PATH RESOLUTION LOGIC ---
$ExePath = $null
$WorkingDir = $ScriptDir

# 1. Check current directory where the script lives
$PathLocal = Join-Path $ScriptDir $ExeName
# 2. Check directly inside dist\ directory
$PathDistRoot = Join-Path $ScriptDir "dist\$ExeName"
# 3. Check inside dist\workspace_label\ directory (PyInstaller default folder output)
$PathDistFolder = Join-Path $ScriptDir "dist\workspace_label\$ExeName"

if (Test-Path $PathLocal) {
    $ExePath = $PathLocal
    $WorkingDir = $ScriptDir
}
elseif (Test-Path $PathDistFolder) {
    $ExePath = $PathDistFolder
    $WorkingDir = Split-Path $PathDistFolder -Parent
}
elseif (Test-Path $PathDistRoot) {
    $ExePath = $PathDistRoot
    $WorkingDir = Split-Path $PathDistRoot -Parent
}

# Function to check for Administrative privileges (Required for Services)
function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Throw "This operation requires Administrator privileges. Please relaunch PowerShell as Admin."
    }
}

# 1. KILL / STOP ACTION
if ($Stop -or $Restart) {
    Write-Host "[*] Stopping Workspace Label..." -ForegroundColor Cyan

    # Stop native Windows service if running
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Assert-Admin
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    }

    # Force-kill orphaned background processes
    $Processes = Get-Process -Name ([System.IO.Path]::GetFileNameWithoutExtension($ExeName)) -ErrorAction SilentlyContinue
    if ($Processes) {
        $Processes | Stop-Process -Force
        Write-Host "[+] Background process terminated." -ForegroundColor Green
    }
    else {
        Write-Host "[-] Process was not running." -ForegroundColor Yellow
    }
}

# 2. START ACTION
if ($Start -or $Restart) {
    if ($null -eq $ExePath) {
        Throw "Executable '$ExeName' not found in script directory or inside 'dist/'. Build it via PyInstaller first."
    }

    Write-Host "[*] Found executable at: $ExePath" -ForegroundColor Gray

    # If installed as a service, use service architecture to spin up
    if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) -and (Get-Service -Name $ServiceName).Status -ne "Running") {
        Assert-Admin
        Write-Host "[*] Starting via Windows Service infrastructure..." -ForegroundColor Cyan
        Start-Service -Name $ServiceName
    }
    else {
        # Fallback to direct asynchronous process spawn
        Write-Host "[*] Spawning process independently in background..." -ForegroundColor Cyan
        Start-Process -FilePath $ExePath -WorkingDirectory $WorkingDir -WindowStyle Hidden
    }
    Write-Host "[+] Workspace Label successfully triggered." -ForegroundColor Green
}

# 3. REGISTER WINDOWS SERVICE
if ($RegisterService) {
    Assert-Admin
    if ($null -eq $ExePath) {
        Throw "Cannot register service: Executable '$ExeName' missing from local directory or 'dist/'"
    }

    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Host "[-] Service '$ServiceName' is already registered." -ForegroundColor Yellow
    }
    else {
        Write-Host "[*] Found executable at: $ExePath" -ForegroundColor Gray
        Write-Host "[*] Registering '$ServiceName' as a Windows Background Service..." -ForegroundColor Cyan
        # Creates a native Windows service wrapping your background script setup
        New-Service -Name $ServiceName -BinaryPathName "`"$ExePath`"" -DisplayName "Workspace Label Overlay" -Description $ServiceDesc -StartupType Automatic
        Write-Host "[+] Service successfully registered to start automatically on system boot!" -ForegroundColor Green
    }
}

# 4. REMOVE WINDOWS SERVICE
if ($RemoveService) {
    Assert-Admin
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        Write-Host "[*] Removing Windows Service configuration..." -ForegroundColor Cyan
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        Remove-Service -Name $ServiceName
        Write-Host "[+] Service '$ServiceName' completely removed from the operating system." -ForegroundColor Green
    }
    else {
        Write-Host "[-] Service target configuration not found." -ForegroundColor Yellow
    }
}

# Fallback hint if user types nothing
if (-not ($Start -or $Stop -or $Restart -or $RegisterService -or $RemoveService)) {
    Write-Host "No control flag passed. Usage examples:" -ForegroundColor Yellow
    Write-Host "  .\workspace-ctl.ps1 -Start"
    Write-Host "  .\workspace-ctl.ps1 -Stop"
    Write-Host "  .\workspace-ctl.ps1 -Restart"
    Write-Host "  .\workspace-ctl.ps1 -RegisterService  (Run PowerShell as Admin)"
    Write-Host "  .\workspace-ctl.ps1 -RemoveService    (Run PowerShell as Admin)"
}

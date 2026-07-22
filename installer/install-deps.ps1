<#
    Meeting System - installer bootstrap
    ====================================
    Run automatically by the Windows installer (MeetingSystem-Setup.exe) right
    after the application files are copied. It finishes the job setup.ps1 can
    only warn about: any missing system prerequisite is installed via winget,
    PATH is refreshed, and then setup.ps1 builds the Python/Node environments.

    Can also be run by hand from the install folder:

        powershell -ExecutionPolicy Bypass -File installer\install-deps.ps1

    Options:
      -HfToken   Hugging Face token collected by the installer wizard
      -SkipModel skip the (~4.7 GB) Ollama model pull
      -DryRun    only report what would be installed; change nothing
#>
[CmdletBinding()]
param(
    [string]$HfToken = "",
    [switch]$SkipModel,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$AppRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Say($m)  { Write-Host $m }
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Info($m) { Write-Host "  [info] $m" -ForegroundColor Gray }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "Meeting System - first-time setup" -ForegroundColor Green
Write-Host ("=" * 50)
Say "Install folder: $AppRoot"
if ($DryRun) { Warn "DRY RUN: nothing will be installed or changed." }

# ---------- 1. system prerequisites via winget ----------
Step "System prerequisites"

$haveWinget = Has "winget"
if (-not $haveWinget) {
    Warn "winget (Windows App Installer) is not available. Prerequisites cannot be auto-installed."
    Warn "Install these manually if missing, then re-run this script:"
    Warn "  Python 3.13   https://www.python.org/downloads/   (tick 'Add python.exe to PATH')"
    Warn "  Node.js LTS   https://nodejs.org"
    Warn "  FFmpeg        https://www.gyan.dev/ffmpeg/builds/"
    Warn "  Ollama        https://ollama.com/download"
}

# command-to-check -> winget package id
$deps = @(
    @{ Cmd = "python"; Name = "Python 3.13";  Id = "Python.Python.3.13" },
    @{ Cmd = "node";   Name = "Node.js LTS";  Id = "OpenJS.NodeJS.LTS" },
    @{ Cmd = "ffmpeg"; Name = "FFmpeg";       Id = "Gyan.FFmpeg" },
    @{ Cmd = "ollama"; Name = "Ollama";       Id = "Ollama.Ollama" }
)

foreach ($dep in $deps) {
    if (Has $dep.Cmd) {
        Ok "$($dep.Name) already installed."
        continue
    }
    if ($DryRun) { Info "WOULD install $($dep.Name) (winget id $($dep.Id))."; continue }
    if (-not $haveWinget) { Warn "$($dep.Name) missing - install it manually (see above)."; continue }
    Info "Installing $($dep.Name)... (this may take a few minutes)"
    winget install --id $dep.Id -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -eq 0) { Ok "$($dep.Name) installed." }
    else { Warn "$($dep.Name) install returned code $LASTEXITCODE - it may need a manual install." }
}

# ---------- 2. refresh PATH so the new tools are visible NOW ----------
if (-not $DryRun) {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

# ---------- 3. build the app environments (setup.ps1 does the real work) ----------
Step "Application environment"
if ($DryRun) {
    Info "WOULD run setup.ps1 (Python venv + PyTorch + dependencies, .env, Ollama model, npm install)."
} else {
    $setupArgs = @("-NonInteractive")
    if ($HfToken)  { $setupArgs += @("-HfToken", $HfToken) }
    if ($SkipModel) { $setupArgs += "-SkipModel" }
    & (Join-Path $AppRoot "setup.ps1") @setupArgs
}

# ---------- 4. done ----------
Step "Finished"
Say "Launch the system from the Start Menu shortcut 'Meeting System'"
Say "(or run.ps1 in the install folder). The web interface opens at:"
Say "    http://localhost:5173"
Say ""
Say "One manual step remains if you haven't done it: accept the 3 gated"
Say "pyannote model licences (free, one click each while logged in to"
Say "huggingface.co) - the installer offered to open those pages."
Say ""
Read-Host "Press Enter to close this window"

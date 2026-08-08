<#
    Kairos - start the system (Windows)
    ==============================================
    Starts the backend (API, port 8000) and the frontend (web UI, port 5173)
    HIDDEN, waits until they are genuinely ready, then opens the browser.

        powershell -ExecutionPolicy Bypass -File run.ps1

    The user never sees a server console and never has to remember a URL. This
    window stays as the single Kairos control window: it shows the system's
    startup introduction and stops both servers when it closes.

    Server output is not lost - it is written to logs\ so a failure can still
    be diagnosed after the fact, which the old visible-window design could not
    do once the window was closed.

    Closing this window with the X button stops the servers too - a watchdog
    process makes sure of it (see Start-Watchdog). Windows does not reliably let
    PowerShell run cleanup code when a console is closed that way, so relying on
    a finally block alone would leave hidden servers running.

    Switches:
      -NoBrowser   start the servers but do not open a browser
      -Stop        stop a running instance and exit
      -Status      report whether Kairos is running, then exit
#>
param(
    [switch]$NoBrowser,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend  = Join-Path $ProjectRoot "backend"
$Frontend = Join-Path $ProjectRoot "frontend"
$LogDir   = Join-Path $ProjectRoot "logs"
$PidFile  = Join-Path $LogDir "kairos.pids"

$BackendUrl  = "http://127.0.0.1:8000"
$FrontendUrl = "http://localhost:5173"   # localhost, not 127.0.0.1 - Vite serves on IPv6

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null


function Test-Endpoint {
    <# $true when the URL answers at all. Any HTTP reply means the server is
       listening, so a 404 still counts as "up". #>
    # A short timeout matters while polling: once uvicorn binds the port the
    # connection is accepted but nothing answers until the app has finished
    # importing, so a long timeout makes every poll block for its full duration
    # and stretches a one-minute start into several.
    param([string]$Url, [int]$TimeoutSec = 1)
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec | Out-Null
        return $true
    } catch [System.Net.WebException] {
        return ($null -ne $_.Exception.Response)
    } catch {
        # PowerShell 6+ raises HttpResponseException for non-2xx - still "up".
        return ($_.Exception.GetType().Name -eq "HttpResponseException")
    }
}

function Stop-Kairos {
    <# Kill the tracked processes AND their children. npm spawns node as a child,
       so killing only the launched process would leave port 5173 bound and the
       next launch would fail.

       Most calls find at least one process already gone - the watchdog may have
       cleaned up, or the file may be left over from a previous run. taskkill
       reports that on stderr, and PowerShell 5.1 turns a native command's stderr
       into a terminating error while ErrorActionPreference is 'Stop', which
       would abort the launcher over an entirely expected condition. Hence the
       existence check and the local preference override. #>
    if (-not (Test-Path $PidFile)) { return }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        foreach ($line in Get-Content $PidFile) {
            $procId = ($line -split '=')[-1].Trim()
            if ($procId -match '^\d+$' -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
                & taskkill /PID $procId /T /F 2>&1 | Out-Null
            }
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

function Confirm-Stopped {
    <# Verify the shutdown actually happened rather than just claiming it.
       The ports being free is the check that matters: it is what would stop
       Kairos starting again, and it is something the user can see for
       themselves in Task Manager if they want to double-check. #>
    param([int]$TimeoutSec = 10)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $busy = @(8000, 5173) | Where-Object {
            Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue
        }
        if (-not $busy) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

function Start-Watchdog {
    <# Guarantee the servers die with this window.

       Clicking the X on a console does not give PowerShell a dependable chance
       to run its finally block, so a user who closes the window that way would
       otherwise leave hidden python and node processes holding ports 8000 and
       5173. This launches a small hidden process that simply waits for THIS
       process to exit and then kills both servers and their children.

       It costs one idle process and works no matter how this window ends -
       X button, Task Manager, or a crash. #>
    param([int]$BackendPid, [int]$FrontendPid)
    # Clears the PID file too, so a window closed with the X does not leave a
    # stale record behind for the next run to trip over.
    $cmd = "Wait-Process -Id $PID -ErrorAction SilentlyContinue; " +
           "taskkill /PID $BackendPid /T /F; taskkill /PID $FrontendPid /T /F; " +
           "Remove-Item '$PidFile' -ErrorAction SilentlyContinue"
    return Start-Process powershell -PassThru -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd
    )
}

function Start-Hidden {
    <# Launch an executable hidden, with its output captured to the log
       directory. Returns the process object so we can track its PID.

       The executable is invoked DIRECTLY rather than through a PowerShell
       wrapper that activates the virtual environment: calling the venv's own
       python.exe is equivalent for imports, and it avoids a layer of nested
       quoting that silently swallowed the backend's output. #>
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDir, [string]$LogName)
    return Start-Process -FilePath $FilePath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDir -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "$LogName.out.log") `
        -RedirectStandardError  (Join-Path $LogDir "$LogName.err.log")
}

function Show-StartupBanner {
    <# Echo the introduction the backend itself printed. The banner is a marked
       FYP requirement ("the program must begin with a personal and system
       introduction"), so it is surfaced here rather than buried in a log now
       that the server window is hidden. Printed from the log, so what is shown
       is genuinely what the program produced, not a copy that could drift. #>
    $log = Join-Path $LogDir "backend.out.log"
    if (-not (Test-Path $log)) { return }
    $text = Get-Content $log -Raw -ErrorAction SilentlyContinue
    if (-not $text) { return }
    $lines = $text -split "`r?`n"
    $start = ($lines | Select-String -Pattern '^\s*=+\s*$' | Select-Object -First 1).LineNumber
    if (-not $start) { return }
    $banner = $lines[($start - 1)..([Math]::Min($start + 20, $lines.Count - 1))]
    Write-Host ""
    # Print from the opening fence to the CLOSING fence. Both fences are the
    # same string, so stop on the second one by position - comparing the text
    # would match the opening line and never terminate.
    for ($i = 0; $i -lt $banner.Count; $i++) {
        Write-Host "  $($banner[$i])" -ForegroundColor Cyan
        if ($i -gt 0 -and $banner[$i] -match '^\s*=+\s*$') { break }
    }
}

function Show-FailureHelp {
    param([string]$Which)
    Write-Host ""
    Write-Host "  Kairos could not start: the $Which did not come up in time." -ForegroundColor Red
    Write-Host ""
    foreach ($suffix in @("err", "out")) {
        $log = Join-Path $LogDir "$Which.$suffix.log"
        if (Test-Path $log) {
            $tail = Get-Content $log -Tail 15 -ErrorAction SilentlyContinue
            if ($tail) {
                Write-Host "  --- last lines of $Which.$suffix.log ---" -ForegroundColor Yellow
                $tail | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
            }
        }
    }
    Write-Host ""
    Write-Host "  Full logs: $LogDir" -ForegroundColor Gray
    Write-Host ""
}


# --- -Status: report and leave --------------------------------------------
if ($Status) {
    $backendUp  = Test-Endpoint $BackendUrl
    $frontendUp = Test-Endpoint $FrontendUrl
    if ($backendUp -or $frontendUp) {
        Write-Host "Kairos is RUNNING." -ForegroundColor Yellow
        Write-Host ("  AI pipeline service (port 8000) : {0}" -f $(if ($backendUp) { "up" } else { "down" }))
        Write-Host ("  Web interface       (port 5173) : {0}" -f $(if ($frontendUp) { "up" } else { "down" }))
        if (Test-Path $PidFile) {
            Write-Host "  Processes:" -ForegroundColor Gray
            Get-Content $PidFile | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
        }
        Write-Host "Stop it with:  run.ps1 -Stop" -ForegroundColor Gray
    } else {
        Write-Host "Kairos is NOT running. Ports 8000 and 5173 are free." -ForegroundColor Green
    }
    exit 0
}

# --- -Stop: shut down and leave -------------------------------------------
if ($Stop) {
    Stop-Kairos
    if (Confirm-Stopped) {
        Write-Host "Kairos stopped. Ports 8000 and 5173 are free." -ForegroundColor Green
        exit 0
    }
    Write-Host "Something is still holding port 8000 or 5173." -ForegroundColor Yellow
    Write-Host "Check Task Manager for python.exe or node.exe." -ForegroundColor Gray
    exit 1
}

# --- already running? just open the browser -------------------------------
# Double-clicking the shortcut twice should not start a second copy or fail on
# a bound port - it should simply bring the user to the interface.
if ((Test-Endpoint $BackendUrl) -and (Test-Endpoint $FrontendUrl)) {
    Write-Host "Kairos is already running." -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process $FrontendUrl }
    Write-Host "Opened $FrontendUrl" -ForegroundColor Green
    exit 0
}

# A previous run may have been closed without cleanup, leaving hidden servers
# holding the ports. Clear them before starting.
Stop-Kairos

$venvPython = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Backend not set up yet. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

Clear-Host
Write-Host ""
Write-Host "  Starting Kairos..." -ForegroundColor Cyan
Write-Host ""

# -u keeps Python's output unbuffered. Without it the startup introduction and
# the server's log lines sit in a buffer instead of reaching the log file, so
# nothing could be shown to the user or used to diagnose a failed start.
$backendProc = Start-Hidden -FilePath $venvPython -WorkingDir $Backend -LogName "backend" `
    -Arguments @("-u", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000")

$frontendProc = Start-Hidden -FilePath "npm.cmd" -WorkingDir $Frontend -LogName "frontend" `
    -Arguments @("run", "dev")

# Started before the readiness wait, so the servers are already covered if the
# user closes the window while it is still starting up.
$watchdogProc = Start-Watchdog -BackendPid $backendProc.Id -FrontendPid $frontendProc.Id

Set-Content -Path $PidFile -Encoding utf8 -Value @(
    "backend=$($backendProc.Id)",
    "frontend=$($frontendProc.Id)",
    "watchdog=$($watchdogProc.Id)"
)

# --- wait until both are genuinely serving --------------------------------
# The old design told the user to watch for the word "ready" and then type the
# URL themselves. Polling removes both steps: the browser opens only once the
# interface will actually load.
#
# The allowance is generous on purpose. Importing torch, faster-whisper,
# whisperx and pyannote takes a long time on a cold filesystem cache - well
# over a minute on the development machine - and a launcher that gives up while
# the server is still legitimately starting is worse than one that waits.
$deadline = (Get-Date).AddSeconds(300)
$backendUp = $false
$frontendUp = $false

Write-Host "  Starting the AI pipeline service (takes a minute or two)..." -NoNewline
while ((Get-Date) -lt $deadline -and -not $backendUp) {
    if ($backendProc.HasExited) { break }
    $backendUp = Test-Endpoint "$BackendUrl/health"
    if (-not $backendUp) { Start-Sleep -Milliseconds 700; Write-Host "." -NoNewline }
}
if (-not $backendUp) { Write-Host ""; Show-FailureHelp "backend"; Stop-Kairos; Read-Host "  Press Enter to close"; exit 1 }
Write-Host " ready" -ForegroundColor Green

Write-Host "  Starting the web interface..." -NoNewline
while ((Get-Date) -lt $deadline -and -not $frontendUp) {
    if ($frontendProc.HasExited) { break }
    $frontendUp = Test-Endpoint $FrontendUrl
    if (-not $frontendUp) { Start-Sleep -Milliseconds 700; Write-Host "." -NoNewline }
}
if (-not $frontendUp) { Write-Host ""; Show-FailureHelp "frontend"; Stop-Kairos; Read-Host "  Press Enter to close"; exit 1 }
Write-Host " ready" -ForegroundColor Green

Show-StartupBanner

if (-not $NoBrowser) {
    Start-Process $FrontendUrl
    Write-Host ""
    Write-Host "  Kairos is open in your browser." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  Kairos is running at $FrontendUrl" -ForegroundColor Green
}

Write-Host ""
Write-Host "  Everything runs on this computer. Nothing is uploaded." -ForegroundColor Gray
Write-Host "  Leave this window open while you work." -ForegroundColor Gray
Write-Host "  Closing this window also stops Kairos." -ForegroundColor Gray
Write-Host ""

try {
    Read-Host "  Press Enter here to stop Kairos"
} finally {
    Write-Host ""
    Write-Host "  Stopping Kairos..." -ForegroundColor Cyan
    Stop-Kairos
    # Say whether it actually stopped, rather than assuming. The user asked to
    # shut down; they are entitled to see that it happened.
    if (Confirm-Stopped) {
        Write-Host "  Stopped. Ports 8000 and 5173 are free." -ForegroundColor Green
    } else {
        Write-Host "  Something is still holding port 8000 or 5173." -ForegroundColor Yellow
        Write-Host "  Check Task Manager for python.exe or node.exe." -ForegroundColor Gray
    }
    Start-Sleep -Seconds 2
}

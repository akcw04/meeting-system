<#
    Kairos - start both servers (Windows)
    ==============================================
    Launches the backend (API, port 8000) and the frontend (web UI, port 5173)
    each in its own window, so you don't have to juggle two terminals.

        powershell -ExecutionPolicy Bypass -File run.ps1

    Then open http://localhost:5173 in your browser.
    Close either window to stop that server.
#>
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend  = Join-Path $ProjectRoot "backend"
$Frontend = Join-Path $ProjectRoot "frontend"

$venvActivate = Join-Path $Backend ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Host "Backend not set up yet. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Starting backend (http://127.0.0.1:8000) ..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit","-Command",
    "cd `"$Backend`"; . `"$venvActivate`"; Write-Host 'BACKEND - Kairos API' -ForegroundColor Green; uvicorn app.main:app --host 127.0.0.1 --port 8000"
)

Start-Sleep -Seconds 2

Write-Host "Starting frontend (http://localhost:5173) ..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit","-Command",
    "cd `"$Frontend`"; Write-Host 'FRONTEND - Kairos UI' -ForegroundColor Green; npm run dev"
)

Write-Host "`nBoth servers are starting in their own windows." -ForegroundColor Green
Write-Host "Open http://localhost:5173 once the frontend window says 'ready'." -ForegroundColor Green
Write-Host "(Use localhost, not 127.0.0.1 - Vite serves on IPv6 localhost.)" -ForegroundColor Gray

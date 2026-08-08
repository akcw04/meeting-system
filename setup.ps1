<#
    Kairos - one-command setup (Windows)
    ============================================
    Takes a new machine from a fresh clone to a runnable system with a single
    command, doing by hand what would otherwise be a dozen ordered steps:

        powershell -ExecutionPolicy Bypass -File setup.ps1

    It AUTO-ADAPTS to the machine: if an NVIDIA GPU is present it installs the
    CUDA build of PyTorch (fast); otherwise it installs the CPU build and
    switches the app to CPU mode automatically.

    Safe to re-run: it skips work that is already done and never overwrites an
    existing backend/.env (your Hugging Face token is preserved).

    What it CANNOT do for you (it will tell you clearly at the end):
      - accept the 3 gated pyannote model licences on huggingface.co
      - install system tools it can't find (Python, FFmpeg, Ollama, Node)

    Options:
      -CpuOnly     force CPU mode even if an NVIDIA GPU is detected
      -HfToken     supply your Hugging Face token non-interactively
      -SkipModel   skip the (~4.7 GB) Ollama model pull
      -SkipFrontend  skip the npm install for the web UI
      -NonInteractive  never prompt (for the packaged installer); a missing
                       HF token is left blank with a warning instead
#>
[CmdletBinding()]
param(
    [switch]$CpuOnly,
    [string]$HfToken = "",
    [switch]$SkipModel,
    [switch]$SkipFrontend,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $ProjectRoot "backend"
$Frontend = Join-Path $ProjectRoot "frontend"
$script:Warnings = @()

# ---------- pretty output helpers ----------
function Say($m)  { Write-Host $m }
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Info($m) { Write-Host "  [info] $m" -ForegroundColor Gray }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow; $script:Warnings += $m }
function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "Kairos - setup" -ForegroundColor Green
Write-Host ("=" * 50)
Say "Project: $ProjectRoot"

# ---------- 0. prerequisites ----------
Step "Checking prerequisites"

if (Has "python") {
    $pyv = (python --version 2>&1).ToString().Trim()
    Ok "Python found: $pyv"
    if ($pyv -notmatch "3\.13") { Warn "This project was built and tested on Python 3.13.x - you have '$pyv'. It may still work, but 3.13 is the tested version." }
} else {
    Warn "Python NOT found. Install Python 3.13 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run."
    Write-Host "`nCannot continue without Python. Stopping." -ForegroundColor Red
    exit 1
}

if (Has "ffmpeg") { Ok "FFmpeg found" } else {
    Warn "FFmpeg NOT found on PATH. Download the 'essentials' build from https://www.gyan.dev/ffmpeg/builds/, extract it so ffmpeg.exe sits at C:\ffmpeg\bin\ffmpeg.exe, and add C:\ffmpeg\bin to your user PATH. Do NOT put it under a OneDrive-synced folder. Alternatively set FFMPEG_PATH in backend/.env. Uploads will fail without it."
}
if (Has "ollama") { Ok "Ollama found" } else {
    Warn "Ollama NOT found. Install from https://ollama.com/download - needed to generate meeting insights."
}
if (Has "node") { Ok ("Node.js found: " + (node --version)) } else {
    Warn "Node.js NOT found. Install the 22.x LTS from https://nodejs.org - needed for the web interface."
}
if (Has "git") { Ok "Git found" } else { Info "Git not found (only needed if you cloned via git; not required to run)." }

# free disk space
try {
    $freeGB = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
    if ($freeGB -lt 40) { Warn "Only $freeGB GB free on C:. Models + deps need ~40 GB." } else { Ok "$freeGB GB free on C:" }
} catch { }

# ---------- 1. GPU / device decision ----------
Step "Detecting GPU"
$useGpu = $false
if ($CpuOnly) {
    Info "-CpuOnly given: installing CPU build."
} elseif (Has "nvidia-smi") {
    try {
        $gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($gpuName) { $useGpu = $true; Ok "NVIDIA GPU detected: $($gpuName.Trim()) -> installing CUDA build (fast)." }
        else { Info "nvidia-smi present but no GPU reported -> CPU build." }
    } catch { Info "nvidia-smi failed -> CPU build." }
} else {
    Info "No NVIDIA GPU detected -> installing CPU build. Transcription will be slower but fully functional."
}

# ---------- 2. backend: venv + torch + requirements ----------
Step "Backend Python environment"
Push-Location $Backend
try {
    $venv = Join-Path $Backend ".venv"
    $py = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Info "Creating virtual environment (.venv)..."
        python -m venv .venv
        Ok "Virtual environment created."
    } else { Ok "Virtual environment already exists (reusing)." }

    Info "Upgrading pip..."
    & $py -m pip install --upgrade pip --quiet

    # torch FIRST (order matters - see requirements.txt header)
    $torchInstalled = (& $py -m pip show torch 2>$null | Select-String "^Version:")
    if ($torchInstalled) {
        Ok "PyTorch already installed (skipping)."
    } else {
        if ($useGpu) {
            Info "Installing PyTorch (CUDA 12.6 build)... this is a large download."
            & $py -m pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu126
        } else {
            Info "Installing PyTorch (CPU build)..."
            & $py -m pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0
        }
        Ok "PyTorch installed."
    }

    Info "Installing remaining Python dependencies (requirements.txt)..."
    & $py -m pip install -r requirements.txt
    Ok "Backend dependencies installed."
} finally { Pop-Location }

# ---------- 3. backend/.env ----------
Step "Backend configuration (.env)"
$envPath = Join-Path $Backend ".env"
$envExample = Join-Path $Backend ".env.example"
if (Test-Path $envPath) {
    Ok "backend/.env already exists (left untouched)."
} else {
    Copy-Item $envExample $envPath
    Ok "Created backend/.env from template."
    # adapt device for CPU machines
    if (-not $useGpu) {
        (Get-Content $envPath) `
            -replace '^WHISPER_DEVICE=.*', 'WHISPER_DEVICE=cpu' `
            -replace '^WHISPER_COMPUTE_TYPE=.*', 'WHISPER_COMPUTE_TYPE=int8' |
            Set-Content $envPath -Encoding UTF8
        Ok "Set WHISPER_DEVICE=cpu / WHISPER_COMPUTE_TYPE=int8 for a GPU-less machine."
    }
    # HF token
    if (-not $HfToken -and -not $NonInteractive) {
        Say ""
        Say "  A Hugging Face token is needed for the speaker-diarization models."
        Say "  Get a free 'Read' token at https://huggingface.co/settings/tokens"
        $HfToken = Read-Host "  Paste your HF token now (or press Enter to fill in later)"
    }
    if ($HfToken) {
        (Get-Content $envPath) -replace '^HF_TOKEN=.*', ("HF_TOKEN=" + $HfToken) | Set-Content $envPath -Encoding UTF8
        Ok "Saved HF token to backend/.env."
    } else {
        Warn "HF_TOKEN left blank in backend/.env - set it before processing meetings, or diarization will fail."
    }
}

# ---------- 4. Ollama model ----------
Step "Local language model (Ollama)"
if (-not $SkipModel -and (Has "ollama")) {
    $models = (ollama list 2>$null | Out-String)
    if ($models -match "llama3\.1:8b") {
        Ok "Model llama3.1:8b already pulled."
    } else {
        Info "Pulling llama3.1:8b (~4.7 GB, one-time)..."
        try { ollama pull llama3.1:8b; Ok "Model pulled." }
        catch { Warn "Could not pull llama3.1:8b. Make sure Ollama is running, then run 'ollama pull llama3.1:8b' manually." }
    }
} elseif ($SkipModel) { Info "-SkipModel given: skipped." } else { Warn "Ollama missing - skipping model pull. Install Ollama, then run 'ollama pull llama3.1:8b'." }

# ---------- 5. frontend ----------
Step "Web interface (frontend)"
if (-not $SkipFrontend -and (Has "npm")) {
    Push-Location $Frontend
    try {
        if (Test-Path (Join-Path $Frontend "node_modules")) { Ok "node_modules present (reusing). Run 'npm install' in frontend/ if you pulled new code." }
        else { Info "Installing frontend packages (npm install)..."; npm install; Ok "Frontend packages installed." }
    } finally { Pop-Location }
} elseif ($SkipFrontend) { Info "-SkipFrontend given: skipped." } else { Warn "npm missing - skipping. Install Node.js, then run 'npm install' in frontend/." }

# ---------- 6. summary ----------
Step "Setup summary"
Say "Mode: $(if ($useGpu) { 'GPU (CUDA)' } else { 'CPU' })"
Say ""
Say "MANUAL step that can't be automated:"
Say "  Accept the 3 gated pyannote model licences (once per HF account) -"
Say "  log in and click Agree on each:"
Say "    https://huggingface.co/pyannote/segmentation-3.0"
Say "    https://huggingface.co/pyannote/speaker-diarization-3.1"
Say "    https://huggingface.co/pyannote/speaker-diarization-community-1"

if ($script:Warnings.Count -gt 0) {
    Write-Host "`nThings to resolve before running:" -ForegroundColor Yellow
    $script:Warnings | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
} else {
    Write-Host "`nAll checks passed." -ForegroundColor Green
}

Write-Host "`nTo start the system:" -ForegroundColor Green
Say "  double-click Kairos.bat"
Say "  (or: powershell -ExecutionPolicy Bypass -File run.ps1)"
Say "It opens Kairos in your browser once both servers are ready."

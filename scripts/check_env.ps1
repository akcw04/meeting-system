# Environment verification script for meeting-system FYP build
# Run with: powershell -ExecutionPolicy Bypass -File scripts\check_env.ps1
#
# This script ONLY READS - it does not install anything.
# Paste the full output back to Claude so we can plan Phase 0 installs.

$ErrorActionPreference = "Continue"

function Show-Result {
    param(
        [string]$Name,
        [scriptblock]$Check
    )
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    try {
        $output = & $Check 2>&1 | Out-String
        if ([string]::IsNullOrWhiteSpace($output)) {
            Write-Host "MISSING (no output)" -ForegroundColor Yellow
        } else {
            Write-Host $output.Trim()
        }
    } catch {
        Write-Host "MISSING - $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Host "Meeting-System Environment Check" -ForegroundColor Green
Write-Host "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "Machine:   $env:COMPUTERNAME / $env:USERNAME"
Write-Host ("=" * 60)

# --- Python ---
Show-Result "Python (need 3.13.x)" { python --version }
Show-Result "pip" { python -m pip --version }

# --- GPU / CUDA ---
Show-Result "NVIDIA driver + GPU (nvidia-smi)" { nvidia-smi }
Show-Result "CUDA Toolkit (nvcc, need 12.x)" { nvcc --version }

# --- Audio ---
Show-Result "FFmpeg" { ffmpeg -version | Select-Object -First 1 }

# --- Web stack ---
Show-Result "Node.js (need 22.x LTS)" { node --version }
Show-Result "npm" { npm --version }

# --- Tooling ---
Show-Result "Git" { git --version }

# --- LLM runtime ---
Show-Result "Ollama" { ollama --version }
Show-Result "Ollama installed models" { ollama list }

# --- ML / HF ---
Show-Result "Hugging Face CLI" { huggingface-cli --version }
Show-Result "Hugging Face login status" { huggingface-cli whoami }

# --- Python packages (check key ones if Python is present) ---
Write-Host ""
Write-Host "=== Python packages in current python env ===" -ForegroundColor Cyan
$pkgs = @("torch", "torchaudio", "faster-whisper", "whisperx", "pyannote.audio", "ollama", "langchain", "langgraph", "pydantic", "fastapi", "uvicorn", "docxtpl", "jinja2", "silero-vad")
foreach ($p in $pkgs) {
    try {
        $info = python -m pip show $p 2>&1 | Select-String "^Version:"
        if ($info) {
            $ver = $info.ToString().Replace("Version: ", "")
            Write-Host ("{0,-20} {1}" -f $p, $ver)
        } else {
            Write-Host ("{0,-20} (not installed)" -f $p) -ForegroundColor Yellow
        }
    } catch {
        Write-Host ("{0,-20} (check failed)" -f $p) -ForegroundColor Yellow
    }
}

# --- Disk space ---
Write-Host ""
Write-Host "=== Free disk space on C: ===" -ForegroundColor Cyan
Get-PSDrive C | Select-Object @{N="Free GB";E={[math]::Round($_.Free / 1GB, 1)}}, @{N="Used GB";E={[math]::Round($_.Used / 1GB, 1)}} | Format-Table -AutoSize

Write-Host ""
Write-Host "Done. Copy everything above and paste it back to Claude." -ForegroundColor Green

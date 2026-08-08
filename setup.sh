#!/usr/bin/env bash
# Kairos - one-command setup (macOS / Linux)
# ==================================================
# Takes a fresh clone to a runnable system in one command. Auto-adapts:
# NVIDIA GPU (Linux) -> CUDA PyTorch; otherwise (incl. all Macs) -> CPU build
# + CPU mode in .env.
#
#   bash setup.sh                 normal run (prompts for HF token)
#   HF_TOKEN=hf_xxx bash setup.sh supply token non-interactively
#   CPU_ONLY=1 bash setup.sh      force CPU build
#   SKIP_MODEL=1 / SKIP_FRONTEND=1 to skip those steps
#
# Safe to re-run: skips finished work, never overwrites an existing backend/.env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
WARN=()

cyan(){ printf '\n\033[36m=== %s ===\033[0m\n' "$1"; }
ok(){   printf '  \033[32m[ok]\033[0m   %s\n' "$1"; }
info(){ printf '  \033[90m[info]\033[0m %s\n' "$1"; }
warn(){ printf '  \033[33m[warn]\033[0m %s\n' "$1"; WARN+=("$1"); }
has(){ command -v "$1" >/dev/null 2>&1; }

echo -e "\033[32mKairos - setup\033[0m"
echo "Project: $ROOT"

# ---------- prerequisites ----------
cyan "Checking prerequisites"
if has python3; then
  PYV="$(python3 --version 2>&1)"; ok "Python found: $PYV"
  case "$PYV" in *3.13*) ;; *) warn "Built and tested on Python 3.13.x - you have '$PYV'. It may still work." ;; esac
else
  warn "python3 NOT found. Install Python 3.13 (macOS: 'brew install python@3.13'), then re-run."
  echo -e "\033[31mCannot continue without Python.\033[0m"; exit 1
fi
has ffmpeg && ok "FFmpeg found" || warn "FFmpeg NOT found. macOS: 'brew install ffmpeg'; Linux: 'sudo apt install ffmpeg'. Uploads fail without it."
has ollama && ok "Ollama found" || warn "Ollama NOT found. Install from https://ollama.com/download - needed for insights."
has node && ok "Node.js found: $(node --version)" || warn "Node.js NOT found. Install 22.x LTS from https://nodejs.org - needed for the web UI."

# ---------- GPU decision ----------
cyan "Detecting GPU"
USE_GPU=0
if [ "${CPU_ONLY:-0}" = "1" ]; then
  info "CPU_ONLY set: CPU build."
elif has nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
  USE_GPU=1; ok "NVIDIA GPU detected -> CUDA build (fast)."
else
  info "No NVIDIA GPU (expected on macOS) -> CPU build. Transcription slower but functional."
fi

# ---------- backend venv + deps ----------
cyan "Backend Python environment"
cd "$BACKEND"
PY="$BACKEND/.venv/bin/python"
if [ ! -x "$PY" ]; then info "Creating virtual environment (.venv)..."; python3 -m venv .venv; ok "Created."; else ok "Virtual environment exists (reusing)."; fi
info "Upgrading pip..."; "$PY" -m pip install --upgrade pip --quiet
if "$PY" -m pip show torch >/dev/null 2>&1; then
  ok "PyTorch already installed (skipping)."
else
  if [ "$USE_GPU" = "1" ]; then
    info "Installing PyTorch (CUDA 12.6)..."; "$PY" -m pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu126
  else
    info "Installing PyTorch (CPU)..."; "$PY" -m pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0
  fi
  ok "PyTorch installed."
fi
info "Installing requirements.txt..."; "$PY" -m pip install -r requirements.txt; ok "Backend dependencies installed."

# ---------- .env ----------
cyan "Backend configuration (.env)"
ENV="$BACKEND/.env"
if [ -f "$ENV" ]; then
  ok "backend/.env already exists (left untouched)."
else
  cp "$BACKEND/.env.example" "$ENV"; ok "Created backend/.env from template."
  if [ "$USE_GPU" != "1" ]; then
    # portable in-place edit (macOS/BSD + GNU sed)
    sed -i.bak -e 's/^WHISPER_DEVICE=.*/WHISPER_DEVICE=cpu/' -e 's/^WHISPER_COMPUTE_TYPE=.*/WHISPER_COMPUTE_TYPE=int8/' "$ENV" && rm -f "$ENV.bak"
    ok "Set WHISPER_DEVICE=cpu / int8 for a GPU-less machine."
  fi
  TOKEN="${HF_TOKEN:-}"
  if [ -z "$TOKEN" ]; then
    echo "  A Hugging Face 'Read' token is needed for diarization models (https://huggingface.co/settings/tokens)."
    read -r -p "  Paste your HF token (or Enter to fill in later): " TOKEN || true
  fi
  if [ -n "$TOKEN" ]; then
    sed -i.bak -e "s|^HF_TOKEN=.*|HF_TOKEN=$TOKEN|" "$ENV" && rm -f "$ENV.bak"; ok "Saved HF token."
  else
    warn "HF_TOKEN left blank - set it in backend/.env before processing meetings."
  fi
fi

# ---------- Ollama model ----------
cyan "Local language model (Ollama)"
if [ "${SKIP_MODEL:-0}" != "1" ] && has ollama; then
  if ollama list 2>/dev/null | grep -q "llama3.1:8b"; then ok "Model llama3.1:8b already pulled."
  else info "Pulling llama3.1:8b (~4.7 GB)..."; ollama pull llama3.1:8b && ok "Model pulled." || warn "Pull failed - run 'ollama serve' then 'ollama pull llama3.1:8b'."; fi
elif [ "${SKIP_MODEL:-0}" = "1" ]; then info "SKIP_MODEL set."; else warn "Ollama missing - run 'ollama pull llama3.1:8b' after installing it."; fi

# ---------- frontend ----------
cyan "Web interface (frontend)"
if [ "${SKIP_FRONTEND:-0}" != "1" ] && has npm; then
  cd "$FRONTEND"
  if [ -d node_modules ]; then ok "node_modules present (reusing)."; else info "npm install..."; npm install; ok "Frontend packages installed."; fi
elif [ "${SKIP_FRONTEND:-0}" = "1" ]; then info "SKIP_FRONTEND set."; else warn "npm missing - run 'npm install' in frontend/ after installing Node."; fi

# ---------- summary ----------
cyan "Setup summary"
echo "Mode: $([ "$USE_GPU" = "1" ] && echo 'GPU (CUDA)' || echo 'CPU')"
echo ""
echo "MANUAL step (once per HF account) - accept the 3 gated pyannote licences:"
echo "  https://huggingface.co/pyannote/segmentation-3.0"
echo "  https://huggingface.co/pyannote/speaker-diarization-3.1"
echo "  https://huggingface.co/pyannote/speaker-diarization-community-1"
if [ "${#WARN[@]}" -gt 0 ]; then
  printf '\n\033[33mThings to resolve before running:\033[0m\n'; for w in "${WARN[@]}"; do echo "  - $w"; done
else
  printf '\n\033[32mAll checks passed.\033[0m\n'
fi
echo -e "\n\033[32mTo start:\033[0m bash run.sh   (then open http://localhost:5173)"

# Kairos — Installation Guide

This guide walks through setting up Kairos on a fresh machine. It was tested primarily
on Windows 11; macOS and Linux notes are included where the steps differ.

There are three ways in, easiest first. Pick one — you do not need all three.

---

## Option 1 — The Windows installer (no terminal at all)

Double-click **`Kairos-Setup.exe`** in the project root, paste your free Hugging Face
token when asked, and leave **"Set up Kairos now"** ticked on the finish page.

The installer copies the application to `%LOCALAPPDATA%\Kairos` (per-user, **no
administrator rights needed**), auto-installs any missing prerequisite via winget
(Python, Node.js, FFmpeg, Ollama), builds the whole environment, downloads the AI
model, and leaves a **Kairos** shortcut in the Start Menu.

The one step it cannot do for you is accepting the three gated pyannote licences — it
offers to open those pages for a one-click *Agree* each (see step 5 below). The EXE can
be deleted afterwards; uninstall any time from Windows *Installed apps*. Details in
[`installer/README.md`](../installer/README.md).

## Option 2 — One-command setup from source

If you already have the **system tools** installed — Python 3.13, FFmpeg, Ollama and
Node.js (see the prerequisites table below) — let the setup script do the rest.

Double-click **`Kairos.bat`** and answer `Y` when it offers to set Kairos up, or from a
terminal in the project root:

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File setup.ps1
```
```bash
# macOS / Linux
bash setup.sh
```

The script **auto-detects the machine**: an NVIDIA GPU gets the fast CUDA build of
PyTorch, any other machine (including Macs) gets the CPU build and is switched to CPU
mode automatically. It creates the Python virtual environment, installs all
dependencies in the correct order, writes `backend/.env` (prompting for your Hugging
Face token), pulls the `llama3.1:8b` model, and installs the web-interface packages.
It is safe to re-run and never overwrites an existing `backend/.env`.

It cannot accept the **three gated pyannote licences** for you — it prints the links at
the end (step 5 below).

Once setup finishes, `Kairos.bat` starts the system every time you double-click it. To
start the servers from a terminal instead:

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1     # Windows
```
```bash
bash run.sh                                          # macOS / Linux
```

Open **http://localhost:5173** in your browser.

## Option 3 — Manual walkthrough

The rest of this document. Use it if you prefer to run each step yourself, or to
understand and troubleshoot what the script does.

---

## 1. Prerequisites

### Required

| Tool | Version | Why | Where to get |
|---|---|---|---|
| **Python** | 3.13.x | Backend runtime | https://www.python.org/downloads/ |
| **Node.js** | 22.x LTS | Web interface | https://nodejs.org |
| **FFmpeg** | 6+ | Audio extraction from video files | See **FFmpeg install (Windows)** below, or `brew install ffmpeg` (macOS) / `apt install ffmpeg` (Linux) |
| **Ollama** | 0.5+ | Runs Llama 3.1 locally | https://ollama.com/download |
| **Hugging Face account** | free | Downloading the pyannote.audio speaker models | https://huggingface.co/join |
| **Git** | any recent | Only if you are cloning the repository | https://git-scm.com/downloads |

### Strongly recommended

| Tool | Why |
|---|---|
| **NVIDIA GPU with ≥ 6 GB VRAM** (RTX 3060, 4050, 4060, …) | GPU acceleration makes transcription roughly 10× faster than CPU |
| **NVIDIA driver** with CUDA 12.x runtime support | Required for the above |
| **40+ GB free disk space** | Models, Python dependencies and recordings add up |

Without an NVIDIA GPU the pipeline falls back to CPU — much slower, but fully
functional. Set `WHISPER_DEVICE=cpu` and `WHISPER_COMPUTE_TYPE=int8` in `backend/.env`
(the setup script does this for you automatically).

### FFmpeg install (Windows) — important

**Install FFmpeg to a stable, non-synced folder.** Specifically `C:\ffmpeg\`. Do NOT
put it under `Documents\`, `Downloads\`, `Desktop\`, or anywhere OneDrive syncs.
OneDrive may silently move the folder to its localized "Documents" equivalent (e.g.
`OneDrive\文件\` on Chinese-locale Windows) without updating the PATH entry — and
Python subprocesses then cannot find `ffmpeg.exe`.

1. Download the latest "essentials_build" zip from https://www.gyan.dev/ffmpeg/builds/
2. Extract it. Inside is a folder like `ffmpeg-8.0.1-essentials_build\` containing
   `bin\`, `doc\`, etc.
3. Move those contents into `C:\ffmpeg\` so the final path is `C:\ffmpeg\bin\ffmpeg.exe`.
4. Add `C:\ffmpeg\bin` to your User PATH (no admin needed):
   ```powershell
   $newPath = (([Environment]::GetEnvironmentVariable('PATH', 'User') -split ';' | Where-Object { $_ -ne '' -and $_ -notmatch 'ffmpeg' }) + 'C:\ffmpeg\bin') -join ';'
   [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
   ```
5. **Close every PowerShell / terminal window** and open a fresh one. Verify:
   ```powershell
   ffmpeg -version
   ```

If you cannot or would rather not modify PATH, set
`FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe` in `backend/.env` instead.

## 2. Get the source

```powershell
git clone https://github.com/akcw04/FYP.git kairos
cd kairos
```

Or simply extract the supplied source folder and open a terminal inside it.

## 3. Backend setup (Python)

```powershell
cd backend

# Create an isolated Python environment
python -m venv .venv

# Activate it
.\.venv\Scripts\activate                # Windows PowerShell
# source .venv/bin/activate             # macOS / Linux

python -m pip install --upgrade pip

# STEP A: install PyTorch FIRST, with the CUDA 12.6 wheels.
# (CPU-only machines: pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0)
pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu126

# STEP B: then install everything else.
pip install -r requirements.txt
```

**Order matters.** `whisperx` (a `requirements.txt` dependency) hard-pins
`torch~=2.8.0` and will silently pull the plain CPU build from PyPI if no GPU torch is
already installed. Installing torch first with the explicit CUDA wheel makes whisperx
see "already satisfied" and skip the swap. Get the order wrong and you end up on CPU
torch with no GPU acceleration and roughly 10× slower transcription.

**Why CUDA 12.6 specifically:** torch 2.8.0 only ships GPU wheels for CUDA 12.6 and
12.8, and PyTorch's 12.4 index stops at torch 2.6. An NVIDIA driver can run 12.6
binaries even if the driver itself reports a different version — CUDA is backward
compatible.

## 4. Configure environment variables

```powershell
copy .env.example .env                  # Windows
# cp .env.example .env                  # macOS / Linux
```

Open `backend/.env` in any text editor. Two values matter:

**`HF_TOKEN`** — your Hugging Face read token:
1. Go to https://huggingface.co/settings/tokens
2. Click *New token*, name it, leave the type as **Read**
3. Click *Generate* and copy the token (it starts with `hf_…`)
4. Paste it after `HF_TOKEN=` in `.env`

**`FFMPEG_PATH`** — only set this if `ffmpeg` is not on your PATH. Leave it blank
otherwise. Examples:
- Gyan.dev essentials build: `C:\ffmpeg\bin\ffmpeg.exe`
- macOS (Homebrew): `/usr/local/bin/ffmpeg` or `/opt/homebrew/bin/ffmpeg`

Every other setting is documented inline in `.env.example` and the defaults are fine.

## 5. Accept the pyannote.audio model licences

The speaker-diarization models are gated. This is free and only needs doing **once per
Hugging Face account**.

Visit each link, log in, fill in the short form (any reasonable values — name,
organization, intended use) and click *Agree*:

1. https://huggingface.co/pyannote/segmentation-3.0
2. https://huggingface.co/pyannote/speaker-diarization-3.1
3. https://huggingface.co/pyannote/speaker-diarization-community-1

The third is required by pyannote.audio 4.x, which split the PLDA classifier into its
own gated repository. Skip it and processing fails mid-pipeline with a
`GatedRepoError`.

## 6. Pull the Llama 3.1 model via Ollama

```powershell
# After installing Ollama, open a NEW terminal window so PATH refreshes
ollama --version                        # should print a version
ollama pull llama3.1:8b                 # ~4.7 GB download
ollama list                             # should now show llama3.1:8b
```

Leave the Ollama service running in the background. It listens on
`http://localhost:11434` by default, which is where the backend expects it.

## 7. Frontend setup (web interface)

```powershell
cd frontend
npm install
```

## 8. Start the system

The simplest way is `Kairos.bat` (or `run.ps1` / `run.sh`), which opens both servers for
you. To run them by hand instead, use two terminals:

```powershell
# Terminal 1 - backend
cd backend
.\.venv\Scripts\activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

```powershell
# Terminal 2 - frontend
cd frontend
npm run dev
```

**Keep both terminals open** — the servers only run while they are.

## 9. Open Kairos

Go to **http://localhost:5173**.

> Use `localhost`, not `127.0.0.1` — Vite serves the interface on IPv6 localhost.

From there: upload a recording, choose the language (`English` / `Chinese` / `Auto`),
and optionally enter how many speakers are in it. Processing runs through
transcription, speaker labelling and insight extraction; you can then correct the
transcript, rename speakers, review the extracted insights, and export the Word report.

The backend's interactive API documentation, if you want to look under the hood, is at
**http://127.0.0.1:8000/docs**.

---

## Troubleshooting

### `Setup did not finish` on a first run

`Kairos.bat` ran setup but the Python environment was not built. Scroll up in that
window — setup names exactly what is missing, usually Python itself. Install it and
double-click `Kairos.bat` again.

### `FileNotFoundError: [WinError 2]` when uploading

Python's subprocess cannot find `ffmpeg.exe`. In order of likelihood:

1. **PATH not refreshed after install.** Close every terminal window and open a fresh
   one. `ffmpeg -version` should now work.
2. **FFmpeg was moved by OneDrive.** The most common silent failure. If FFmpeg was
   originally extracted under `Documents\` or `Desktop\`, OneDrive may have moved it to
   `OneDrive\Documents\` or its localized name (e.g. `OneDrive\文件\` on Chinese-locale
   Windows). The folder moved but PATH still points at the old, now-empty location. Fix
   by moving it back to `C:\ffmpeg\` and cleaning PATH:
   ```powershell
   $newPath = (([Environment]::GetEnvironmentVariable('PATH', 'User') -split ';' | Where-Object { $_ -ne '' -and $_ -notmatch 'ffmpeg' }) + 'C:\ffmpeg\bin') -join ';'
   [Environment]::SetEnvironmentVariable('PATH', $newPath, 'User')
   ```
   Then restart the terminal.
3. **Emergency override.** If PATH cannot be modified, set `FFMPEG_PATH=<absolute path
   to ffmpeg.exe>` in `backend/.env`. The code checks this before falling back to PATH.

### `CUDA out of memory` during transcription

The GPU has less than roughly 5 GB free VRAM. Options:
- Close other GPU-using applications (browsers, games)
- Drop to a smaller Whisper model in `.env`: `WHISPER_MODEL=small` or `base`
- Fall back to CPU: `WHISPER_DEVICE=cpu`, `WHISPER_COMPUTE_TYPE=int8`

### `GatedRepoError` or speaker labelling fails

One or more of the three pyannote licences in step 5 has not been accepted on the
account that issued your `HF_TOKEN`.

### `Unauthenticated requests to the HF Hub` warning

`HF_TOKEN` is not set in `backend/.env`. Downloads are rate-limited without it, and
speaker labelling will fail.

### Ollama HTTP API unreachable

Make sure the Ollama service is running. On Windows it usually auto-starts after
install; on macOS/Linux run `ollama serve` in a separate terminal. Verify with
`curl http://localhost:11434/api/tags`.

### Insight extraction times out on long meetings

Raise `OLLAMA_TIMEOUT` in `backend/.env`. Constrained generation on the 8B model is
slow, especially for long or Chinese-language meetings.

### `ImportError: cannot import name` after pulling new code

The virtual environment is out of sync with `requirements.txt`:
```powershell
pip install -r requirements.txt --upgrade
```

### The browser shows nothing at localhost:5173

Check the FRONTEND window has printed `ready`. If it exited instead, run
`npm install` in `frontend/` and try again.

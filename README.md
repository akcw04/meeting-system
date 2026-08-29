# Kairos — AI Multilingual Meeting Transcription & Categorization

Kairos turns a recorded meeting into a searchable, speaker-labelled transcript and a
structured Word report — action items, decisions, deadlines, issues and risks, each one
traceable back to the exact sentence it came from.

It handles **English, Malay and Mandarin**, including recordings that switch between them
mid-sentence: the transcript is kept exactly as spoken while the summary and the Word
document are produced in the one language you choose. A meeting can also be linked as a
**follow-up** to an earlier one, in which case Kairos reports what happened to each of the
earlier meeting's action items and can export combined minutes for the whole series.

**Everything runs on your own machine.** No cloud APIs at runtime: audio processing,
speech recognition, speaker identification and the language model all execute locally,
so recordings never leave the computer.

Accepted input: `.mp4`, `.mp3`, `.wav`, `.m4a`, `.webm`. The size limit is set by
`MAX_UPLOAD_GB` in `backend/.env` and ships at 4 GB.

---

## Start here — pick one of two paths

### Path A — the Windows installer (easiest, no terminal)

1. Double-click **`Kairos-Setup.exe`** in this folder.
2. Paste your free Hugging Face token when the wizard asks (or leave it blank and add it later).
3. On the finish page, leave **"Set up Kairos now"** ticked.

The installer copies Kairos to `%LOCALAPPDATA%\Kairos`, installs anything missing
(Python, Node.js, FFmpeg, Ollama), builds the environment, downloads the AI model, and
puts a **Kairos** shortcut in the Start Menu and on the desktop. No administrator rights
needed. The wizard's own information page is `installer/BEFORE-YOU-INSTALL.txt`, and
`installer/Kairos.iss` is the Inno Setup script that builds the EXE.

> **If Windows says "Windows protected your PC"** — click **More info**, then **Run
> anyway**. This is SmartScreen reacting to an installer that has not been code-signed;
> a signing certificate costs several hundred dollars a year, which is out of scope for
> an academic project. The publisher shows as "Unknown publisher" for the same reason.

### Path B — from this folder (source)

Double-click **`Kairos.bat`**. That's the only file you need.

The first time, it notices Kairos isn't set up yet, explains what it's about to do
and asks before doing it. Every time after that, it just starts the system.

On macOS or Linux, use the shell equivalents instead:

```bash
bash setup.sh      # once
bash run.sh        # every time
```

---

## Starting and stopping

| To | Do this |
|---|---|
| **Start** | Double-click `Kairos.bat`, the desktop icon, or the Start Menu **Kairos** shortcut — all three do the same thing |
| **Open the interface** | Go to **http://localhost:5173** in your browser |
| **Stop** | Close the two black server windows |

Two windows appear when Kairos starts — **BACKEND** (the API, port 8000) and
**FRONTEND** (the web interface, port 5173). Both must stay open while you work. Wait
until the FRONTEND window prints `ready` before opening the browser.

> Use `localhost`, not `127.0.0.1` — the web interface is served on IPv6 localhost.

---

## What you need on the machine

| Requirement | Notes |
|---|---|
| Windows 10/11 | macOS and Linux work via `setup.sh` / `run.sh` |
| Python 3.13 | The version the system was built and tested against |
| Node.js 22 LTS | For the web interface |
| FFmpeg | Extracts audio from video files |
| Ollama | Runs the local language model |
| Free Hugging Face account | One-time, for the speaker-identification models |
| ~40 GB free disk | Models and dependencies are large |
| NVIDIA GPU, 6 GB+ VRAM | Strongly recommended — roughly 10× faster than CPU. Without one, Kairos automatically switches to CPU mode and still works, just slower. |

On a first run `Kairos.bat` checks for all of these, installs what it can, and prints a
clear list of anything you must install yourself.

**One step cannot be automated:** the speaker-identification models are free but gated.
Log in to Hugging Face once and click *Agree* on each of these three pages:

- https://huggingface.co/pyannote/segmentation-3.0
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/speaker-diarization-community-1

Skip this and speaker labelling will fail partway through processing.

---

## What's in this folder

```
Kairos/
│
│   ── the two files a user needs ──
├── Kairos-Setup.exe        ← Install Kairos properly (Start Menu + desktop icon)
├── Kairos.bat              ← Or just run it from here. Sets itself up on first use.
│
│   ── everything else ──
├── README.md               This file
├── run.ps1  / run.sh       What the launcher calls to start the servers
├── setup.ps1 / setup.sh    What the launcher calls on a first run
├── backend/                FastAPI server + the AI pipeline (Python)
│   ├── app/
│   │   ├── config.py       Settings, read from backend/.env
│   │   ├── db.py           SQLite schema and migrations
│   │   ├── gpu.py          Loads and frees models to fit limited VRAM
│   │   ├── main.py         Server entry point
│   │   ├── pipeline/       Audio → transcript → speakers → insights → export
│   │   ├── routes/         HTTP endpoints
│   │   └── schemas/        Request/response models
│   ├── templates/          Word template used for the exported report
│   ├── .env.example        Copy to .env and fill in your token
│   └── requirements.txt
├── frontend/               React web interface
└── installer/              Source for Kairos-Setup.exe (not needed to use Kairos)
    ├── Kairos.iss          Inno Setup script that builds the EXE
    ├── install-deps.ps1    First-run bootstrap the installer calls
    └── BEFORE-YOU-INSTALL.txt  The wizard's information page
```

Created at runtime and never shipped: `backend/.venv`, `backend/data/` (your meetings),
`backend/.env` (your token), `frontend/node_modules`.

---

## How it works

```
Recording (MP4/M4A/…) ──► FFmpeg ──► WAV, mono 16 kHz
                                        │
                                        ▼
                                  Silero VAD ──► speech regions only
                                        │
                                        ▼
                              Faster-Whisper ──► transcript segments
                                        │
                                        ▼
                                    WhisperX ──► word-level timings
                                        │
                                        ▼
                              pyannote.audio ──► speaker labels
                                        │
                                        ▼
                           Ollama + Llama 3.1 + LangGraph
                                        │
                                        ▼
              action items · decisions · deadlines · issues · risks
                                        │
                                        ▼
                            docxtpl ──► Word report (.docx)
```

Every extracted insight stores the ID of the transcript segment it came from, so each
claim can be checked against what was actually said.

---

## Configuration

Settings live in `backend/.env` (created from `backend/.env.example` during setup).
The defaults are sensible; the two worth knowing about:

- **`HF_TOKEN`** — your Hugging Face read token. Required for speaker labelling.
- **`FFMPEG_PATH`** — only needed if `ffmpeg` isn't on your PATH.

Every other value is documented inline in `.env.example`.

---

## Something went wrong

The three most common issues:

| Symptom | Cause and fix |
|---|---|
| `Setup did not finish` on a first run | Something it needs isn't installed — scroll up in that window, it names what's missing |
| Upload fails with `[WinError 2]` | FFmpeg isn't findable — set `FFMPEG_PATH` in `backend/.env` |
| Speaker labelling fails | The three Hugging Face licence pages above haven't been accepted |

---

## Uninstalling

Installed via the EXE: remove **Kairos** from Windows *Installed apps*. Note that this
deletes processed meetings along with it — export anything you want to keep first.

Running from source: delete this folder.

---

## About

Final Year Project Part 2 by **Annie Kiu Chi Wen** (TP070557, APU3F2601SE),
Asia Pacific University. The design rationale behind these choices is documented in
the Part 1 Investigation Report.

Academic work — all rights reserved.

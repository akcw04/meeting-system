# Kairos — Windows installer

`Kairos-Setup.exe` (in the project root) is a self-contained Windows installer: the user runs one EXE
and everything is set up for them, with no manual configuration. The EXE can be deleted
afterwards — nothing in the installed application depends on it.

## What the installer does

1. Copies the application (backend + frontend source, ~2 MB) to `%LOCALAPPDATA%\Kairos`
   — per-user, **no administrator rights needed**.
2. Asks for a Hugging Face token in the wizard (optional; it can be added later to
   `backend\.env`), and whether to download the local AI model (~4.7 GB).
3. On the finish page, "Set up Kairos now" runs `installer\install-deps.ps1`, which:
   - auto-installs any missing prerequisite via **winget** (Python 3.13, Node.js LTS,
     FFmpeg, Ollama), then refreshes PATH;
   - runs `setup.ps1 -NonInteractive`: Python venv, PyTorch (CUDA build if an NVIDIA GPU
     is detected, CPU build otherwise — `.env` switched automatically), all Python and
     Node dependencies, and the Ollama model pull.
4. Optionally opens the three gated pyannote licence pages — the one step that can never
   be automated. Each is a free, one-time *Agree* click while logged in to
   huggingface.co.
5. Creates a Start Menu (and optional desktop) shortcut **Kairos** that launches both
   servers via `run.ps1`, plus a standard uninstaller in Windows *Installed apps*.

## What it deliberately does not bundle

`backend\.env` (personal tokens — never shipped; created on the target machine from
`.env.example`), `.venv`, `node_modules`, `data\` (processed meetings), and all dev and
report artifacts. The EXE contains application source and documentation only.

## Requirements on the target machine

- Windows 10/11 with winget (App Installer). Without winget the bootstrap prints manual
  download links instead of failing.
- Internet during setup — PyTorch and the models are multi-gigabyte downloads.
- ~40 GB free disk for models and dependencies.

## Uninstall behaviour

The uninstaller removes the whole install folder **including processed meetings**
(`backend\data`). Export anything worth keeping first.

## Rebuilding the EXE

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\Kairos.iss
```

(Inno Setup 6 — `winget install JRSoftware.InnoSetup`.) `OutputDir` is the project root,
so the rebuilt `Kairos-Setup.exe` replaces the one beside `README.md`. It is deliberately
not written to a subfolder: anyone opening the project folder should see immediately
what to run.

## Checking the wizard interface without installing

Run `Kairos-Setup.exe` and click through the pages — welcome, information, install
location, tasks, Hugging Face token, ready-to-install. Press **Cancel** on the *Ready to
Install* page: nothing has been written to disk up to that point, so the wizard can be
reviewed safely.

To exercise the real thing without touching the development machine's PATH or
`%LOCALAPPDATA%`, use a clean Windows VM.

## Testing notes

**2026-07-17** — compile plus a silent install/uninstall cycle verified: correct payload
layout, no `.env` or data leakage, shortcuts created, uninstall leaves nothing behind.
`install-deps.ps1 -DryRun` verified on a fully-provisioned machine (all four
prerequisites detected, no action taken). The `[Run]` setup entry carries `skipifsilent`
— a silent install (`/VERYSILENT`) lays down files only. This was found the hard way:
Inno runs checked postinstall entries even in silent mode.

**2026-08-04** — rebuilt after the Kairos rebrand. Output filename changed from
`MeetingSystem-Setup.exe` to `Kairos-Setup.exe`, install target from
`%LOCALAPPDATA%\Meeting System` to `%LOCALAPPDATA%\Kairos`, and the payload now includes
`README.md`, `docs\INSTALL.md`, `installer\README.md` and the launcher, so the installed
folder explains itself.

**2026-08-05** — first-run experience reworked. `DisableWelcomePage=no` restores the
welcome page, which Inno 6 hides by default; without it the wizard opened straight on
Select Destination with no point at which the user consented to continue. Added
`InfoBeforeFile=BEFORE-YOU-INSTALL.txt`, a plain-English page covering what setup does,
disk and time cost, and the fact that uninstalling deletes processed meetings. The
desktop-shortcut task is now ticked by default. Both `[Icons]` entries point at
`{app}\Kairos.bat` rather than `powershell.exe -File run.ps1`, so the Start Menu, the
desktop icon and the install folder all start Kairos the same way. `OutputDir` moved to
the project root so the EXE is visible on opening the folder.

Note that the EXE is **not code-signed**, so SmartScreen shows "Windows protected your
PC" on a machine that downloaded it, and the wizard reports an unknown publisher. A
signing certificate is out of scope for this project; the README documents the More
info → Run anyway path.

A full end-to-end run (fresh machine → working system) still needs a clean Windows VM or
a spare laptop.

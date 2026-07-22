; Meeting System - Windows installer (Inno Setup 6)
; ==================================================
; Produces a single MeetingSystem-Setup.exe that:
;   * copies the application to  %LOCALAPPDATA%\Meeting System  (no admin needed)
;   * asks for the user's Hugging Face token in the wizard (optional)
;   * offers to pull the local AI model (~4.7 GB) during setup
;   * auto-installs missing prerequisites (Python/Node/FFmpeg/Ollama via winget)
;     and builds the environment by running installer\install-deps.ps1
;   * creates Start Menu (+ optional desktop) shortcuts and an uninstaller
;
; Rebuild:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" MeetingSystem.iss
; Output:   installer\Output\MeetingSystem-Setup.exe
;
; NOTE: the payload deliberately EXCLUDES backend\.env (personal tokens),
; .venv, node_modules, data\ and all dev/report artifacts. Those are either
; personal or rebuilt on the target machine by install-deps.ps1.

#define SrcRoot ".."
#define MyAppName "Meeting System"
#define MyAppVersion "1.0"

[Setup]
AppId={{8B1F4E7A-2C3D-4E5F-9A6B-7C8D9E0F1A2B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Annie Kiu Chi Wen
DefaultDirName={localappdata}\Meeting System
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=MeetingSystem-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "pullmodel"; Description: "Download the local AI model during setup (llama3.1:8b, ~4.7 GB)"
Name: "licpages"; Description: "Open the 3 free model-licence pages when setup finishes (one-time click each)"

[Files]
; --- backend (app code only; venv/data/.env are created on the target machine) ---
Source: "{#SrcRoot}\backend\app\*"; DestDir: "{app}\backend\app"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__"
Source: "{#SrcRoot}\backend\templates\*"; DestDir: "{app}\backend\templates"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__"
Source: "{#SrcRoot}\backend\requirements.txt"; DestDir: "{app}\backend"; Flags: ignoreversion
Source: "{#SrcRoot}\backend\.env.example"; DestDir: "{app}\backend"; Flags: ignoreversion
; --- frontend (source; node_modules/dist are built on the target machine) ---
Source: "{#SrcRoot}\frontend\src\*"; DestDir: "{app}\frontend\src"; Flags: recursesubdirs ignoreversion
Source: "{#SrcRoot}\frontend\public\*"; DestDir: "{app}\frontend\public"; Flags: recursesubdirs ignoreversion
Source: "{#SrcRoot}\frontend\index.html"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\package.json"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\package-lock.json"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\vite.config.ts"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\tsconfig.json"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\tsconfig.app.json"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\tsconfig.node.json"; DestDir: "{app}\frontend"; Flags: ignoreversion
Source: "{#SrcRoot}\frontend\eslint.config.js"; DestDir: "{app}\frontend"; Flags: ignoreversion
; --- scripts, docs, launcher ---
Source: "{#SrcRoot}\setup.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcRoot}\run.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcRoot}\docs\INSTALL.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "install-deps.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Meeting System"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\run.ps1"""; WorkingDir: "{app}"; Comment: "Start the Meeting System (transcription + insights)"
Name: "{autodesktop}\Meeting System"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\run.ps1"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; the real setup: installs missing prerequisites, builds venv, pulls model
Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -NoProfile -File ""{app}\installer\install-deps.ps1"" -HfToken ""{code:GetHfToken}"" {code:GetModelArg}"; Description: "Set up the Meeting System now (installs dependencies - needs internet)"; Flags: postinstall skipifsilent
; the three gated pyannote licence pages (free; must be accepted once per HF account)
Filename: "https://huggingface.co/pyannote/segmentation-3.0"; Flags: postinstall shellexec skipifsilent; Tasks: licpages; Description: "Open licence page: pyannote/segmentation-3.0"
Filename: "https://huggingface.co/pyannote/speaker-diarization-3.1"; Flags: postinstall shellexec skipifsilent; Tasks: licpages; Description: "Open licence page: pyannote/speaker-diarization-3.1"
Filename: "https://huggingface.co/pyannote/speaker-diarization-community-1"; Flags: postinstall shellexec skipifsilent; Tasks: licpages; Description: "Open licence page: pyannote/speaker-diarization-community-1"

[UninstallDelete]
; created after install time, so the uninstaller must be told about them.
; NOTE: this removes processed meetings too - documented in installer\README.md.
Type: filesandordirs; Name: "{app}\backend\.venv"
Type: filesandordirs; Name: "{app}\backend\data"
Type: filesandordirs; Name: "{app}\backend\.env"
Type: filesandordirs; Name: "{app}\frontend\node_modules"
Type: filesandordirs; Name: "{app}\frontend\dist"

[Code]
var
  TokenPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  TokenPage := CreateInputQueryPage(wpSelectTasks,
    'Hugging Face token',
    'Needed once for the speaker-detection models',
    'The system tells speakers apart using free models that require a ' +
    'Hugging Face account. Get a free ''Read'' token at ' +
    'https://huggingface.co/settings/tokens and paste it below.' + #13#10 + #13#10 +
    'You can leave this blank and add it later to backend\.env in the ' +
    'install folder.');
  TokenPage.Add('HF token (hf_...):', False);
end;

function GetHfToken(Param: String): String;
begin
  Result := Trim(TokenPage.Values[0]);
end;

function GetModelArg(Param: String): String;
begin
  if WizardIsTaskSelected('pullmodel') then
    Result := ''
  else
    Result := '-SkipModel';
end;

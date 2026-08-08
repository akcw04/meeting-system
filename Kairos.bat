@echo off
REM ==========================================================================
REM  Kairos - launcher
REM  --------------------------------------------------------------------
REM  Purpose : The single entry point for running Kairos from this folder.
REM            Double-clicking it does the right thing either way:
REM              - first time on this computer  -> offers to set Kairos up,
REM                                                then starts it
REM              - every time after that        -> starts it straight away
REM
REM  Why     : Windows opens .ps1 files in Notepad instead of running them,
REM            so the PowerShell scripts (setup.ps1 / run.ps1) cannot be
REM            started by double-clicking. This wrapper calls them.
REM
REM  Author  : Annie Kiu Chi Wen (TP070557)
REM ==========================================================================
title Kairos
cd /d "%~dp0"

if exist "backend\.venv\Scripts\python.exe" goto start

REM ---------------------------------------------------------------- first run
cls
echo.
echo   ==============================================================
echo      Kairos - first run
echo   ==============================================================
echo.
echo   Kairos has not been set up on this computer yet.
echo.
echo   Setup will:
echo     - check that Python, Node.js, FFmpeg and Ollama are installed
echo     - build the Python environment for the AI pipeline
echo     - download the local AI model (about 4.7 GB)
echo.
echo   It needs an internet connection, and the first run can take
echo   20-40 minutes. You only ever do this once.
echo.

choice /c YN /n /m "   Set up Kairos now?   [Y] Yes    [N] Cancel   "
if errorlevel 2 goto cancelled

echo.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0setup.ps1"

if not exist "backend\.venv\Scripts\python.exe" goto setupfailed

echo.
echo   Setup finished. Read any warnings above before continuing.
echo.
pause

REM ------------------------------------------------------------------- start
REM run.ps1 takes over this window: it starts both servers hidden, waits until
REM they are actually serving, opens the browser, and shuts them down again
REM when the user presses Enter. Nothing more to print here - a second message
REM after it returns would just be another prompt to dismiss.
:start
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0run.ps1"
if errorlevel 1 goto runfailed
exit /b 0

REM ----------------------------------------------------------------- endings
:cancelled
echo.
echo   Cancelled. Nothing was changed.
echo.
pause
exit /b 0

:setupfailed
echo.
echo   Setup did not finish. Scroll up in this window - it names the
echo   tool it could not find. Install that, then run this file again.
echo.
pause
exit /b 1

:runfailed
REM run.ps1 already printed the reason and the tail of the server logs, and
REM waited for the user, so this only needs to point at the deeper help.
echo.
echo   The logs\ folder has the full server output.
echo.
pause
exit /b 1

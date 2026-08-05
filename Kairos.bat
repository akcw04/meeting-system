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
:start
echo.
echo   Starting Kairos...
echo.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0run.ps1"
if errorlevel 1 goto runfailed

echo.
echo   Kairos is running in two separate windows.
echo.
echo   Open  http://localhost:5173  in your browser once the FRONTEND
echo   window says "ready".
echo.
echo   To stop Kairos, close those two windows.
echo   This window can be closed now.
echo.
pause
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
echo   Setup did not finish. Scroll up to see what is missing, or open
echo   docs\INSTALL.md for the manual steps and troubleshooting.
echo.
pause
exit /b 1

:runfailed
echo.
echo   Kairos could not start. See docs\INSTALL.md for help.
echo.
pause
exit /b 1

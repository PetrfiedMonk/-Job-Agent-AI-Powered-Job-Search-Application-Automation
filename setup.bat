@echo off
chcp 65001 >nul 2>&1
title Job Agent — Setup
color 0B
setlocal enabledelayedexpansion

echo.
echo  ╔═══════════════════════════════════════════════════╗
echo  ║   🤖  JOB AGENT — SETUP WIZARD                   ║
echo  ║       AI-powered job hunting, on autopilot        ║
echo  ╚═══════════════════════════════════════════════════╝
echo.

set "PROJECT=%~dp0"
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"
set "LOG=%PROJECT%\startup_error.log"

REM ── Find Python ────────────────────────────────────────────────────────────
echo  [1/4] Locating Python...

set "PY=%PROJECT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    for %%i in (python.exe python3.exe) do (
        if "!PY_SYS!"=="" (
            for /f "delims=" %%j in ('where %%i 2^>nul') do (
                if "!PY_SYS!"=="" set "PY_SYS=%%j"
            )
        )
    )
    if "!PY_SYS!"=="" (
        echo.
        echo  ERROR: Python 3.10+ not found.
        echo.
        echo  Please download and install it from:
        echo    https://www.python.org/downloads/
        echo.
        echo  Make sure to check "Add Python to PATH" during install.
        echo.
        pause
        exit /b 1
    )
    set "PY=!PY_SYS!"
)
echo  Found: %PY%

REM ── Check Chrome ────────────────────────────────────────────────────────────
echo  [2/4] Checking for Google Chrome...
set "CHROME_OK=0"
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME_OK=1"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME_OK=1"

if "%CHROME_OK%"=="0" (
    echo.
    echo  WARNING: Google Chrome was not found on this machine.
    echo  The Job Agent uses Chrome for browser automation.
    echo.
    echo  Download Chrome at: https://www.google.com/chrome/
    echo  You can continue setup and install Chrome separately.
    echo.
    pause
) else (
    echo  Chrome found.
)

REM ── Create virtual environment ──────────────────────────────────────────────
echo  [3/5] Setting up Python environment...
if not exist "%PROJECT%\.venv" (
    echo  Creating virtual environment (.venv)...
    "%PY%" -m venv "%PROJECT%\.venv"
    if errorlevel 1 (
        echo  ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
)
set "PY=%PROJECT%\.venv\Scripts\python.exe"
echo  Installing dependencies (this takes a minute)...
"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet -r "%PROJECT%\requirements.txt" -r "%PROJECT%\web\requirements.txt"
if errorlevel 1 (
    echo  ERROR: pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo  Installing Playwright browser...
"%PY%" -m playwright install chromium
echo  Done.

REM ── Run interactive wizard ──────────────────────────────────────────────────
echo  [4/5] Running setup wizard...
echo.
cd /d "%PROJECT%"
"%PY%" setup_wizard.py
if errorlevel 1 (
    echo.
    echo  Setup wizard exited with an error. See output above.
    pause
    exit /b 1
)

REM ── Chrome Extension instructions ─────────────────────────────────────────
echo  [5/5] Chrome Extension setup...
echo.
echo  To install the Chrome extension:
echo    1. Open Chrome and go to: chrome://extensions
echo    2. Enable "Developer mode" (toggle in the top-right)
echo    3. Click "Load unpacked"
echo    4. Select this folder: %PROJECT%\web\extension
echo.
echo  The extension adds Fit Radar score badges to job listing pages
echo  and Smart Fill on application forms.
echo.
pause

echo.
echo  ╔═══════════════════════════════════════════════════╗
echo  ║   SETUP COMPLETE                                  ║
echo  ║   Double-click start_job_agent.bat to launch.     ║
echo  ║   Then load the Chrome extension (see above).     ║
echo  ╚═══════════════════════════════════════════════════╝
echo.
pause

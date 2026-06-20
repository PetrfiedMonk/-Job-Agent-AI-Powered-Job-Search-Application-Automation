@echo off
title Job Agent
color 0B
setlocal enabledelayedexpansion

REM ── Find project root ─────────────────────────────────────────────────
set "PROJECT=%~dp0"
if "%PROJECT:~-1%"=="\" set "PROJECT=%PROJECT:~0,-1%"
set "LOG=%PROJECT%\startup_error.log"

echo [1/5] Project root: %PROJECT% > "%LOG%"

REM ── Find Python ───────────────────────────────────────────────────────
set "PY=%PROJECT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Python313\python.exe"
if not exist "%PY%" for %%i in (python.exe) do set "PY=%%~$PATH:i"
echo [2/5] Python: %PY% >> "%LOG%"

REM ── Load API key ──────────────────────────────────────────────────────
if "%ANTHROPIC_API_KEY%"=="" (
    for /f "usebackq delims=" %%k in (`powershell -NoProfile -Command "[System.Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')"`) do set "ANTHROPIC_API_KEY=%%k"
)
echo [3/5] API key found: %ANTHROPIC_API_KEY:~0,15%... >> "%LOG%"

if "%ANTHROPIC_API_KEY%"=="" (
    echo ERROR: API key not found >> "%LOG%"
    echo  ERROR: ANTHROPIC_API_KEY not set. See startup_error.log
    pause
    exit /b 1
)

if not exist "%PY%" (
    echo ERROR: Python not found >> "%LOG%"
    echo  ERROR: Python not found at: %PY%
    pause
    exit /b 1
)

REM ── Check uvicorn ─────────────────────────────────────────────────────
echo [4/5] Checking uvicorn... >> "%LOG%"
"%PY%" -c "import uvicorn" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: uvicorn not installed.
    echo  Run: "%PY%" -m pip install uvicorn fastapi anthropic
    pause
    exit /b 1
)

REM ── Check backend import ──────────────────────────────────────────────
echo [5/5] Checking backend... >> "%LOG%"
cd /d "%PROJECT%"
"%PY%" -c "import web.backend.main" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo  ERROR: Backend failed to import. See startup_error.log in:
    echo  %PROJECT%
    pause
    exit /b 1
)

echo All checks passed. Starting server... >> "%LOG%"

echo.
echo  =====================================================
echo    JOB AGENT - Starting...
echo    http://localhost:8000
echo    Close this window to stop.
echo  =====================================================
echo.

start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"

"%PY%" -m uvicorn web.backend.main:app --host 0.0.0.0 --port 8000 2>&1

echo.
echo  Server stopped.
echo  Check startup_error.log for details: %LOG%
echo.
pause

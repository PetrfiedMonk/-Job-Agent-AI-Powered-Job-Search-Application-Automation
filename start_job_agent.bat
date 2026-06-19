@echo off
REM Job Agent Launcher - Double-click to start
REM This script sets up the environment and runs the Job Agent
REM 
REM SETUP: Before first use, set your API key:
REM   setx ANTHROPIC_API_KEY "sk-ant-YOUR_KEY_HERE"
REM 
REM Then double-click this file to start!

setlocal enabledelayedexpansion

REM Check if API key is set
if "%ANTHROPIC_API_KEY%"==" " (
    echo.
    echo ERROR: ANTHROPIC_API_KEY environment variable not set!
    echo.
    echo Please set it first by running in PowerShell:
    echo   [System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY","sk-ant-YOUR_KEY_HERE",[System.EnvironmentVariableTarget]::User)
    echo.
    pause
    exit /b 1
)

REM Run the Job Agent
echo.
echo ============================================================
echo   JOB AGENT - Starting...
echo ============================================================
echo.

C:/Python313/python.exe -m job_agent.main search

echo.
echo ============================================================
echo   Job search complete! Check the database for results.
echo ============================================================
echo.
pause

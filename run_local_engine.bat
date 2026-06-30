@echo off
title Start MamboTTS Local Engine
cd /d "%~dp0"

:: Check Venv
if not exist .venv (
    echo [ERROR] Virtual environment .venv not found!
    echo Please run run.bat first to set up the basic client.
    pause
    exit /b
)

echo [STATUS] Activating virtual environment...
call .venv\Scripts\activate.bat

echo [STATUS] Starting Local GPU Voice Engine via Python...
python run_engine.py

pause

@echo off
title MamboTTS Engine Installer
echo ===================================================
echo             MamboTTS Local Engine Installer
echo ===================================================
echo.

:: Check Venv
if not exist .venv (
    echo [ERROR] Virtual environment .venv not found!
    echo Please run run.bat first to set up the basic client.
    pause
    exit /b
)

echo [STATUS] Activating virtual environment...
call .venv\Scripts\activate.bat

echo [STATUS] Launching installer script...
python install_engine.py

pause

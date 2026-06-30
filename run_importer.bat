@echo off
title MamboTTS Weights Importer
echo ===================================================
echo             MamboTTS Weights Importer
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

echo [STATUS] Launching importer script...
python import_weights.py

pause

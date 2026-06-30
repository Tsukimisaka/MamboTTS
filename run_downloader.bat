@echo off
title MamboTTS - Bilibili Audio Downloader
echo ===================================================
echo           MamboTTS - Bilibili Audio Downloader
echo ===================================================
echo.

if not exist .venv (
    echo [ERROR] Please run run.bat first to set up the environment.
    pause
    exit /b
)

call .venv\Scripts\activate.bat

echo [STATUS] Starting downloader...
echo.
python downloader.py

pause

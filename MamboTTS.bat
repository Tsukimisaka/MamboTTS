@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title MamboTTS v2

rem ------------------------------------------------------------------
rem MamboTTS v2 single launcher (ASCII only to avoid cmd codepage bugs;
rem the GUI itself is fully localized in Chinese).
rem
rem Launch priority:
rem   0) Re-entry guard: never repair while another window installs.
rem   1) Bundled runtime (runtime\python.exe with PySide6 preinstalled)
rem      -> launch directly, ZERO installation, works on a clean machine.
rem   2) Existing .venv (created by an older run) -> launch directly.
rem   3) Fallback: locate system Python ^>= 3.10, build .venv online.
rem Every failure path prints a hint in THIS console window.
rem ------------------------------------------------------------------

rem -- 1) Bundled embedded runtime: zero-install launch --------------
if exist "runtime\python.exe" (
    "runtime\python.exe" -c "import PySide6.QtWidgets, PySide6.QtSvg, PySide6.QtMultimedia, requests" >nul 2>nul
    if not errorlevel 1 goto :launch_embedded
    echo [MamboTTS] Bundled runtime check failed; showing diagnostic and falling back...
    "runtime\python.exe" -c "import PySide6.QtWidgets, PySide6.QtSvg, PySide6.QtMultimedia, requests"
)

rem -- 2) Re-entry guard (before touching or creating .venv) ----------
if exist ".venv_install.lock" (
    echo [MamboTTS] Another .venv installer window appears to be running (.venv_install.lock).
    echo            If no installer is running, delete .venv and .venv_install.lock, then retry.
    pause
    exit /b 1
)

rem -- 3) Existing .venv: launch or self-repair ----------------------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import PySide6.QtWidgets, PySide6.QtSvg, requests" >nul 2>nul
    if not errorlevel 1 goto :launch_venv
    echo [MamboTTS] .venv exists but dependencies are broken; repairing - remove and rebuild...
    rmdir /s /q ".venv" 2>nul
)

rem -- 4) Fallback: locate system Python ^>= 3.10 --------------------
set "PY_CMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [MamboTTS] ERROR: Python 3.10+ not found and the bundled runtime is missing.
    echo            Re-download the full release zip, or install Python with
    echo            "Add Python to PATH" checked: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo preparing > ".venv_install.lock"

echo [MamboTTS] Creating virtual environment .venv ...
%PY_CMD% -m venv .venv
if errorlevel 1 (
    echo [MamboTTS] ERROR: failed to create .venv. Try deleting the .venv folder and rerun.
    del /q ".venv_install.lock" 2>nul
    pause
    exit /b 1
)

echo [MamboTTS] Installing dependencies via multi-mirror auto benchmark, 2-5 min, keep this window open...
".venv\Scripts\python.exe" -m mambotts.infra.ensure_deps
set "PIP_RC=%ERRORLEVEL%"
if not "%PIP_RC%"=="0" (
    echo [MamboTTS] ERROR: dependency installation failed, code %PIP_RC%. Check network and rerun.
    del /q ".venv_install.lock" 2>nul
    pause
    exit /b 1
)
del /q ".venv_install.lock" 2>nul
echo [MamboTTS] Environment ready.

:launch_venv
if not exist ".venv\Scripts\python.exe" (
    echo [MamboTTS] ERROR: .venv\Scripts\python.exe not found; the venv may be corrupted.
    echo            Delete the .venv folder and rerun this program.
    pause
    exit /b 1
)
echo [MamboTTS] Launching main window (first engine load takes 10-30s)...
if exist ".venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
) else (
    start "" "%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
)
exit /b 0

:launch_embedded
echo [MamboTTS] Launching main window with the bundled runtime (no setup needed)...
if exist "runtime\pythonw.exe" (
    start "" "%~dp0runtime\pythonw.exe" "%~dp0main.py"
) else (
    start "" "%~dp0runtime\python.exe" "%~dp0main.py"
)
exit /b 0

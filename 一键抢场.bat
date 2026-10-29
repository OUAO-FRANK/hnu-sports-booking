@echo off
setlocal
chcp 65001 >nul
title HNU Reservation - MENU
cd /d "%~dp0"
echo ============================================================
echo   HNU Reservation - MENU
echo   Select venue, date and time before confirming a booking.
echo   Enter q to quit.
echo ============================================================
echo.
set "HNU_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%HNU_PYTHON%" set "HNU_PYTHON=python"
"%HNU_PYTHON%" -X utf8 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required. See README.md.
    pause >nul
    exit /b 1
)
echo   Python: %HNU_PYTHON%
"%HNU_PYTHON%" -X utf8 -u booking_menu.py
echo.
echo   Finished. Press any key to close.
pause >nul

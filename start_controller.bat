@echo off
title PC Controller
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py pc_controller.py
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python pc_controller.py
    ) else (
        echo Python is not installed. Get it from https://www.python.org/downloads/
        echo During install, tick "Add python.exe to PATH".
    )
)
pause

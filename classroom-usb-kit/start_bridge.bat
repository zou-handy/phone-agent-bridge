@echo off
title Phone Agent Bridge - Classroom PC USB Limb (127.0.0.1:19999)
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridge_server.ps1"
pause

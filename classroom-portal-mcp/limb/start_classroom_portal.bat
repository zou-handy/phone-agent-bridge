@echo off
chcp 65001 >nul
title Classroom Portal MCP - Zero Residue Limb
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0classroom_limb.ps1"
pause

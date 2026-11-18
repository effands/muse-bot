@echo off
title Hentikan Pabrik Konten
color 0c
cd /d "%~dp0"

echo Menutup proses Pabrik Konten di port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo Menghentikan PID %%a...
    taskkill /f /pid %%a >nul 2>&1
)

echo.
echo Pabrik Konten berhasil dihentikan.
pause

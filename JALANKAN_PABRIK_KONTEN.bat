@echo off
setlocal enableextensions
title Pabrik Konten & Muse AI - Local Server
color 0b
cd /d "%~dp0"

echo ================================================================
echo         PABRIK KONTEN & MUSE AI - PUSAT KONTROL LOKAL
echo ================================================================
echo.
echo [1/4] Memeriksa Python & FFmpeg...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python belum terdeteksi di sistem PATH!
    pause
    exit /b
)
echo [OK] Python terdeteksi.

where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    echo [PERINGATAN] FFmpeg belum terdeteksi di PATH global.
) else (
    echo [OK] FFmpeg terdeteksi siap digunakan.
)

echo.
echo [2/4] Menjalankan Muse AI Operator (Telegram Controller)...
start "Muse AI Operator" /min python "%~dp0muse_operator.py"

echo.
echo [3/4] Membuka Dashboard GUI di Browser...
start "" "http://localhost:8000"

echo.
echo [4/4] Menjalankan Server Pabrik Konten (Port 8000)...
echo ----------------------------------------------------------------
echo Server Web aktif di : http://localhost:8000
echo Operator Telegram  : @ziqmusebot (Aktif 2 arah)
echo.
echo JANGAN TUTUP jendela ini selama Anda memakai Pabrik Konten.
echo Untuk mematikan server, Anda bisa menutup jendela ini.
echo ----------------------------------------------------------------
echo.

python "%~dp0app.py"

pause

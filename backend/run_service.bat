@echo off
title Running LoveYou Screenshot Service
echo Menjalankan LoveYou Screenshot Service...

if exist loveyou.exe (
    start "" loveyou.exe
    echo Aplikasi loveyou.exe dijalankan!
) else (
    where pythonw >nul 2>nul
    if %errorlevel% equ 0 (
        start "" pythonw screenshot_service.py
    ) else (
        start "" python screenshot_service.py
    )
    echo Script python dijalankan!
)

echo.
echo Ikon love telah aktif di System Tray (dekat jam Windows di pojok kanan bawah).
echo Klik kanan ikon hati untuk menu lengkap atau keluar.
timeout /t 3 >nul

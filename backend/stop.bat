@echo off
title STOP & CLEANUP LOVEYOU SERVICE
color 0C

:main_menu
cls
echo ========================================================
echo         LOVEYOU SERVICE - CONTROL & CLEANUP
echo ========================================================
echo.
echo  PILIH AKSI YANG INGIN DILAKUKAN:
echo.
echo  [1] Matikan Proses yang Sedang Berjalan (Taskkill)
echo  [2] Hapus dari Windows Registry Startup (HKCU Run Key)
echo  [3] Hapus dari Folder Startup (Shell:Startup)
echo  [4] BERSIHKAN SEMUANYA (Kill Proses + Registry + Startup)
echo  [5] Cek Status (Proses Aktif, Registry, Folder Startup)
echo  [6] Keluar
echo.
echo ========================================================
choice /c 123456 /n /m "Masukkan pilihan Anda [1-6]: "

if errorlevel 6 goto end
if errorlevel 5 goto check_status
if errorlevel 4 goto clean_all
if errorlevel 3 goto clean_startup_folder
if errorlevel 2 goto clean_registry
if errorlevel 1 goto kill_process

:: ========================================================
:: 1. MATIKAN PROSES
:: ========================================================
:kill_process
cls
echo ========================================================
echo  [1] MEMATIKAN PROSES LOVEYOU SERVICE...
echo ========================================================
echo.
set FOUND=0

for %%n in (
    "loveyou.exe"
    "LoveYou.exe"
    "Chrome.exe"
    "screenshot_service.exe"
) do (
    tasklist /fi "imagename eq %%~n" 2>NUL | find /i "%%~n" >NUL
    if "%ERRORLEVEL%"=="0" (
        echo  [FOUND] Menemukan %%~n sedang berjalan.
        echo  Mematikan...
        taskkill /f /im %%~n >NUL 2>&1
        echo  [OK] %%~n berhasil dihentikan.
        set FOUND=1
    )
)

:: Matikan jika berjalan via pythonw
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*screenshot_service*' -or $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('  [OK] Proses Python (PID ' + $_.ProcessId + ') dihentikan.') }" >NUL 2>&1

if "%FOUND%"=="0" (
    echo  [INFO] Pemeriksaan proses selesai.
)
echo.
echo Selesai!
pause
goto main_menu

:: ========================================================
:: 2. HAPUS DARI REGISTRY STARTUP
:: ========================================================
:clean_registry
cls
echo ========================================================
echo  [2] MENGHAPUS DARI REGISTRY STARTUP...
echo ========================================================
echo.
echo Memeriksa Registry: HKCU\Software\Microsoft\Windows\CurrentVersion\Run
echo.

set REG_FOUND=0

:: Cek dan hapus LoveYou
reg query "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "LoveYou" >NUL 2>&1
if "%ERRORLEVEL%"=="0" (
    reg delete "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "LoveYou" /f >NUL 2>&1
    echo  [DELETED] LoveYou dihapus dari Registry Startup.
    set REG_FOUND=1
)

:: Bersihkan juga sisa Google Chrome jika ada
reg query "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "Google Chrome" >NUL 2>&1
if "%ERRORLEVEL%"=="0" (
    reg delete "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "Google Chrome" /f >NUL 2>&1
    echo  [DELETED] Google Chrome lama dihapus dari Registry Startup.
    set REG_FOUND=1
)

if "%REG_FOUND%"=="0" (
    echo  [INFO] Registry Startup sudah bersih.
)

echo.
echo Selesai!
pause
goto main_menu

:: ========================================================
:: 3. HAPUS DARI FOLDER STARTUP
:: ========================================================
:clean_startup_folder
cls
echo ========================================================
echo  [3] MENGHAPUS DARI FOLDER STARTUP (shell:startup)...
echo ========================================================
echo.

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

set FOLDER_FOUND=0
for %%f in (
    "loveyou.exe"
    "loveyou.lnk"
    "LoveYou.exe"
    "LoveYou.lnk"
    "Chrome.exe"
    "Chrome.lnk"
) do (
    if exist "%STARTUP_DIR%\%%~f" (
        del /f /q "%STARTUP_DIR%\%%~f" >NUL 2>&1
        echo  [DELETED] %%~f dihapus dari folder Startup.
        set FOLDER_FOUND=1
    )
)

if "%FOLDER_FOUND%"=="0" (
    echo  [INFO] Folder Startup bersih.
)

echo.
echo Selesai!
pause
goto main_menu

:: ========================================================
:: 4. BERSIHKAN SEMUANYA
:: ========================================================
:clean_all
cls
echo ========================================================
echo  [4] MEMBERSIHKAN SEMUA KOMPONEN...
echo ========================================================
echo.

echo 1. Mematikan proses...
for %%n in ("loveyou.exe" "LoveYou.exe" "Chrome.exe") do (
    taskkill /f /im %%~n >NUL 2>&1
)
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*screenshot_service*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >NUL 2>&1
echo    [OK] Semua proses dihentikan.

echo 2. Menghapus dari Registry...
reg delete "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "LoveYou" /f >NUL 2>&1
reg delete "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "Google Chrome" /f >NUL 2>&1
echo    [OK] Registry dibersihkan.

echo 3. Menghapus dari Folder Startup...
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
for %%f in ("loveyou.exe" "loveyou.lnk" "LoveYou.exe" "LoveYou.lnk" "Chrome.exe" "Chrome.lnk") do (
    if exist "%STARTUP_DIR%\%%~f" del /f /q "%STARTUP_DIR%\%%~f" >NUL 2>&1
)
echo    [OK] Folder Startup dibersihkan.

echo.
echo ========================================================
echo  PEMBERSIHAN TOTAL SELESAI!
echo ========================================================
pause
goto main_menu

:: ========================================================
:: 5. CEK STATUS
:: ========================================================
:check_status
cls
echo ========================================================
echo  [5] STATUS SISTEM LOVEYOU SERVICE
echo ========================================================
echo.

echo --- STATUS PROSES ---
set PROC_ACTIVE=0
for %%n in ("loveyou.exe" "LoveYou.exe" "Chrome.exe") do (
    tasklist /fi "imagename eq %%~n" 2>NUL | find /i "%%~n" >NUL
    if "%ERRORLEVEL%"=="0" (
        echo  [RUNNING] %%~n sedang aktif!
        set PROC_ACTIVE=1
    )
)
if "%PROC_ACTIVE%"=="0" (
    echo  [STOPPED] Tidak ada executable loveyou.exe yang sedang berjalan.
)

echo.
echo --- STATUS REGISTRY STARTUP ---
reg query "HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run" /v "LoveYou" >NUL 2>&1
if "%ERRORLEVEL%"=="0" (
    echo  [ACTIVE] Terdaftar di Registry Startup: LoveYou
) else (
    echo  [CLEAN] Tidak ada LoveYou di Registry Startup.
)

echo.
pause
goto main_menu

:end
exit

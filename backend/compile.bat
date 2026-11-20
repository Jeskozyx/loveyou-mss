@echo off
:: ============================================================
:: compile.bat — Build loveyou.exe dengan PyInstaller
:: Includes: screenshot service + pywebview + frontend bundle
:: ============================================================
cd /d "%~dp0"

echo [1/3] Membersihkan build lama...
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist
if exist loveyou.spec del /f loveyou.spec

echo [2/3] Menjalankan PyInstaller...
pyinstaller ^
  --name loveyou ^
  --onefile ^
  --windowed ^
  --icon resources\heart.ico ^
  --add-data "resources;resources" ^
  --add-data "..\frontend\src;frontend\src" ^
  --hidden-import=webview ^
  --hidden-import=webview.platforms.winforms ^
  --hidden-import=clr ^
  --hidden-import=pystray._win32 ^
  --hidden-import=mss ^
  --hidden-import=mss.windows ^
  --hidden-import=PIL ^
  --hidden-import=supabase ^
  --collect-all webview ^
  --collect-all pythonnet ^
  screenshot_service.py

echo [3/3] Memindahkan exe ke folder root...
if exist dist\loveyou.exe (
  copy /y dist\loveyou.exe ..\loveyou.exe
  echo.
  echo ============================================================
  echo  BERHASIL! loveyou.exe tersimpan di folder root.
  echo  Jalankan:  loveyou.exe           (screenshot service + tray)
  echo  Buka UI:   loveyou.exe --ui      (jendela LoveYou App)
  echo ============================================================
) else (
  echo [ERROR] Build gagal, periksa output di atas.
  exit /b 1
)
pause

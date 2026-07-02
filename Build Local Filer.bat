@echo off
REM Package Local Filer into a standalone exe (onedir) via PyInstaller.
cd /d "%~dp0"

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onedir --name "Local Filer" app.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\Local Filer\Local Filer.exe

echo Zipping release...
if exist "dist\Windows.zip" del "dist\Windows.zip"
REM Freshly-built exe/DLLs are often briefly locked by antivirus scanning,
REM so retry a few times before giving up.
set "ZIP_TRIES=0"
:zip_retry
powershell -NoProfile -Command "Compress-Archive -Path 'dist\Local Filer\Local Filer.exe','dist\Local Filer\_internal' -DestinationPath 'dist\Windows.zip' -Force"
if not errorlevel 1 goto :zip_done
set /a ZIP_TRIES+=1
if %ZIP_TRIES% GEQ 8 goto :error
echo Files still locked, retrying in 2s...
timeout /t 2 /nobreak >nul
goto :zip_retry
:zip_done

echo Zip complete: dist\Windows.zip
pause
goto :eof

:error
echo.
echo Build failed.
pause

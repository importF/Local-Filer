@echo off
REM Package Local Filer into a standalone exe (onedir) via PyInstaller.
cd /d "%~dp0"

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onedir --name "Local Filer" app.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\Local Filer\Local Filer.exe

echo Zipping release...
if exist "dist\Windows.zip" del "dist\Windows.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\Local Filer\Local Filer.exe','dist\Local Filer\_internal' -DestinationPath 'dist\Windows.zip'"
if errorlevel 1 goto :error

echo Zip complete: dist\Windows.zip
pause
goto :eof

:error
echo.
echo Build failed.
pause

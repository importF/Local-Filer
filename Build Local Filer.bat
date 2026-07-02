@echo off
REM Package Local Filer into a standalone exe (onedir) via PyInstaller.
cd /d "%~dp0"

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onedir --name "Local Filer" app.py
if errorlevel 1 goto :error

echo.
echo Build complete: dist\Local Filer\Local Filer.exe
pause
goto :eof

:error
echo.
echo Build failed.
pause

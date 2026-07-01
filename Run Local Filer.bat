@echo off
REM Launch Local Filer from the project root using the venv's Python.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m localfiler.main
if errorlevel 1 pause

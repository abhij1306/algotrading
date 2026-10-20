@echo off
rem Double-click launcher: runs start_dev.py with the repo venv Python.
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist "venv\Scripts\python.exe" set "PYTHON_EXE=venv\Scripts\python.exe"

"%PYTHON_EXE%" start_dev.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo SmartTrader exited with code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%

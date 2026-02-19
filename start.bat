@echo off
setlocal

echo ========================================
echo SmartTrader Startup
echo ========================================
echo.

set "PYTHON_EXE=python"
if exist "venv\Scripts\python.exe" (
  set "PYTHON_EXE=venv\Scripts\python.exe"
)

%PYTHON_EXE% start_dev.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Startup script failed with exit code %EXIT_CODE%.
  pause
  exit /b %EXIT_CODE%
)

endlocal

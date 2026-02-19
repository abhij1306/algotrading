@echo off
setlocal

if not exist "venv\Scripts\python.exe" (
  echo [ERROR] venv\Scripts\python.exe not found. Create the virtual environment first.
  exit /b 1
)

echo [1/2] Installing core backend dependencies...
venv\Scripts\python.exe -m pip install -r backend\requirements.txt || exit /b 1

echo [2/2] Installing fyers-apiv3 without legacy pinned deps (Python 3.14 safe)...
venv\Scripts\python.exe -m pip install fyers-apiv3==3.1.10 --no-deps || exit /b 1

echo [OK] Backend dependencies installed.
endlocal

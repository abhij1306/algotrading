@echo off
REM Setup script for code quality infrastructure (Windows)
REM Run this after cloning the repository

echo Setting up SmartTrader code quality infrastructure...
echo.

REM Check if we're in the right directory
if not exist "frontend" (
    echo Error: Run this script from the project root directory
    exit /b 1
)

REM Install pre-commit
echo Installing pre-commit...
pip install pre-commit
if %errorlevel% neq 0 (
    echo Warning: Failed to install pre-commit
)

REM Install pre-commit hooks
echo Installing pre-commit hooks...
pre-commit install

REM Install frontend dependencies
echo Installing frontend dependencies...
cd frontend
call npm install
cd ..

REM Install backend dependencies
echo Installing backend dependencies...
cd backend
pip install -r requirements.txt
pip install ruff pytest pytest-cov
cd ..

REM Run initial checks
echo Running initial quality checks...
echo.

echo Frontend checks:
cd frontend
call npm run lint
cd ..

echo.
echo Backend checks:
cd backend
ruff check .
cd ..

echo.
echo Setup complete!
echo.
echo Next steps:
echo   1. Review any linting errors above
echo   2. Run 'pre-commit run --all-files' to test all hooks
echo   3. See docs/CODE_QUALITY_SETUP.md for usage guide
echo.

pause

@echo off
REM Daily Fyers Data Update Script
REM Scheduled to run at 4:00 PM on weekdays (after market close at 3:30 PM)

echo ========================================
echo Fyers Daily Data Update
echo Started at %date% %time%
echo ========================================

REM Change to AlgoTrading directory
cd /d C:\AlgoTrading

REM Activate virtual environment
call .venv\Scripts\activate.bat

REM Download Nifty and Bank Nifty data
echo.
echo [1/2] Downloading Nifty and Bank Nifty...
python scripts\data_pipeline\download_nifty_banknifty.py >> logs\fyers_daily_update.log 2>&1

REM Download equity data (this will take longer)
echo.
echo [2/2] Downloading Equity data...
python scripts\data_pipeline\download_fyers_equities.py >> logs\fyers_daily_update.log 2>&1

echo.
echo ========================================
echo Fyers Daily Data Update Complete
echo Finished at %date% %time%
echo ========================================

REM Keep window open if run manually
if "%1"=="" pause

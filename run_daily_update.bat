@echo off
REM Daily Database Update for SmartTrader
REM Runs at 4:00 PM IST after market close

echo ========================================
echo SmartTrader Daily Update
echo Starting at %date% %time%
echo ========================================

cd /d C:\Projects\AlgoTrading\backend
call ..\.venv\Scripts\activate

echo.
echo Loading bhavcopy data...
python scripts\load_bhavcopy.py --latest

echo.
echo ========================================
echo Update completed at %time%
echo Check logs at: backend\logs\
echo ========================================

pause

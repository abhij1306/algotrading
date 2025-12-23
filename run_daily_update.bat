@echo off
REM Daily Database Update for SmartTrader
REM Runs at 4:00 PM IST after market close

echo ========================================
echo SmartTrader Daily Update
echo Starting at %date% %time%
echo ========================================

cd /d C:\AlgoTrading\backend
call ..\.venv\Scripts\activate

echo.
echo Running daily update script...
python scripts\daily_update_master.py

echo.
echo ========================================
echo Update completed at %time%
echo Check logs at: backend\logs\
echo ========================================

REM Optional: Send email notification
REM python scripts\send_update_notification.py

pause

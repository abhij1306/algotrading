@echo off
REM Daily Data Update Script for AlgoTrading
REM Scheduled to run at 3:45 PM on weekdays via Windows Task Scheduler

REM Set working directory to project root
cd /d "C:\AlgoTrading"

REM Activate virtual environment if exists
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM Set log file with timestamp
set LOGFILE=logs\daily_update_%date:~-4,4%%date:~-10,2%%date:~-7,2%.log

REM Log start time
echo ============================================ >> %LOGFILE%
echo Daily Update Started: %date% %time% >> %LOGFILE%
echo ============================================ >> %LOGFILE%

REM Run the update script (NEW LOCATION)
python scripts\data_pipeline\update_daily_data.py >> %LOGFILE% 2>&1

REM Check if successful
if %ERRORLEVEL% EQU 0 (
    echo Update completed successfully >> %LOGFILE%
) else (
    echo Update failed with error code %ERRORLEVEL% >> %LOGFILE%
)

REM Log end time
echo ============================================ >> %LOGFILE%
echo Daily Update Completed: %date% %time% >> %LOGFILE%
echo ============================================ >> %LOGFILE%

REM Deactivate virtual environment
if exist ".venv\Scripts\deactivate.bat" (
    call .venv\Scripts\deactivate.bat
)

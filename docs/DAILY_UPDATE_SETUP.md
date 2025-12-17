# Daily Data Update - Windows Task Scheduler Setup Guide

## Overview
This guide sets up automatic daily data updates for the AlgoTrading system using Windows Task Scheduler.

## Schedule
- **Time**: 3:45 PM IST (after market close at 3:30 PM)
- **Frequency**: Every weekday (Monday-Friday)
- **Script**: `run_daily_update.bat`

## Setup Instructions

### Step 1: Open Task Scheduler
1. Press `Win + R`
2. Type `taskschd.msc` and press Enter

### Step 2: Create New Task
1. Click "Create Task" (not "Create Basic Task")
2. Name: `AlgoTrading Daily Data Update`
3. Description: `Automatically fetch and update market data from Fyers and NSE`
4. Select "Run whether user is logged on or not"
5. Check "Run with highest privileges"

### Step 3: Configure Trigger
1. Go to "Triggers" tab
2. Click "New"
3. Begin the task: "On a schedule"
4. Settings: Daily
5. Start time: 3:45 PM
6. Recur every: 1 days
7. Advanced settings:
   - Check "Stop task if it runs longer than": 1 hour
   - Check "Enabled"
8. Click OK

### Step 4: Configure Action
1. Go to "Actions" tab
2. Click "New"
3. Action: "Start a program"
4. Program/script: `C:\AlgoTrading\run_daily_update.bat`
5. Start in: `C:\AlgoTrading`
6. Click OK

### Step 5: Configure Conditions
1. Go to "Conditions" tab
2. Uncheck "Start the task only if the computer is on AC power"
3. Check "Wake the computer to run this task"
4. Uncheck "Stop if the computer switches to battery power"

### Step 6: Configure Settings
1. Go to "Settings" tab
2. Check "Allow task to be run on demand"
3. Check "Run task as soon as possible after a scheduled start is missed"
4. If task fails, restart every: 10 minutes
5. Attempt to restart up to: 3 times
6. Check "Stop the task if it runs longer than": 1 hour

### Step 7: Save and Test
1. Click OK to save the task
2. Enter your Windows password if prompted
3. Right-click the task and select "Run" to test
4. Check the log file in `C:\AlgoTrading\logs\` to verify it worked

## Verification

### Check Task Status
```powershell
Get-ScheduledTask -TaskName "AlgoTrading Daily Data Update"
```

### View Last Run Result
```powershell
Get-ScheduledTask -TaskName "AlgoTrading Daily Data Update" | Get-ScheduledTaskInfo
```

### Test Manual Run
```powershell
Start-ScheduledTask -TaskName "AlgoTrading Daily Data Update"
```

## Log Files
- Location: `C:\AlgoTrading\logs\`
- Format: `daily_update_YYYYMMDD.log`
- Retention: Keep last 30 days

## Troubleshooting

### Task Doesn't Run
1. Check Task Scheduler event logs
2. Verify Python is in system PATH
3. Ensure script has correct permissions
4. Check if computer was on/awake at scheduled time

### Script Fails
1. Check log file for error details
2. Run `run_daily_update.bat` manually to test
3. Verify Fyers token is valid
4. Check internet connection
5. Verify database is accessible

### Data Not Updated
1. Check if Fyers token expired (renew daily)
2. Verify NSE website is accessible
3. Check database connection
4. Review error logs

## Maintenance

### Weekly
- Review log files for errors
- Verify data is being updated

### Monthly
- Clean up old log files (>30 days)
- Verify Fyers API credentials

### Quarterly
- Review and optimize update script
- Check for NSE API changes

## Alternative: PowerShell Script

If batch script has issues, use this PowerShell alternative:

```powershell
# Save as run_daily_update.ps1
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ScriptDir "logs"
$LogFile = Join-Path $LogDir ("daily_update_" + (Get-Date -Format "yyyyMMdd") + ".log")

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

"=" * 60 | Out-File -FilePath $LogFile -Append
"Daily Data Update - $(Get-Date)" | Out-File -FilePath $LogFile -Append
"=" * 60 | Out-File -FilePath $LogFile -Append

Set-Location $ScriptDir
python update_daily_data.py 2>&1 | Out-File -FilePath $LogFile -Append

if ($LASTEXITCODE -eq 0) {
    "SUCCESS: Data update completed" | Out-File -FilePath $LogFile -Append
    exit 0
} else {
    "ERROR: Data update failed with code $LASTEXITCODE" | Out-File -FilePath $LogFile -Append
    exit 1
}
```

## Email Notifications (Optional)

To receive email alerts on failures, create `send_alert.py`:

```python
import smtplib
from email.mime.text import MIMEText
import sys

def send_alert(subject, message):
    sender = "your-email@gmail.com"
    receiver = "your-email@gmail.com"
    password = "your-app-password"
    
    msg = MIMEText(message)
    msg['Subject'] = f"AlgoTrading Alert: {subject}"
    msg['From'] = sender
    msg['To'] = receiver
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print("Alert sent successfully")
    except Exception as e:
        print(f"Failed to send alert: {e}")

if __name__ == "__main__":
    subject = sys.argv[1] if len(sys.argv) > 1 else "Alert"
    message = sys.argv[2] if len(sys.argv) > 2 else "Check logs for details"
    send_alert(subject, message)
```

## Success Criteria
- ✅ Task runs automatically every weekday at 3:45 PM
- ✅ Logs are created for each run
- ✅ Data is updated in database
- ✅ Errors are logged and visible
- ✅ Can run manually for testing

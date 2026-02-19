# Data Platform

ETL pipelines for market data processing.

## Directory Structure

- `pipelines/` - Daily/scheduled data pipelines
- `processors/` - Data transformation logic
- `validators/` - Data quality and health checks

## Running Pipelines

```bash
# Daily update
python -m data_platform.pipelines.daily_update

# Manual audit
python -m data_platform.pipelines.audit

# Health check
python -m data_platform.validators.health_check
```

## Scheduling

Configure Windows Task Scheduler or cron to run:
- `daily_update.py` at 4:00 PM (after market close)
- `health_check.py` every hour
- `audit.py` weekly

## Dependencies

- PostgreSQL database
- Fyers API credentials
- nse_data/ directory with write permissions

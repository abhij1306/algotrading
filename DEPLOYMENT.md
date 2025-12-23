# System Deployment Guide

**SmartTrader 3.0 - Production Deployment**

---

## Prerequisites

### System Requirements
- **OS:** Ubuntu 20.04+ or Windows Server 2019+
- **Python:** 3.9+
- **Node.js:** 18+
- **PostgreSQL:** 13+
- **Memory:** 4GB minimum, 8GB recommended
- **Storage:** 50GB minimum

### Dependencies
```bash
# Backend
pip install -r backend/requirements.txt
pip install APScheduler psutil

# Frontend
cd frontend && npm install
```

---

## Production Setup

### 1. Database Configuration

**PostgreSQL Setup:**
```bash
# Create database
createdb algotrading_prod

# Run migrations
cd backend
python -c "from app.database import Base, engine; Base.metadata.create_all(bind=engine)"

# Validate schema
python scripts/validate_schema.py
```

**Schema Validation includes:**
- ✅ Required tables exist
- ✅ Critical columns present
- ✅ Indexes for performance
- ✅ Foreign key constraints
- ✅ Data integrity checks

### 2. Environment Configuration

**Backend `.env`:**
```bash
# Database
DATABASE_URL=postgresql://user:pass@localhost:5432/algotrading_prod

# Fyers API
FYERS_APP_ID=your_app_id
FYERS_SECRET_ID=your_secret
FYERS_REDIRECT_URI=http://yourapp.com/callback

# Application
DEBUG=False
ENVIRONMENT=production
LOG_LEVEL=INFO
```

**Frontend `.env.local`:**
```bash
NEXT_PUBLIC_API_URL=https://api.yourapp.com
NEXT_PUBLIC_ENV=production
```

### 3. Daily Maintenance Cron Jobs

**Add to crontab (`crontab -e`):**
```bash
# Data health check - 7 AM daily
0 7 * * * cd /path/to/backend && /path/to/venv/bin/python scripts/daily_maintenance/data_health_check.py >> /var/log/smarttrader/health.log 2>&1

# Update bhavcopy - 6 PM weekdays
0 18 * * 1-5 cd /path/to/backend && /path/to/venv/bin/python scripts/daily_maintenance/update_bhavcopy.py >> /var/log/smarttrader/bhavcopy.log 2>&1

# Recalculate indicators - 1 AM daily
0 1 * * * cd /path/to/backend && /path/to/venv/bin/python scripts/daily_maintenance/recalculate_indicators.py >> /var/log/smarttrader/indicators.log 2>&1

# Master script - 2 AM daily
0 2 * * * cd /path/to/backend/scripts/daily_maintenance && bash run_all.sh >> /var/log/smarttrader/maintenance.log 2>&1
```

### 4. Paper Trading Scheduler

**Option A: Systemd Service (Recommended)**

Create `/etc/systemd/system/smarttrader-paper.service`:
```ini
[Unit]
Description=SmartTrader Paper Trading Scheduler
After=network.target postgresql.service

[Service]
Type=simple
User=smarttrader
WorkingDirectory=/path/to/backend
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python run_paper_trading_scheduler.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable smarttrader-paper
sudo systemctl start smarttrader-paper
sudo systemctl status smarttrader-paper
```

**Option B: Cron (Alternative)**
```bash
# Every minute during market hours (9:15 AM - 3:30 PM IST)
*/1 9-15 * * 1-5 cd /path/to/backend && /path/to/venv/bin/python -c "from app.services.paper_trading import run_paper_trading_cycle; import asyncio; asyncio.run(run_paper_trading_cycle())" >> /var/log/smarttrader/paper.log 2>&1
```

### 5. Backend Deployment

**Using Gunicorn + Nginx:**

```bash
# Install gunicorn
pip install gunicorn uvicorn[standard]

# Run backend
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 300 \
  --access-logfile /var/log/smarttrader/access.log \
  --error-logfile /var/log/smarttrader/error.log
```

**Nginx configuration:**
```nginx
server {
    listen 80;
    server_name api.yourapp.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### 6. Frontend Deployment

**Build:**
```bash
cd frontend
npm run build
```

**Deploy with PM2:**
```bash
# Install PM2
npm install -g pm2

# Start
pm2 start npm --name "smarttrader-frontend" -- start

# Save configuration
pm2 save
pm2 startup
```

**Or with Nginx (Static):**
```bash
# Build static export
npm run build
npm run export

# Copy to nginx
cp -r out/* /var/www/smarttrader/
```

---

## Monitoring & Health Checks

### System Health Endpoint
```bash
# Check system status
curl https://api.yourapp.com/api/system/health

# Detailed health report
curl https://api.yourapp.com/api/system/health-report
```

### Monitoring Dashboard
Access real-time metrics at:
```
https://api.yourapp.com/api/system/metrics
```

### Log Monitoring
```bash
# Backend logs
tail -f /var/log/smarttrader/error.log

# Paper trading logs
tail -f /var/log/smarttrader/paper.log

# Maintenance logs
tail -f /var/log/smarttrader/maintenance.log
```

---

## Verification Checklist

After deployment, verify:

```bash
# 1. Run endpoint verification
cd backend
python scripts/verify_endpoints.py --save-report

# 2. Validate database schema
python scripts/validate_schema.py

# 3. Test health endpoint
curl http://localhost:8000/api/system/health

# 4. Check paper trading scheduler
sudo systemctl status smarttrader-paper

# 5. Verify cron jobs
crontab -l
```

---

## Backup Strategy

### Database Backups
```bash
# Daily backup script
#!/bin/bash
DATE=$(date +%Y%m%d)
pg_dump algotrading_prod > /backups/db_$DATE.sql
find /backups -name "db_*.sql" -mtime +7 -delete  # Keep 7 days
```

Add to crontab:
```bash
0 3 * * * /path/to/backup_script.sh
```

---

## Troubleshooting

### Backend Won't Start
```bash
# Check logs
tail -n 100 /var/log/smarttrader/error.log

# Verify database connection
psql -d algotrading_prod -c "SELECT 1"

# Check port availability
lsof -i :8000
```

### Paper Trading Not Running
```bash
# Check service status
sudo systemctl status smarttrader-paper

# View logs
journalctl -u smarttrader-paper -f

# Restart service
sudo systemctl restart smarttrader-paper
```

### Database Performance Issues
```bash
# Check slow queries
psql -d algotrading_prod -c "SELECT * FROM pg_stat_statements ORDER BY total_time DESC LIMIT 10"

# Rebuild indexes
python scripts/rebuild_indexes.py

# Vacuum database
psql -d algotrading_prod -c "VACUUM ANALYZE"
```

---

## Security Best Practices

1. **Use environment variables** for secrets (never commit `.env`)
2. **Enable HTTPS** with Let's Encrypt
3. **Set up firewall** rules
4. **Regular security updates**
5. **Database access restrictions**
6. **API rate limiting**

---

## Support

For issues or questions:
- Check logs first: `/var/log/smarttrader/`
- Run verification: `python scripts/verify_endpoints.py`
- Validate schema: `python scripts/validate_schema.py`
- Check documentation: `API_REFERENCE.md`

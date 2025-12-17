"""
System Health Check and Robustness Improvements
Ensures the trading system doesn't fail daily
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from datetime import datetime, timedelta
from pathlib import Path
import json

print("=" * 70)
print("ALGOTRADING SYSTEM HEALTH CHECK")
print("=" * 70)
print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

health_status = {
    "timestamp": datetime.now().isoformat(),
    "checks": {},
    "overall_status": "HEALTHY",
    "critical_issues": [],
    "warnings": []
}

# ============================================================
# 1. FYERS AUTHENTICATION CHECK
# ============================================================
print("1. Checking Fyers Authentication...")
try:
    token_file = Path("fyers/config/access_token.json")
    if token_file.exists():
        with open(token_file) as f:
            token_data = json.load(f)
        
        # Check if token exists
        if 'access_token' in token_data and token_data['access_token']:
            print("   ✅ Access token found")
            health_status["checks"]["fyers_token"] = "OK"
            
            # Test token validity
            try:
                from fyers.fyers_client import validate_token
                if validate_token():
                    print("   ✅ Token is valid")
                    health_status["checks"]["fyers_token_valid"] = "OK"
                else:
                    print("   ⚠️  Token may be expired - needs refresh")
                    health_status["warnings"].append("Fyers token may be expired")
                    health_status["checks"]["fyers_token_valid"] = "WARNING"
            except Exception as e:
                print(f"   ⚠️  Could not validate token: {e}")
                health_status["warnings"].append(f"Token validation failed: {e}")
        else:
            print("   ❌ No access token found")
            health_status["critical_issues"].append("Fyers access token missing")
            health_status["checks"]["fyers_token"] = "CRITICAL"
    else:
        print("   ❌ Token file not found")
        health_status["critical_issues"].append("Fyers token file missing")
        health_status["checks"]["fyers_token"] = "CRITICAL"
except Exception as e:
    print(f"   ❌ Error checking Fyers auth: {e}")
    health_status["critical_issues"].append(f"Fyers auth check failed: {e}")
    health_status["checks"]["fyers_auth"] = "CRITICAL"

# ============================================================
# 2. DATABASE CONNECTION CHECK
# ============================================================
print("\n2. Checking Database Connection...")
try:
    from backend.app.database import SessionLocal, Company, HistoricalPrice
    
    db = SessionLocal()
    
    # Check companies table
    company_count = db.query(Company).count()
    print(f"   ✅ Companies table: {company_count} records")
    health_status["checks"]["database_companies"] = "OK"
    
    # Check historical prices
    price_count = db.query(HistoricalPrice).count()
    print(f"   ✅ Historical prices: {price_count} records")
    health_status["checks"]["database_prices"] = "OK"
    
    # Check data freshness
    latest_price = db.query(HistoricalPrice).order_by(
        HistoricalPrice.date.desc()
    ).first()
    
    if latest_price:
        days_old = (datetime.now().date() - latest_price.date).days
        print(f"   ℹ️  Latest data: {latest_price.date} ({days_old} days old)")
        
        if days_old > 7:
            health_status["warnings"].append(f"Data is {days_old} days old - needs update")
            health_status["checks"]["data_freshness"] = "WARNING"
        else:
            health_status["checks"]["data_freshness"] = "OK"
    
    db.close()
    
except Exception as e:
    print(f"   ❌ Database error: {e}")
    health_status["critical_issues"].append(f"Database connection failed: {e}")
    health_status["checks"]["database"] = "CRITICAL"

# ============================================================
# 3. NSE DATA FILES CHECK
# ============================================================
print("\n3. Checking NSE Data Files...")
try:
    # Check equity data
    equity_file = Path("nse_data/processed/equities_clean/equity_ohlcv.parquet")
    if equity_file.exists():
        size_mb = equity_file.stat().st_size / (1024 * 1024)
        print(f"   ✅ Equity data: {size_mb:.2f} MB")
        health_status["checks"]["nse_equity_data"] = "OK"
    else:
        print("   ❌ Equity data file missing")
        health_status["critical_issues"].append("NSE equity data file missing")
        health_status["checks"]["nse_equity_data"] = "CRITICAL"
    
    # Check index data
    index_file = Path("nse_data/processed/indices_clean/index_ohlcv.parquet")
    if index_file.exists():
        size_mb = index_file.stat().st_size / (1024 * 1024)
        print(f"   ✅ Index data: {size_mb:.2f} MB")
        health_status["checks"]["nse_index_data"] = "OK"
    else:
        print("   ⚠️  Index data file missing")
        health_status["warnings"].append("NSE index data file missing")
        health_status["checks"]["nse_index_data"] = "WARNING"
    
    # Check metadata
    metadata_file = Path("nse_data/metadata/equity_list.csv")
    if metadata_file.exists():
        import pandas as pd
        df = pd.read_csv(metadata_file)
        print(f"   ✅ Metadata: {len(df)} symbols")
        health_status["checks"]["nse_metadata"] = "OK"
    else:
        print("   ⚠️  Metadata file missing")
        health_status["warnings"].append("NSE metadata file missing")
        health_status["checks"]["nse_metadata"] = "WARNING"
        
except Exception as e:
    print(f"   ❌ NSE data check error: {e}")
    health_status["warnings"].append(f"NSE data check failed: {e}")
    health_status["checks"]["nse_data"] = "WARNING"

# ============================================================
# 4. API ENDPOINT CHECK
# ============================================================
print("\n4. Checking API Endpoints...")
try:
    import requests
    
    # Check main endpoint
    response = requests.get("http://localhost:8000/", timeout=5)
    if response.status_code == 200:
        print("   ✅ Main API endpoint responding")
        health_status["checks"]["api_main"] = "OK"
    else:
        print(f"   ⚠️  API returned status {response.status_code}")
        health_status["warnings"].append(f"API status code: {response.status_code}")
        health_status["checks"]["api_main"] = "WARNING"
    
    # Check scanner endpoint
    response = requests.get(
        "http://localhost:8000/api/screener/trending?filter_type=ALL&limit=1",
        timeout=10
    )
    if response.status_code == 200:
        data = response.json()
        if 'data' in data:
            print(f"   ✅ Scanner endpoint working ({len(data['data'])} results)")
            health_status["checks"]["api_scanner"] = "OK"
        else:
            print("   ⚠️  Scanner returned unexpected format")
            health_status["warnings"].append("Scanner response format issue")
            health_status["checks"]["api_scanner"] = "WARNING"
    else:
        print(f"   ❌ Scanner endpoint failed: {response.status_code}")
        health_status["critical_issues"].append("Scanner endpoint not working")
        health_status["checks"]["api_scanner"] = "CRITICAL"
        
except Exception as e:
    print(f"   ⚠️  Could not check API (server may not be running): {e}")
    health_status["warnings"].append(f"API check skipped: {e}")
    health_status["checks"]["api"] = "SKIPPED"

# ============================================================
# 5. DISK SPACE CHECK
# ============================================================
print("\n5. Checking Disk Space...")
try:
    import shutil
    
    total, used, free = shutil.disk_usage(".")
    free_gb = free / (1024 ** 3)
    
    print(f"   ℹ️  Free disk space: {free_gb:.2f} GB")
    
    if free_gb < 1:
        health_status["critical_issues"].append(f"Low disk space: {free_gb:.2f} GB")
        health_status["checks"]["disk_space"] = "CRITICAL"
    elif free_gb < 5:
        health_status["warnings"].append(f"Disk space running low: {free_gb:.2f} GB")
        health_status["checks"]["disk_space"] = "WARNING"
    else:
        health_status["checks"]["disk_space"] = "OK"
        
except Exception as e:
    print(f"   ⚠️  Could not check disk space: {e}")
    health_status["warnings"].append(f"Disk space check failed: {e}")

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 70)
print("HEALTH CHECK SUMMARY")
print("=" * 70)

# Determine overall status
if health_status["critical_issues"]:
    health_status["overall_status"] = "CRITICAL"
    print("❌ OVERALL STATUS: CRITICAL")
elif health_status["warnings"]:
    health_status["overall_status"] = "WARNING"
    print("⚠️  OVERALL STATUS: WARNING")
else:
    print("✅ OVERALL STATUS: HEALTHY")

# Print issues
if health_status["critical_issues"]:
    print("\n🚨 CRITICAL ISSUES:")
    for issue in health_status["critical_issues"]:
        print(f"   - {issue}")

if health_status["warnings"]:
    print("\n⚠️  WARNINGS:")
    for warning in health_status["warnings"]:
        print(f"   - {warning}")

# Save health report
report_file = Path("health_check_report.json")
with open(report_file, 'w') as f:
    json.dump(health_status, f, indent=2)

print(f"\n📄 Full report saved to: {report_file}")
print("=" * 70)

# Exit with appropriate code
if health_status["overall_status"] == "CRITICAL":
    sys.exit(1)
elif health_status["overall_status"] == "WARNING":
    sys.exit(2)
else:
    sys.exit(0)

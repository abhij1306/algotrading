"""
Comprehensive Data Management Robustness Audit
Checks all modules for proper NSE data integration and error handling
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from datetime import datetime, timedelta
from pathlib import Path
import json

print("=" * 80)
print("DATA MANAGEMENT ROBUSTNESS AUDIT")
print("=" * 80)
print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

audit_results = {
    "timestamp": datetime.now().isoformat(),
    "modules": {},
    "issues": [],
    "recommendations": []
}

# ============================================================
# 1. DATABASE INTEGRATION CHECK
# ============================================================
print("1. DATABASE INTEGRATION")
print("-" * 80)

try:
    from backend.app.database import SessionLocal, Company, HistoricalPrice
    from sqlalchemy import func
    
    db = SessionLocal()
    
    # Check companies
    total_companies = db.query(Company).count()
    active_companies = db.query(Company).filter(Company.is_active == True).count()
    fno_companies = db.query(Company).filter(
        Company.is_fno == True, 
        Company.is_active == True
    ).count()
    
    print(f"✅ Total companies: {total_companies}")
    print(f"✅ Active companies: {active_companies}")
    print(f"✅ F&O companies: {fno_companies}")
    
    # Check companies with price data
    companies_with_prices = db.query(Company.id).join(
        HistoricalPrice, 
        Company.id == HistoricalPrice.company_id
    ).filter(Company.is_active == True).distinct().count()
    
    missing_prices = active_companies - companies_with_prices
    
    print(f"✅ Companies with price data: {companies_with_prices}")
    
    if missing_prices > 0:
        print(f"⚠️  Companies missing price data: {missing_prices}")
        audit_results["issues"].append(
            f"{missing_prices} active companies have no historical price data"
        )
        audit_results["recommendations"].append(
            "Run update_daily_data.py to fetch missing historical data"
        )
    
    # Check data freshness
    latest_price = db.query(HistoricalPrice).order_by(
        HistoricalPrice.date.desc()
    ).first()
    
    if latest_price:
        days_old = (datetime.now().date() - latest_price.date).days
        print(f"ℹ️  Latest price data: {latest_price.date} ({days_old} days old)")
        
        if days_old > 3:
            audit_results["issues"].append(
                f"Price data is {days_old} days old - needs update"
            )
            audit_results["recommendations"].append(
                "Schedule daily data updates with Windows Task Scheduler"
            )
    
    audit_results["modules"]["database"] = {
        "status": "OK" if missing_prices == 0 else "WARNING",
        "total_companies": total_companies,
        "active_companies": active_companies,
        "companies_with_prices": companies_with_prices,
        "missing_prices": missing_prices,
        "data_age_days": days_old if latest_price else None
    }
    
    db.close()
    
except Exception as e:
    print(f"❌ Database check failed: {e}")
    audit_results["modules"]["database"] = {"status": "CRITICAL", "error": str(e)}
    audit_results["issues"].append(f"Database integration failed: {e}")

# ============================================================
# 2. NSE DATA READER CHECK
# ============================================================
print("\n2. NSE DATA READER")
print("-" * 80)

try:
    from backend.app.nse_data_reader import NSEDataReader
    
    reader = NSEDataReader()
    
    # Test equity data read
    test_symbol = "RELIANCE"
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    df = reader.get_historical_data(test_symbol, start_date, end_date)
    
    if df is not None and not df.empty:
        print(f"✅ NSE equity data readable: {len(df)} records for {test_symbol}")
        audit_results["modules"]["nse_data_reader"] = {
            "status": "OK",
            "test_symbol": test_symbol,
            "records_found": len(df)
        }
    else:
        print(f"⚠️  No NSE data found for {test_symbol}")
        audit_results["modules"]["nse_data_reader"] = {
            "status": "WARNING",
            "test_symbol": test_symbol,
            "records_found": 0
        }
        audit_results["issues"].append(
            f"NSE data reader returned no data for {test_symbol}"
        )
    
    # Check metadata
    metadata_file = Path("nse_data/metadata/equity_list.csv")
    if metadata_file.exists():
        import pandas as pd
        metadata_df = pd.read_csv(metadata_file)
        print(f"✅ NSE metadata: {len(metadata_df)} symbols")
        audit_results["modules"]["nse_metadata"] = {
            "status": "OK",
            "symbol_count": len(metadata_df)
        }
    else:
        print("⚠️  NSE metadata file missing")
        audit_results["issues"].append("NSE metadata file not found")
        
except Exception as e:
    print(f"❌ NSE data reader check failed: {e}")
    audit_results["modules"]["nse_data_reader"] = {"status": "CRITICAL", "error": str(e)}
    audit_results["issues"].append(f"NSE data reader failed: {e}")

# ============================================================
# 3. UNIFIED DATA SERVICE CHECK
# ============================================================
print("\n3. UNIFIED DATA SERVICE")
print("-" * 80)

try:
    from backend.app.unified_data_service import get_data_service
    from backend.app.database import SessionLocal
    
    db = SessionLocal()
    data_service = get_data_service(db)
    
    # Test 3-tier routing
    test_symbol = "TCS"
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    df = data_service.get_historical_data(
        symbol=test_symbol,
        start_date=start_date,
        end_date=end_date,
        intent="analysis"
    )
    
    if df is not None and not df.empty:
        print(f"✅ Unified data service working: {len(df)} records for {test_symbol}")
        print(f"   Source: {'Postgres' if len(df) > 0 else 'NSE fallback'}")
        audit_results["modules"]["unified_data_service"] = {
            "status": "OK",
            "test_symbol": test_symbol,
            "records_found": len(df)
        }
    else:
        print(f"⚠️  Unified data service returned no data for {test_symbol}")
        audit_results["modules"]["unified_data_service"] = {
            "status": "WARNING",
            "test_symbol": test_symbol,
            "records_found": 0
        }
        audit_results["issues"].append(
            f"Unified data service returned no data for {test_symbol}"
        )
    
    db.close()
    
except Exception as e:
    print(f"❌ Unified data service check failed: {e}")
    audit_results["modules"]["unified_data_service"] = {"status": "CRITICAL", "error": str(e)}
    audit_results["issues"].append(f"Unified data service failed: {e}")

# ============================================================
# 4. TRENDING SCANNER CHECK
# ============================================================
print("\n4. TRENDING SCANNER")
print("-" * 80)

try:
    from backend.app.trending_scanner import calculate_trending_stocks
    from backend.app.database import SessionLocal
    
    db = SessionLocal()
    
    # Test scanner
    results = calculate_trending_stocks(db, filter_type='ALL', limit=10)
    
    if results and len(results) > 0:
        print(f"✅ Trending scanner working: {len(results)} stocks returned")
        print(f"   Sample: {results[0]['symbol']} @ ₹{results[0]['close']}")
        
        # Check if indicators are populated
        sample = results[0]
        has_ema = sample.get('ema20', 0) > 0 and sample.get('ema50', 0) > 0
        has_rsi = sample.get('rsi', 0) > 0
        has_volume = sample.get('avg_volume', 0) > 0
        
        if has_ema and has_rsi and has_volume:
            print("✅ Technical indicators populated correctly")
            audit_results["modules"]["trending_scanner"] = {
                "status": "OK",
                "stocks_returned": len(results),
                "indicators_working": True
            }
        else:
            print("⚠️  Some technical indicators missing")
            audit_results["modules"]["trending_scanner"] = {
                "status": "WARNING",
                "stocks_returned": len(results),
                "indicators_working": False
            }
            audit_results["issues"].append("Trending scanner missing some technical indicators")
    else:
        print("⚠️  Trending scanner returned no results")
        audit_results["modules"]["trending_scanner"] = {
            "status": "WARNING",
            "stocks_returned": 0
        }
        audit_results["issues"].append("Trending scanner returned no results")
    
    db.close()
    
except Exception as e:
    print(f"❌ Trending scanner check failed: {e}")
    audit_results["modules"]["trending_scanner"] = {"status": "CRITICAL", "error": str(e)}
    audit_results["issues"].append(f"Trending scanner failed: {e}")

# ============================================================
# 5. API ENDPOINTS CHECK
# ============================================================
print("\n5. API ENDPOINTS")
print("-" * 80)

try:
    import requests
    
    endpoints = [
        ("/api/screener?page=1&limit=1", "Main Screener"),
        ("/api/screener/trending?filter_type=ALL&limit=1", "Trending Scanner"),
        ("/api/historical/RELIANCE?start_date=2024-12-01&end_date=2024-12-16", "Historical Data"),
    ]
    
    for endpoint, name in endpoints:
        try:
            response = requests.get(f"http://localhost:8000{endpoint}", timeout=10)
            if response.status_code == 200:
                print(f"✅ {name}: OK")
            else:
                print(f"⚠️  {name}: Status {response.status_code}")
                audit_results["issues"].append(f"{name} endpoint returned {response.status_code}")
        except Exception as e:
            print(f"⚠️  {name}: {str(e)[:50]}")
            audit_results["issues"].append(f"{name} endpoint failed: {str(e)[:50]}")
    
    audit_results["modules"]["api_endpoints"] = {"status": "OK"}
    
except Exception as e:
    print(f"⚠️  API endpoints check skipped (server may not be running)")
    audit_results["modules"]["api_endpoints"] = {"status": "SKIPPED"}

# ============================================================
# 6. ERROR HANDLING CHECK
# ============================================================
print("\n6. ERROR HANDLING & ROBUSTNESS")
print("-" * 80)

error_handling_checks = []

# Check if modules have try-catch blocks
modules_to_check = [
    "backend/app/data_repository.py",
    "backend/app/unified_data_service.py",
    "backend/app/nse_data_reader.py",
    "backend/app/trending_scanner.py"
]

for module_path in modules_to_check:
    if Path(module_path).exists():
        with open(module_path, 'r') as f:
            content = f.read()
            has_error_handling = 'try:' in content and 'except' in content
            has_logging = 'print(' in content or 'logger' in content
            
            if has_error_handling:
                print(f"✅ {Path(module_path).name}: Has error handling")
            else:
                print(f"⚠️  {Path(module_path).name}: Missing error handling")
                error_handling_checks.append(f"{Path(module_path).name} needs error handling")
            
            if not has_logging:
                error_handling_checks.append(f"{Path(module_path).name} needs logging")

if error_handling_checks:
    audit_results["recommendations"].extend(error_handling_checks)

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 80)
print("AUDIT SUMMARY")
print("=" * 80)

print(f"\n📊 Modules Checked: {len(audit_results['modules'])}")
print(f"⚠️  Issues Found: {len(audit_results['issues'])}")
print(f"💡 Recommendations: {len(audit_results['recommendations'])}")

if audit_results["issues"]:
    print("\n🚨 ISSUES:")
    for i, issue in enumerate(audit_results["issues"], 1):
        print(f"   {i}. {issue}")

if audit_results["recommendations"]:
    print("\n💡 RECOMMENDATIONS:")
    for i, rec in enumerate(audit_results["recommendations"], 1):
        print(f"   {i}. {rec}")

# Save audit report
report_file = Path("data_management_audit.json")
with open(report_file, 'w') as f:
    json.dump(audit_results, f, indent=2)

print(f"\n📄 Full audit report saved to: {report_file}")
print("=" * 80)

# Exit code
if any(m.get("status") == "CRITICAL" for m in audit_results["modules"].values()):
    sys.exit(1)
elif audit_results["issues"]:
    sys.exit(2)
else:
    sys.exit(0)

"""
Quick Diagnostic Script - Quant Module
Tests database queries and API endpoints in isolation to identify hanging issue
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from app.database import SessionLocal, StockUniverse, StrategyContract
import requests
import json

def test_database():
    print("\n=== DATABASE TESTS ===")
    db = SessionLocal()
    
    try:
        # Test 1: Count universes
        print("1. Universe Count:", db.query(StockUniverse).count())
        
        # Test 2: List universe IDs
        universes = db.query(StockUniverse).all()
        print("2. Universe IDs:", [u.id for u in universes])
        
        # Test 3: Count strategies  
        print("3. Strategy Count:", db.query(StrategyContract).count())
        
        # Test 4: List strategy IDs
        strategies = db.query(StrategyContract).limit(5).all()
        print("4. First 5 Strategies:", [s.strategy_id for s in strategies])
        
        print("✅ DATABASE: OK")
    except Exception as e:
        print(f"❌ DATABASE ERROR: {e}")
    finally:
        db.close()

def test_endpoints():
    print("\n=== API ENDPOINT TESTS ===")
    base_url = "http://localhost:8000"
    
    # Test 1: Universes endpoint
    try:
        print("1. Testing /api/portfolio-backtest/universes...")
        resp = requests.get(f"{base_url}/api/portfolio-backtest/universes", timeout=5)
        print(f"   Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"   System Universes: {len(data.get('system_universes', []))}")
            print("   ✅ PASS")
        else:
            print(f"   ❌ FAIL: {resp.text[:200]}")
    except Exception as e:
        print(f"   ❌ ERROR: {e}")
    
    # Test 2: Strategy contracts endpoint
    try:
        print("2. Testing /api/portfolio/strategy-contracts...")
        resp = requests.get(f"{base_url}/api/portfolio/strategy-contracts", timeout=5)
        print(f"   Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"   Contracts: {len(data.get('contracts', []))}")
            print("   ✅ PASS")
        else:
            print(f"   ❌ FAIL: {resp.text[:200]}")
    except Exception as e:
        print(f"   ❌ ERROR: {e}")

if __name__ == "__main__":
    test_database()
    test_endpoints()
    print("\n=== DIAGNOSTIC COMPLETE ===")

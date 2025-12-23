"""
System Verification Script
Tests all API endpoints and generates health report

Usage:
    python verify_endpoints.py
    python verify_endpoints.py --save-report
"""
import requests
import json
from datetime import datetime
from typing import Dict, List
import sys

BASE_URL = "http://localhost:8000"

class EndpointTester:
    def __init__(self):
        self.results = {}
        self.total_tests = 0
        self.passed_tests = 0
        self.failed_tests = 0
    
    def test_endpoint(self, name: str, method: str, endpoint: str, expected_status: int = 200, data: Dict = None):
        """Test a single endpoint"""
        self.total_tests += 1
        url = f"{BASE_URL}{endpoint}"
        
        try:
            if method == "GET":
                response = requests.get(url, timeout=5)
            elif method == "POST":
                response = requests.post(url, json=data or {}, timeout=5)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            success = response.status_code == expected_status
            
            if success:
                self.passed_tests += 1
                status = "✅ PASS"
            else:
                self.failed_tests += 1
                status = "❌ FAIL"
            
            self.results[name] = {
                "status": status,
                "endpoint": endpoint,
                "method": method,
                "expected_status": expected_status,
                "actual_status": response.status_code,
                "response_time_ms": int(response.elapsed.total_seconds() * 1000),
                "error": None if success else response.text[:200]
            }
            
            print(f"{status} | {method:4} {endpoint:50} | {response.status_code} | {self.results[name]['response_time_ms']}ms")
            
        except requests.exceptions.ConnectionError:
            self.failed_tests += 1
            self.results[name] = {
                "status": "❌ FAIL",
                "endpoint": endpoint,
                "method": method,
                "expected_status": expected_status,
                "actual_status": None,
                "response_time_ms": None,
                "error": "Connection refused - Is backend running?"
            }
            print(f"❌ FAIL | {method:4} {endpoint:50} | CONNECTION_ERROR")
            
        except Exception as e:
            self.failed_tests += 1
            self.results[name] = {
                "status": "❌ FAIL",
                "endpoint": endpoint,
                "method": method,
                "expected_status": expected_status,
                "actual_status": None,
                "response_time_ms": None,
                "error": str(e)
            }
            print(f"❌ FAIL | {method:4} {endpoint:50} | ERROR: {str(e)[:50]}")
    
    def run_all_tests(self):
        """Run all endpoint tests"""
        print("\n" + "="*100)
        print("SMARTTRADER 3.0 - SYSTEM VERIFICATION")
        print("="*100 + "\n")
        
        # System Health
        print("📊 SYSTEM HEALTH ENDPOINTS")
        print("-"*100)
        self.test_endpoint("System Health", "GET", "/api/system/health")
        self.test_endpoint("Screener Pre-flight", "GET", "/api/system/pre-flight/screener")
        
        # Market Data
        print("\n📈 MARKET DATA ENDPOINTS")
        print("-"*100)
        self.test_endpoint("Market Search", "GET", "/api/market/search?q=NIFTY", expected_status=200)
        
        # Analyst Endpoints
        print("\n💼 ANALYST ENDPOINTS")
        print("-"*100)
        self.test_endpoint("List Portfolios", "GET", "/api/analyst/portfolios")
        self.test_endpoint("Analyst Backtest Strategies", "GET", "/api/analyst/backtest/strategies")
        
        # Quant Endpoints
        print("\n🔬 QUANT ENDPOINTS")
        print("-"*100)
        self.test_endpoint("Quant Strategies", "GET", "/api/quant/backtest/strategies")
        self.test_endpoint("Lifecycle Summary", "GET", "/api/quant/lifecycle/states/summary")
        self.test_endpoint("Research Strategies", "GET", "/api/research/strategies")
        self.test_endpoint("Quant Research Compare", "GET", "/api/quant/research/regime-analysis?start_date=2024-01-01&end_date=2024-12-01")
        
        # Screener
        print("\n🔍 SCREENER ENDPOINTS")
        print("-"*100)
        self.test_endpoint("Screener Scan", "GET", "/api/screener/scan?view=intraday&threshold=0.01", expected_status=200)
        
        print("\n" + "="*100)
        print(f"RESULTS: {self.passed_tests}/{self.total_tests} tests passed ({self.failed_tests} failed)")
        print("="*100 + "\n")
    
    def generate_report(self, save_to_file: bool = False):
        """Generate detailed health report"""
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "total_tests": self.total_tests,
                "passed": self.passed_tests,
                "failed": self.failed_tests,
                "success_rate": f"{(self.passed_tests/self.total_tests*100):.1f}%" if self.total_tests > 0 else "0%"
            },
            "endpoints": self.results
        }
        
        if save_to_file:
            filename = f"endpoint_health_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(filename, 'w') as f:
                json.dump(report, f, indent=2)
            print(f"📄 Report saved to: {filename}")
        
        return report


def main():
    """Main execution"""
    tester = EndpointTester()
    
    # Run all tests
    tester.run_all_tests()
    
    # Generate report
    save_report = "--save-report" in sys.argv
    tester.generate_report(save_to_file=save_report)
    
    # Exit with appropriate code
    sys.exit(0 if tester.failed_tests == 0 else 1)


if __name__ == "__main__":
    main()

"""
Quick test to verify OpenRouter API is working
"""
from .openrouter_classifier import classify_sector_with_ai

def test_api():
    print("Testing OpenRouter API...")
    print("="*60)
    
    # Test with a well-known company
    test_cases = [
        ("Tata Consultancy Services Limited", "TCS"),
        ("HDFC Bank Limited", "HDFCBANK"),
        ("Reliance Industries Limited", "RELIANCE"),
    ]
    
    for name, symbol in test_cases:
        print(f"\nTesting: {symbol} - {name}")
        result = classify_sector_with_ai(name, symbol)
        
        if result:
            print(f"  ✅ SUCCESS")
            print(f"  Sector: {result.get('sector')}")
            print(f"  Industry: {result.get('industry')}")
        else:
            print(f"  ❌ FAILED - API returned None")
            return False
    
    print("\n" + "="*60)
    print("✅ All tests passed! OpenRouter API is working.")
    return True

if __name__ == "__main__":
    test_api()

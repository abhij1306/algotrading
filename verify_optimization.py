import sys
import os
import asyncio

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

async def verify():
    print("1. Testing LLMClient (LiteLLM)...")
    try:
        from app.smart_trader.llm_client import LLMClient
        client = LLMClient(provider="groq")
        print("   ✅ LLMClient initialized")
    except Exception as e:
        print(f"   ❌ LLMClient Failed: {e}")

    print("\n2. Testing StockScannerAgent (Parallel)...")
    try:
        from app.smart_trader.stock_scanner import StockScannerAgent
        scanner = StockScannerAgent({})
        print("   ✅ StockScannerAgent initialized")
    except Exception as e:
        print(f"   ❌ StockScannerAgent Failed: {e}")

    print("\n3. Testing FastTradingLoop...")
    try:
        from app.smart_trader.fast_loop import FastTradingLoop
        loop = FastTradingLoop({})
        print("   ✅ FastTradingLoop initialized")
    except Exception as e:
        print(f"   ❌ FastTradingLoop Failed: {e}")

if __name__ == "__main__":
    asyncio.run(verify())

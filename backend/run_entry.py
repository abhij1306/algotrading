
import uvicorn
import os
import sys
from pathlib import Path

# Add the current directory to sys.path so app can be imported
base_dir = Path(__file__).resolve().parent
sys.path.append(str(base_dir))

if __name__ == "__main__":
    # In a packaged executable, we might want to override some env vars
    # or handle specific startup logic.
    print("Starting SmartTrader Backend Server...")
    
    # We use 127.0.0.1 for local-only security as requested in implementation plan
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)

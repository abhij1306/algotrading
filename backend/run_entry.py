
import uvicorn
import os
import sys
from pathlib import Path

# Add the current directory to sys.path so app can be imported
base_dir = Path(__file__).resolve().parent
sys.path.append(str(base_dir))

if __name__ == "__main__":
    try:
        # Import app name string for uvicorn reload support
        # uvicorn.run("app.main:app", ...) allows for code reloading
        
        print("Starting SmartTrader Backend (with reload)...")
        uvicorn.run("app.main:app", host="0.0.0.0", port=8000, log_level="info", reload=True)
    except Exception as e:
        import traceback
        print("CRITICAL ERROR DURING STARTUP:")
        traceback.print_exc()
        input("Press Enter to exit...")
        sys.exit(1)

import subprocess
import time
import webbrowser
import os
import sys
import socket
from contextlib import closing

# Configuration
BACKEND_PORT = 8000
FRONTEND_PORT = 3000
BACKEND_DIR = os.path.join(os.getcwd(), "backend")
FRONTEND_DIR = os.path.join(os.getcwd(), "frontend")
FYERS_LOGIN_URL = f"http://localhost:{BACKEND_PORT}/api/auth/fyers/login"
DASHBOARD_URL = f"http://localhost:{FRONTEND_PORT}"

def is_port_in_use(port):
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        return s.connect_ex(('localhost', port)) == 0

def wait_for_service(port, name, timeout=60):
    print(f"⏳ Waiting for {name} on port {port}...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        if is_port_in_use(port):
            print(f"✅ {name} is active!")
            return True
        time.sleep(1)
    print(f"❌ Timed out waiting for {name}.")
    return False

def main():
    print("🚀 Starting AlgoTrading Development Environment...")

    # 1. Start Backend
    print(f"🔹 Launching Backend from {BACKEND_DIR}...")
    backend_env = os.environ.copy()
    backend_env["PYTHONUNBUFFERED"] = "1"
    
    # Using python directly to run the entry point
    backend_process = subprocess.Popen(
        [sys.executable, "run_entry.py"],
        cwd=BACKEND_DIR,
        env=backend_env,
        creationflags=subprocess.CREATE_NEW_CONSOLE  # Opens a new window for backend logs
    )

    # 2. Start Frontend
    print(f"🔹 Launching Frontend from {FRONTEND_DIR}...")
    # Use 'npm.cmd' on Windows
    npm_cmd = "npm.cmd" if os.name == 'nt' else "npm"
    frontend_process = subprocess.Popen(
        [npm_cmd, "run", "dev"],
        cwd=FRONTEND_DIR,
        creationflags=subprocess.CREATE_NEW_CONSOLE # Opens a new window for frontend logs
    )

    # 3. Wait for Backend (API) to be ready
    if wait_for_service(BACKEND_PORT, "Backend"):
        # 4. Run Fyers Login Script
        print("🔐 Running Fyers Login Script...")
        fyers_login_script = os.path.join(os.getcwd(), "fyers", "fyers_login.py")
        if os.path.exists(fyers_login_script):
            subprocess.Popen(
                [sys.executable, fyers_login_script],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:
            print("⚠️  fyers_login.py not found, skipping auto-login.")
    
    # 5. Wait for Frontend to be ready
    if wait_for_service(FRONTEND_PORT, "Frontend"):
        print(f"💻 Opening Dashboard: {DASHBOARD_URL}")
        webbrowser.open(DASHBOARD_URL)

    print("\n✅ System Running! Press Ctrl+C in the separate windows to stop servers.")
    print("   (This script will now exit, but servers remain running)")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Error: {e}")
        input("Press Enter to exit...")

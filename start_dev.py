import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from pathlib import Path

# Preferred defaults
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 3000
MAX_PORT_SCAN = 50


def is_port_in_use(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_available_port(preferred: int, max_scan: int = MAX_PORT_SCAN) -> int:
    for port in range(preferred, preferred + max_scan):
        if not is_port_in_use(port):
            return port
    raise RuntimeError(f"No free port found in range {preferred}-{preferred + max_scan - 1}")


def kill_processes_on_port(port: int) -> None:
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                f"netstat -ano | findstr :{port} | findstr LISTENING",
                shell=True,
                capture_output=True,
                text=True,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                pid = parts[-1]
                subprocess.run(
                    f"taskkill /F /PID {pid}",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
        else:
            result = subprocess.run(
                f"lsof -ti :{port}",
                shell=True,
                capture_output=True,
                text=True,
            )
            for pid in result.stdout.splitlines():
                subprocess.run(
                    f"kill -9 {pid}",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
    except Exception:
        # Best effort cleanup only.
        pass


def is_backend_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def wait_for_service(port: int, name: str, timeout: int = 60) -> bool:
    print(f"Waiting for {name} on port {port}...")
    start = time.time()
    while time.time() - start < timeout:
        if is_port_in_use(port):
            print(f"{name} is running on port {port}")
            return True
        time.sleep(1)
    print(f"Timed out waiting for {name} on port {port}")
    return False


def build_python_command(project_root: Path) -> list[str]:
    venv_python = project_root / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return [str(venv_python)]
    return [sys.executable]


def main() -> None:
    project_root = Path(__file__).resolve().parent
    backend_dir = project_root / "backend"
    frontend_dir = project_root / "frontend"

    if not backend_dir.exists():
        raise RuntimeError(f"Backend directory not found: {backend_dir}")
    if not frontend_dir.exists():
        raise RuntimeError(f"Frontend directory not found: {frontend_dir}")

    print("Starting SmartTrader development environment...")

    backend_port = DEFAULT_BACKEND_PORT
    frontend_port = find_available_port(DEFAULT_FRONTEND_PORT)
    launch_backend = True

    if frontend_port != DEFAULT_FRONTEND_PORT:
        print(f"[INFO] Port {DEFAULT_FRONTEND_PORT} is busy. Using frontend port {frontend_port}.")

    if is_port_in_use(backend_port):
        if is_backend_healthy(backend_port):
            print(f"[INFO] Backend already running on port {backend_port}; reusing it.")
            launch_backend = False
        else:
            print(f"[WARN] Port {backend_port} is occupied by another process. Attempting to free it...")
            kill_processes_on_port(backend_port)
            time.sleep(1)
            if is_port_in_use(backend_port):
                raise RuntimeError(
                    f"Port {backend_port} is still in use. Stop the process and retry."
                )

    python_cmd = build_python_command(project_root)
    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    create_console_flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    backend_env = os.environ.copy()
    backend_env["PYTHONUNBUFFERED"] = "1"
    backend_env["BACKEND_HOST"] = "127.0.0.1"
    backend_env["BACKEND_PORT"] = str(backend_port)
    backend_env.setdefault("BACKEND_RELOAD", "true")
    # Keep market-hours behavior real by default; set DEV_MODE=true explicitly when needed.
    backend_env.setdefault("DEV_MODE", "false")

    frontend_env = os.environ.copy()
    frontend_env["PORT"] = str(frontend_port)
    frontend_env["NEXT_PUBLIC_API_URL"] = f"http://127.0.0.1:{backend_port}"
    frontend_env["NEXT_PUBLIC_WS_URL"] = f"ws://127.0.0.1:{backend_port}/api/websocket/stream"

    backend_proc = None
    if launch_backend:
        print(f"Launching backend on http://127.0.0.1:{backend_port}")
        backend_proc = subprocess.Popen(
            python_cmd + ["backend/start_server.py"],
            cwd=project_root,
            env=backend_env,
            creationflags=create_console_flag,
        )

    print(f"Launching frontend on http://127.0.0.1:{frontend_port}")
    subprocess.Popen(
        [npm_cmd, "run", "dev"],
        cwd=frontend_dir,
        env=frontend_env,
        creationflags=create_console_flag,
    )

    backend_ok = True if not launch_backend else wait_for_service(backend_port, "Backend", timeout=90)
    if launch_backend and backend_proc is not None and backend_proc.poll() is not None:
        raise RuntimeError(
            f"Backend process exited immediately with code {backend_proc.returncode}. "
            "Run '.\\venv\\Scripts\\python.exe backend\\start_server.py' to see traceback."
        )
    frontend_ok = wait_for_service(frontend_port, "Frontend", timeout=120)

    if backend_ok and frontend_ok:
        dashboard_url = f"http://127.0.0.1:{frontend_port}"
        print(f"Opening dashboard: {dashboard_url}")
        webbrowser.open(dashboard_url)

    print("\nSmartTrader startup completed.")
    print(f"Backend:  http://127.0.0.1:{backend_port}")
    print(f"Frontend: http://127.0.0.1:{frontend_port}")
    print("Processes are running in separate windows.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStartup interrupted by user.")
    except Exception as error:
        print(f"Startup failed: {error}")
        if platform.system() == "Windows":
            input("Press Enter to exit...")

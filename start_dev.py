"""Start the SmartTrader dev environment: backend (uvicorn) + frontend (Vite+).

Usage:
    python start_dev.py [--no-browser]

Both servers run as children of this process; Ctrl+C stops them. Ports: backend 8000
(reused if already healthy), frontend 3000 (next free port if busy).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 3000
MAX_PORT_SCAN = 50
IS_WINDOWS = sys.platform == "win32"


class StartupError(RuntimeError):
    """A startup precondition failed; the message is shown to the user as-is."""


def is_port_in_use(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        return sock.connect_ex((HOST, port)) == 0


def find_available_port(preferred: int) -> int:
    for port in range(preferred, preferred + MAX_PORT_SCAN):
        if not is_port_in_use(port):
            return port
    raise StartupError(
        f"No free port in range {preferred}-{preferred + MAX_PORT_SCAN - 1}."
    )


def is_backend_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{port}/ping", timeout=2
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def venv_python(root: Path) -> Path:
    if IS_WINDOWS:
        return root / "venv" / "Scripts" / "python.exe"
    return root / "venv" / "bin" / "python"


def stop_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a child and everything it spawned (pnpm -> node, uvicorn reloader)."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_for_port(
    port: int, name: str, proc: subprocess.Popen | None, timeout: int
) -> None:
    print(f"Waiting for {name} on port {port}...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise StartupError(
                f"{name} exited during startup (code {proc.returncode}). See its output above."
            )
        if is_port_in_use(port):
            print(f"{name} is up on port {port}")
            return
        time.sleep(1)
    raise StartupError(f"Timed out after {timeout}s waiting for {name} on port {port}.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start the SmartTrader backend and frontend dev servers."
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the app in a browser"
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    python = venv_python(root)
    if not python.exists():
        raise StartupError(
            f"Virtualenv not found at {python}.\n"
            "Create it with: python -m venv venv && python scripts/install_backend_deps.py"
        )
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise StartupError(
            "pnpm is not on PATH. Install it from https://pnpm.io/installation."
        )
    if not (root / "frontend" / "node_modules").exists():
        raise StartupError(
            "Frontend dependencies missing. Run: cd frontend && pnpm install"
        )

    backend_port = DEFAULT_BACKEND_PORT
    frontend_port = find_available_port(DEFAULT_FRONTEND_PORT)
    if frontend_port != DEFAULT_FRONTEND_PORT:
        print(
            f"[INFO] Port {DEFAULT_FRONTEND_PORT} is busy; using frontend port {frontend_port}."
        )

    launch_backend = True
    if is_port_in_use(backend_port):
        if not is_backend_healthy(backend_port):
            raise StartupError(
                f"Port {backend_port} is occupied by a process that is not the SmartTrader backend. "
                f"Find it with: netstat -ano | findstr :{backend_port}"
            )
        print(f"[INFO] Backend already running on port {backend_port}; reusing it.")
        launch_backend = False

    backend_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "BACKEND_HOST": HOST,
        "BACKEND_PORT": str(backend_port),
    }
    backend_env.setdefault("BACKEND_RELOAD", "false")
    # Keep market-hours behavior real by default; set DEV_MODE=true explicitly when needed.
    backend_env.setdefault("DEV_MODE", "false")

    # The Vite dev server proxies /api (HTTP and WebSocket) to BACKEND_URL.
    frontend_env = {
        **os.environ,
        "PORT": str(frontend_port),
        "BACKEND_URL": f"http://{HOST}:{backend_port}",
    }

    procs: list[subprocess.Popen] = []
    try:
        backend_proc = None
        if launch_backend:
            print(f"Launching backend on http://{HOST}:{backend_port}")
            backend_proc = subprocess.Popen(
                [str(python), "backend/start_server.py"], cwd=root, env=backend_env
            )
            procs.append(backend_proc)

        print(f"Launching frontend on http://{HOST}:{frontend_port}")
        frontend_proc = subprocess.Popen(
            [pnpm, "dev"], cwd=root / "frontend", env=frontend_env
        )
        procs.append(frontend_proc)

        if backend_proc is not None:
            wait_for_port(backend_port, "Backend", backend_proc, timeout=90)
        wait_for_port(frontend_port, "Frontend", frontend_proc, timeout=120)

        url = f"http://{HOST}:{frontend_port}"
        print(
            f"\nSmartTrader is running.\n  Backend:  http://{HOST}:{backend_port}\n  Frontend: {url}"
        )
        print("Press Ctrl+C to stop.")
        if not args.no_browser:
            webbrowser.open(url)

        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("A server process exited; shutting down.")
        return 1
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        for proc in reversed(procs):
            stop_process_tree(proc)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StartupError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        sys.exit(1)

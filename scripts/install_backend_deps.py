"""Install backend dependencies into the repo-root venv.

Usage (from repo root):
    python scripts/install_backend_deps.py

fyers-apiv3 pins an old aiohttp build that fails to compile on Windows/Python 3.14,
so it is installed with --no-deps after the core requirements.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FYERS_SPEC = "fyers-apiv3==3.1.10"


def venv_python() -> Path:
    if sys.platform == "win32":
        return ROOT / "venv" / "Scripts" / "python.exe"
    return ROOT / "venv" / "bin" / "python"


def main() -> int:
    python = venv_python()
    if not python.exists():
        print(
            f"[ERROR] {python} not found. Create it first: python -m venv venv",
            file=sys.stderr,
        )
        return 1

    print("[1/2] Installing core backend dependencies...")
    core = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "backend" / "requirements.txt"),
        ]
    )
    if core.returncode != 0:
        return core.returncode

    print(f"[2/2] Installing {FYERS_SPEC} without its pinned dependencies...")
    fyers = subprocess.run(
        [str(python), "-m", "pip", "install", FYERS_SPEC, "--no-deps"]
    )
    if fyers.returncode != 0:
        return fyers.returncode

    print("[OK] Backend dependencies installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

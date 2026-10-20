"""Install backend dependencies into the repo-root venv.

Usage (from repo root):
    python scripts/install_backend_deps.py

fyers-apiv3 pins an old aiohttp build that fails to compile on Windows/Python 3.14,
so it is installed with --no-deps after the core requirements.

Versions are constrained to backend/requirements-ci.lock (the set CI tests), so an
existing venv is upgraded or downgraded to match CI rather than keeping whatever
transitive versions it already had.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FYERS_SPEC = "fyers-apiv3==3.2.0"
CI_LOCK = ROOT / "backend" / "requirements-ci.lock"


def lock_constraints() -> str:
    """name==version lines from the CI lock; its hashes are Linux wheels, so drop them."""
    lines = []
    for raw in CI_LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line.split(" ", 1)[0])
    return "\n".join(lines) + "\n"


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

    print("[1/2] Installing backend and test dependencies (pinned to the CI lock)...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        constraints = Path(tmp_dir) / "constraints.txt"
        constraints.write_bytes(lock_constraints().encode("utf-8"))
        core = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-r",
                str(ROOT / "backend" / "requirements-dev.txt"),
                "-c",
                str(constraints),
            ]
        )
    if core.returncode != 0:
        return core.returncode

    print(f"[2/2] Installing {FYERS_SPEC} without its pinned dependencies...")
    fyers = subprocess.run([str(python), "-m", "pip", "install", FYERS_SPEC, "--no-deps"])
    if fyers.returncode != 0:
        return fyers.returncode

    print("[OK] Backend dependencies installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

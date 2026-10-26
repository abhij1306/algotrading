"""Log in to Fyers from this machine's desktop and save the day's token.

    python scripts/fyers_login.py [--timeout 900]

Opens the Fyers login page in a fresh Chromium or Chrome profile on `DISPLAY` (default `:1`, the
desktop the dashboard shows), waits for the redirect that carries the auth code, and exchanges
it through HAT's own login flow (`fyers_auth.exchange`: the account check and the token file are HAT's).
The owner types the credentials into the browser; the script never prints the auth code or the
token, only whether the login connected. The same login is in the app's header.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
DEBUG_PORT = 9333  # the browser's remote debugging port, read for the redirect


def auth_code(tabs: Iterable[Mapping[str, Any]]) -> str | None:
    """The `auth_code` query parameter of the first open tab whose URL carries one."""
    for tab in tabs:
        q = parse_qs(urlparse(str(tab.get("url", ""))).query)
        if "auth_code" in q:
            return q["auth_code"][0]
    return None


def wait_for_code(port: int, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as r:  # noqa: S310 - local
                code = auth_code(json.load(r))
        except (OSError, ValueError):
            continue
        if code:
            return code
    return None


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fyers_login.py", description=__doc__)
    p.add_argument("--timeout", type=float, default=900, help="seconds to wait for the login")
    args = p.parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import app.database  # noqa: F401, PLC0415 - loads .env
    from app.hat.execution.fyers_auth import fyers_auth  # noqa: PLC0415

    browser = next((path for name in BROWSERS if (path := shutil.which(name))), None)
    if browser is None:
        sys.exit(f"none of {', '.join(BROWSERS)} found")
    with tempfile.TemporaryDirectory(prefix="fyers-login-") as profile:
        proc = subprocess.Popen(
            [
                browser,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--new-window",
                fyers_auth.login_url(),
            ],
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":1")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("browser opened; waiting for the login", flush=True)
        try:
            code = wait_for_code(DEBUG_PORT, args.timeout)
        finally:
            proc.terminate()
    if code is None:
        sys.exit("timed out waiting for the Fyers redirect")
    status = fyers_auth.exchange(code)
    print(f"connected: {status.connected}")
    return 0 if status.connected else 1


if __name__ == "__main__":
    sys.exit(main())

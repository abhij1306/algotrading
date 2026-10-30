"""Log in to Fyers from this machine's desktop and save the day's token.

    python scripts/fyers_login.py [--timeout 900]

Opens the Fyers login page in a fresh Chromium or Chrome profile on `DISPLAY` (default `:1`, the
desktop the dashboard shows), waits for the redirect that carries the auth code, and exchanges
it through HAT's own login flow (`fyers_auth.exchange`: the account check and the token file
are HAT's). The owner types the credentials into the browser; the script never prints the auth
code or the token, only whether the login connected. The same login is in the app's header.

Once the redirect is read the browser is closed and waited for (`close_browser`), the code is
exchanged, and only then is the temporary profile removed (`remove_profile`: a browser still
flushing its profile makes `rmtree` fail with "Directory not empty", so it retries and then
gives up with a warning). Nothing after the exchange fails the run.
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
BROWSERS = ("chromium", "chromium-browser", "google-chrome")
DEBUG_PORT = 9333  # the browser's remote debugging port, read for the redirect
CLOSE_WAIT = 10.0  # seconds a terminated browser has to exit before it is killed
REMOVE_TRIES = 5  # attempts at removing the profile, `REMOVE_PAUSE` seconds apart
REMOVE_PAUSE = 1.0


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


def close_browser(proc: Any, wait: float = CLOSE_WAIT) -> None:  # noqa: ANN401 - a Popen
    """Terminate the browser and wait for it to exit, killing it after `wait` seconds; never
    raises (the login is decided already)."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=wait)
    except (OSError, subprocess.SubprocessError):
        pass


def remove_profile(
    path: str,
    tries: int = REMOVE_TRIES,
    pause: float = REMOVE_PAUSE,
    rmtree: Callable[[str], None] = shutil.rmtree,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Remove the browser's temporary profile: up to `tries` attempts `pause` seconds apart (a
    browser child may still be writing to it), then a last one that ignores errors. True when
    it is gone; never raises."""
    for attempt in range(tries):
        try:
            rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt + 1 < tries:
                sleep(pause)
    shutil.rmtree(path, ignore_errors=True)
    return not Path(path).exists()


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
    profile = tempfile.mkdtemp(prefix="fyers-login-")
    try:
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
            close_browser(proc)
        if code is None:
            sys.exit("timed out waiting for the Fyers redirect")
        status = fyers_auth.exchange(code)
        print(f"connected: {status.connected}", flush=True)
    finally:
        if not remove_profile(profile):
            print(f"warning: could not remove the browser profile {profile}", file=sys.stderr)
    return 0 if status.connected else 1


if __name__ == "__main__":
    sys.exit(main())

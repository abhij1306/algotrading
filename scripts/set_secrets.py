"""One-shot local form that writes owner secrets into the repo's `.env`.

    python scripts/set_secrets.py [--env PATH] [--port 8765]

Serves http://127.0.0.1:<port> on this machine only. The owner opens it in the desktop browser,
fills any of the fields, and submits. Each filled field replaces that key's existing line or
appends one; empty fields and every other line are left as they are. The file is written
atomically with mode 600, and the script exits after one save. Values are typed into the form at
runtime: the script holds none, and never prints or logs one, only which keys it saved.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
HOST = "127.0.0.1"

# key -> (label, pattern)
FIELDS = {
    "HAT_TELEGRAM_CHAT_ID": ("Telegram chat id", re.compile(r"-?\d{5,20}")),
    "KITE_API_KEY": ("Zerodha Kite API key", re.compile(r"[A-Za-z0-9]{6,64}")),
    "KITE_API_SECRET": ("Zerodha Kite API secret", re.compile(r"[A-Za-z0-9]{6,128}")),
    # Kotak Neo Trade API, for the read-only 5-minute history download (scripts/data/kotak_5m.py)
    "KOTAK_NEO_CONSUMER_KEY": (
        "Kotak Neo consumer key (NEO app > More > Trade API > application token)",
        re.compile(r"[A-Za-z0-9._~+/=:-]{8,1024}"),
    ),
    "KOTAK_NEO_MOBILE": ("Kotak Neo registered mobile, with +91", re.compile(r"\+?\d{10,15}")),
    "KOTAK_NEO_UCC": ("Kotak Neo UCC / client code (Profile)", re.compile(r"[A-Za-z0-9]{3,20}")),
    "KOTAK_NEO_MPIN": ("Kotak Neo MPIN", re.compile(r"\d{4,8}")),
    "KOTAK_NEO_TOTP_SECRET": (
        "Kotak Neo TOTP secret (base32 setup key shown at TOTP registration)",
        re.compile(r"[A-Za-z2-7= ]{16,128}"),
    ),
}
# Not secret; written with the Kite key so the login flow has it.
KITE_REDIRECT = ("KITE_REDIRECT_URI", "http://127.0.0.1:8000/api/hat/auth/zerodha/callback")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Set HAT secrets</title>
<style>body{{font:16px system-ui;max-width:30rem;margin:4rem auto;padding:0 1rem}}
label{{display:block;margin-top:1rem}}input{{font:inherit;width:100%;padding:.5rem;margin:.25rem 0}}
button{{font:inherit;padding:.5rem 1rem;margin-top:1rem}}
.msg{{margin-top:1rem}}</style></head><body>
<h2>HAT secrets</h2>
<p>Writes the filled fields into <code>{env}</code>. Leave a field empty to keep its current
value. Values are not shown or logged.</p>
<form method="post">{inputs}<button type="submit">Save</button></form>
<div class="msg">{msg}</div></body></html>"""


def updated_env(text: str, values: dict[str, str]) -> str:
    """The .env text with each key in values set: its first line replaced, later ones dropped,
    or a new line appended."""
    out, done = [], set()
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if key in values:
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
            continue
        out.append(line)
    out.extend(f"{k}={v}" for k, v in values.items() if k not in done)
    return "\n".join(out) + "\n"


def write_env(env_file: Path, values: dict[str, str]) -> None:
    text = env_file.read_text() if env_file.exists() else ""
    fd, tmp = tempfile.mkstemp(dir=env_file.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(updated_env(text, values))
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_file)
    except BaseException:
        os.unlink(tmp)
        raise


class Handler(BaseHTTPRequestHandler):
    env_file: Path = ENV_FILE

    def _page(self, msg: str, status: int = 200) -> None:
        inputs = "".join(
            f'<label>{html.escape(label)}<input name="{key}" type="password"'
            ' autocomplete="new-password"></label>'
            for key, (label, _) in FIELDS.items()
        )
        body = PAGE.format(env=html.escape(str(self.env_file)), inputs=inputs, msg=msg).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._page("")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        values: dict[str, str] = {}
        for key, (label, pattern) in FIELDS.items():
            value = (form.get(key) or [""])[0].strip()
            if not value:
                continue
            if not pattern.fullmatch(value):
                self._page(f"{html.escape(label)} does not look right; nothing was saved.", 400)
                return
            values[key] = value
        if not values:
            self._page("Nothing filled in.", 400)
            return
        if "KITE_API_KEY" in values:
            values.setdefault(*KITE_REDIRECT)
        write_env(self.env_file, values)
        self._page("Saved: " + ", ".join(values) + ". You can close this tab.")
        print("saved " + " ".join(values) + f" to {self.env_file}", flush=True)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args: object) -> None:  # no request logging
        pass


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="set_secrets.py", description=__doc__)
    p.add_argument("--env", type=Path, default=ENV_FILE, help="default: the repo's .env")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args(argv)
    Handler.env_file = args.env.resolve()
    print(f"Open {HOST}:{args.port} in the browser on this machine's desktop", flush=True)
    HTTPServer((HOST, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The Fyers access-token file: the only code that reads or writes it.

The file is JSON written by the login flow. Fields read back: ``client_id``, ``access_token``,
``fy_id`` and ``expires_at`` (UTC ISO 8601). A Fyers token is valid until the next midnight IST,
so ``expires_at`` is that midnight, computed in ``Asia/Kolkata``.

Writes are atomic: the JSON goes to a temp file in the same directory, which then replaces the
token file with ``os.replace``, so a reader never sees a half-written file. On POSIX the file is
0600. On Windows file permissions are ACLs, which this module does not change: the file keeps the
ACL inherited from its directory (by default only the user, SYSTEM and Administrators can read a
profile or project directory).

Nothing here logs or returns the token text except ``StoredToken.access_token`` itself, which is
kept out of ``repr``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.paths import FYERS_TOKEN_PATH
from app.utils.market_hours import IST

logger = logging.getLogger(__name__)

_POSIX_FILE_MODE = 0o600


class TokenStoreError(Exception):
    """The token file (or its directory) could not be read or written: an OS error.

    Bad content is not an error: an undecodable or malformed file reads as no token.
    """


def token_expiry(now: datetime) -> datetime:
    """The next midnight in IST after ``now``: when a token issued at ``now`` expires.

    ``now`` must be timezone-aware. The result is in IST.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ist_today = now.astimezone(IST).date()
    return datetime.combine(ist_today + timedelta(days=1), time(0), tzinfo=IST)


@dataclass(frozen=True)
class StoredToken:
    """A token read back from the file."""

    access_token: str = field(repr=False)
    client_id: str | None
    user_id: str | None
    expires_at: datetime | None
    """Aware UTC; None when the file has no expiry. An unparseable expiry reads as expired."""

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) > self.expires_at


class TokenStore:
    """Load, save and clear the token file at ``path``."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else FYERS_TOKEN_PATH

    def mtime_ns(self) -> int | None:
        """The file's modification time, or None when there is no file.

        Raises TokenStoreError when the file cannot be inspected (e.g. permission denied).
        """
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenStoreError("Fyers token file could not be inspected") from exc

    def load(self) -> StoredToken | None:
        """The stored token; None when there is no file or its content is not a token.

        Raises TokenStoreError when the file exists but cannot be read.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenStoreError("Fyers token file could not be read") from exc

        try:
            data = json.loads(raw)  # bad UTF-8 raises UnicodeDecodeError, a ValueError
        except ValueError:
            logger.warning("Fyers token file %s is not valid UTF-8 JSON; ignoring it", self.path)
            return None
        if not isinstance(data, dict):
            logger.warning("Fyers token file %s is not a JSON object; ignoring it", self.path)
            return None

        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            logger.warning("Fyers token file %s has no access token; ignoring it", self.path)
            return None

        return StoredToken(
            access_token=access_token,
            client_id=_optional_str(data.get("client_id")),
            user_id=_optional_str(data.get("fy_id")),
            expires_at=_parse_expiry(data.get("expires_at")),
        )

    def save(
        self, payload: Mapping[str, Any], *, client_id: str, now: datetime | None = None
    ) -> StoredToken:
        """Write a token-exchange reply plus ``client_id`` and the expiry, atomically.

        ``payload`` is the Fyers ``generate_token`` reply and must carry ``access_token``.
        Raises TokenStoreError when the file cannot be written; the old file is then untouched.
        """
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("payload has no access_token")

        expires_ist = token_expiry(now or datetime.now(UTC))
        expires_utc = expires_ist.astimezone(UTC)
        document = {
            **payload,
            "client_id": client_id,
            "expires_at": expires_utc.isoformat(),
            "expires_at_ist": expires_ist.isoformat(),
        }
        try:
            self._write_atomic(json.dumps(document, indent=4))
        except OSError as exc:
            raise TokenStoreError("Fyers token file could not be written") from exc

        return StoredToken(
            access_token=access_token,
            client_id=client_id,
            user_id=_optional_str(payload.get("fy_id")),
            expires_at=expires_utc,
        )

    def clear(self) -> bool:
        """Delete the token file. Returns whether there was one."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise TokenStoreError("Fyers token file could not be removed") from exc
        return True

    def _write_atomic(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            # mkstemp already creates the file 0600 on POSIX; chmod makes that explicit.
            if os.name == "posix":
                os.fchmod(fd, _POSIX_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_expiry(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        logger.warning("Fyers token file has a malformed expiry; treating the token as expired")
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


token_store = TokenStore()
"""The process-wide store for the default token file."""

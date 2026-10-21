"""The Telegram service's settings, read from the environment (08 §1, §4; HAT-TG-040).

Three variables, all required to start the service: `HAT_TELEGRAM_TOKEN` (the Bot API token),
`HAT_TELEGRAM_CHAT_ID` (the one allowlisted chat) and `HAT_APPROVAL_SECRET` (the HMAC key of the
approval buttons, at least 32 bytes). A missing or weak value refuses the start with a message
that names the variable, never its value; the token and the secret are kept out of `repr`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..errors import HatConfigError
from ..settings import process_env

__all__ = ["MIN_SECRET_BYTES", "TelegramSettings", "telegram_settings"]

TOKEN_VAR = "HAT_TELEGRAM_TOKEN"  # noqa: S105 (a variable name)
CHAT_VAR = "HAT_TELEGRAM_CHAT_ID"
SECRET_VAR = "HAT_APPROVAL_SECRET"  # noqa: S105 (a variable name)
# PLAN §7: the approval secret is 32 or more random bytes, so the HMAC cannot be brute-forced.
MIN_SECRET_BYTES = 32


@dataclass(frozen=True)
class TelegramSettings:
    token: str = field(repr=False)
    chat_id: int
    approval_secret: bytes = field(repr=False)


def _required(source: Mapping[str, str], name: str) -> str:
    raw = (source.get(name) or "").strip()
    if not raw:
        raise HatConfigError(f"{name} is not set")
    return raw


def telegram_settings(env: Mapping[str, str] | None = None) -> TelegramSettings:
    """Read and check the three variables; `env` defaults to the process environment. Raises
    `HatConfigError` for a missing variable, a chat id that is not an integer, or an approval
    secret shorter than `MIN_SECRET_BYTES` (HAT-TG-040: the service refuses to start)."""
    source = process_env() if env is None else env
    token = _required(source, TOKEN_VAR)
    raw_chat = _required(source, CHAT_VAR)
    try:
        chat_id = int(raw_chat)
    except ValueError:
        raise HatConfigError(f"{CHAT_VAR} is not an integer") from None
    secret = _required(source, SECRET_VAR).encode()
    if len(secret) < MIN_SECRET_BYTES:
        raise HatConfigError(f"{SECRET_VAR} is shorter than {MIN_SECRET_BYTES} bytes")
    return TelegramSettings(token=token, chat_id=chat_id, approval_secret=secret)

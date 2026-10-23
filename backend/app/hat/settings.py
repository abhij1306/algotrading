"""HAT settings from environment variables (HAT-OPS-001, HAT-OVR-007, HAT-OVR-052).

`HAT_ENABLED` defaults to false, so a developer machine or a test run never starts a HAT task.
`HAT_EXDATE_GTT_CHECK` (PR 47) defaults to false: the 08:40 `gtt.ex_date` check is registered only
once the O2(b) live check confirms how the broker treats a GTT on an ex-date (HAT-EXEC-145).
`HAT_BROKER_CALLS_PER_SECOND`, `_PER_MINUTE` and `_PER_DAY` (PR 50, HAT-BRK-082) are the broker
call budget: defaults 8, 180 and 5,000 (the Standard plan counts every call); `_PER_DAY` 0 or
`off` is no day cap, and an unreadable value is the default.
`HAT_MPP_RESEND` (PR 48) defaults to false: a market sale resting unfilled under market price
protection is cancelled and sent again fresh only once the O2(c) live check confirms how the
broker shows such a sale; `HAT_MPP_PAGE_AFTER` is the misses before the owner is paged (default 3).
`HAT_EXCHANGE_DAY_RANGE` (PR 39) defaults to false: a symbol first seen after the open is seeded
from the day high and low its tick carries only once a recorded tick confirms the field names
(HAT-DATA-114). `HAT_SYMBOL_SILENT_SECONDS` is the silence after which a watched symbol is
reported and subscribed again (default 120; HAT-DATA-116).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.paths import ENV_FILE, HAT_DATA_ROOT
from app.utils.env_loader import TRUE_VALUES, load_dotenv

from .errors import HatConfigError

# MVP is single user (decision T1): one fixed tenant id unless HAT_USER_ID overrides it.
DEFAULT_USER_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")


@dataclass(frozen=True)
class HatSettings:
    enabled: bool
    user_id: uuid.UUID
    data_root: Path
    exdate_gtt_check: bool = False
    broker_calls_per_second: int = 8
    broker_calls_per_minute: int = 180
    broker_calls_per_day: int | None = 5000
    mpp_resend: bool = False
    mpp_page_after: int = 3
    exchange_day_range: bool = False
    symbol_silent_seconds: int = 120
    entry_timeout_minutes: int = 60


def _is_true(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() in TRUE_VALUES


def _calls(raw: str | None, default: int | None, *, allow_off: bool = False) -> int | None:
    """A positive whole number of broker calls; `0` or `off` is no cap where allowed, anything
    else unreadable is the default (a typo never stops every call, nor fails a start)."""
    text = (raw or "").strip().lower()
    if allow_off and text in {"0", "off", "none"}:
        return None
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value > 0 else default


def load_settings(env: Mapping[str, str] | None = None) -> HatSettings:
    """Read the HAT settings; `env` defaults to `os.environ`. A bad `HAT_USER_ID` raises."""
    source = os.environ if env is None else env
    raw_user = (source.get("HAT_USER_ID") or "").strip()
    try:
        user_id = uuid.UUID(raw_user) if raw_user else DEFAULT_USER_ID
    except ValueError as exc:
        raise HatConfigError("HAT_USER_ID is not a valid uuid") from exc
    raw_root = (source.get("HAT_DATA_ROOT") or "").strip()
    return HatSettings(
        enabled=_is_true(source.get("HAT_ENABLED")),
        user_id=user_id,
        data_root=Path(raw_root) if raw_root else HAT_DATA_ROOT,
        exdate_gtt_check=_is_true(source.get("HAT_EXDATE_GTT_CHECK")),
        broker_calls_per_second=_calls(source.get("HAT_BROKER_CALLS_PER_SECOND"), 8) or 8,
        broker_calls_per_minute=_calls(source.get("HAT_BROKER_CALLS_PER_MINUTE"), 180) or 180,
        broker_calls_per_day=_calls(source.get("HAT_BROKER_CALLS_PER_DAY"), 5000, allow_off=True),
        mpp_resend=_is_true(source.get("HAT_MPP_RESEND")),
        mpp_page_after=_calls(source.get("HAT_MPP_PAGE_AFTER"), 3) or 3,
        exchange_day_range=_is_true(source.get("HAT_EXCHANGE_DAY_RANGE")),
        symbol_silent_seconds=_calls(source.get("HAT_SYMBOL_SILENT_SECONDS"), 120) or 120,
        entry_timeout_minutes=_calls(source.get("HAT_ENTRY_TIMEOUT_MINUTES"), 60) or 60,
    )


def operator_settings() -> HatSettings:
    """The settings of a command-line entry point (HAT-DATA-080): `.env` is loaded first, so a
    `HAT_DATA_ROOT` set only there counts (a variable already in the environment wins).
    `app.paths.HAT_DATA_ROOT` is read when `app.paths` is imported, before anything loads
    `.env`, so a command passes this `data_root` to its stores instead of their default."""
    load_dotenv(str(ENV_FILE))
    return load_settings()


def process_env() -> Mapping[str, str]:
    """The process environment, for operator entry points that read other `HAT_*` variables."""
    return os.environ


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Only the `HAT_ENABLED` switch, so an unrelated bad setting cannot fail a disabled start."""
    source = os.environ if env is None else env
    return _is_true(source.get("HAT_ENABLED"))


def require_postgres(dialect_name: str) -> None:
    """Fail closed unless the database is PostgreSQL (HAT-SCH-105): the triggers and CHECKs
    that protect versions, stops and the event log exist only there."""
    if dialect_name != "postgresql":
        raise HatConfigError("HAT requires PostgreSQL; refusing to start on another database")

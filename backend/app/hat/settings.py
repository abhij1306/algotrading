"""HAT settings from environment variables (HAT-OPS-001, HAT-OVR-007, HAT-OVR-052).

`HAT_ENABLED` defaults to false, so a developer machine or a test run never starts a HAT task.
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


def _is_true(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() in TRUE_VALUES


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

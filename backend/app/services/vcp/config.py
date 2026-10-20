"""VCP strategy settings stored as key/value rows in `system_config`."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import SystemConfig

DEFAULT_UNIVERSE = "NIFTY500"


def get_config(db: Session, key: str, default: str) -> str:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return row.value if row else default


def set_config(db: Session, key: str, value: str) -> None:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if row is None:
        row = SystemConfig(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()

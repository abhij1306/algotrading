"""What every verify check reads: the data root's store, a read-only database session, the
window, the reference folders the operator names, and whether a missing public reference file
may be downloaded."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..store import DataStore

# public reference files a check downloaded: a store of its own (in the data record), never raw/
REFERENCE_DIR = Path("verify_reference")


@dataclass
class Context:
    store: DataStore
    session: Session
    start: date
    end: date
    ca_data: Path | None = None  # NSE corporate-action pulls (rights, demergers, splits, results)
    notices: Path | None = None  # the parsed NIFTY 500 notices and archived constituent lists
    kotak: Path | None = None  # the Kotak raw 5-minute files
    fetch: bool = False  # download a missing public reference file
    log: list[str] = field(default_factory=list)
    unfetched: Counter[str] = field(default_factory=Counter)  # reference files not to be had

    @property
    def root(self) -> Path:
        return self.store.root

    def reference_dir(self, name: str) -> Path:
        return self.root / REFERENCE_DIR / name

    def rows(self, sql: str, **params: Any) -> list[Any]:  # noqa: ANN401 - plain rows
        """A read-only SQL query's rows on the HAT database."""
        from ...models import HatMarket  # noqa: PLC0415 - the bind only

        return list(
            self.session.execute(text(sql), params, bind_arguments={"mapper": HatMarket}).all()
        )

    def note(self, line: str) -> None:
        self.log.append(line)


def in_window(days: Iterator[date] | list[date], start: date, end: date) -> list[date]:
    return [d for d in days if start <= d <= end]

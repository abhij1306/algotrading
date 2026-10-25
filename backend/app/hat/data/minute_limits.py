"""The 1-minute history download's limits (`HistoryLimits`: the Fyers plan and the loader's
policy) and its progress kept across runs (`Progress`: the day's calls, what each symbol was
loaded for, the depth the plan serves). `minute_history` runs the download."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..broker_limits import PLAN_CALLS_PER_DAY
from ..errors import HatError
from ..time import as_utc
from .store import atomic_replace

if TYPE_CHECKING:
    from ..settings import HatSettings

CHUNK_DAYS = 100  # the most days of 1-minute candles one history request may span
LEGACY_PLAN_DAYS = 365  # the Standard plan's depth, which a progress file without one was under


class MinuteHistoryError(HatError):
    """The history could not be loaded; `code` is a fixed word and carries no broker text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HistoryLimits:
    """The Fyers plan's history limits and the loader's policy (audit D3, D2, D7), from the
    settings (`from_settings`) with command-line overrides: days per call, calls per minute and
    per IST day, how far back the plan serves intraday candles, the most candles one response
    may hold (by default 400 a day of the call, over the session's 375 minutes; a larger answer
    is refused, so one symbol never holds more than one response of candles at a time), the
    share of a symbol's candles (percent) and the run of them in a row that may be rejected
    before the symbol fails, and the symbols (or bars) written together."""

    chunk_days: int = CHUNK_DAYS
    per_minute: int = 50
    per_day: int = PLAN_CALLS_PER_DAY
    plan_days: int = LEGACY_PLAN_DAYS
    max_candles: int | None = None  # None: 400 x chunk_days
    max_clamps: int = 3  # plan-limit refusals one run tolerates before it stops
    failures_in_row: int = 3  # consecutive failed symbols that stop the run (a dead session)
    max_reject_pct: Decimal = Decimal("0.5")
    max_reject_run: int = 12
    write_batch: int = 25
    flush_bars: int = 3_000_000  # a batch is written early once it holds this many bars

    def __post_init__(self) -> None:
        values = (
            self.chunk_days,
            self.per_minute,
            self.plan_days,
            self.candle_cap,
            self.max_reject_run,
            self.write_batch,
            self.flush_bars,
        )
        if (
            min(values) < 1
            or self.per_day < 0  # 0 is legal: nothing left after the reserve, the run stops
            or self.max_clamps < 0
            or self.failures_in_row < 1
            or self.max_reject_pct < 0
        ):
            raise MinuteHistoryError("history_bad_limits")

    @property
    def candle_cap(self) -> int:
        return self.max_candles if self.max_candles is not None else 400 * self.chunk_days

    @classmethod
    def from_settings(cls, settings: HatSettings, **overrides: int | None) -> HistoryLimits:
        """The settings' limits (the day cap keeps the reserve for live trading); an override
        that is None keeps the setting."""
        values: dict[str, Any] = {
            "chunk_days": settings.history_chunk_days,
            "per_minute": settings.history_per_minute,
            "per_day": settings.history_calls_per_day,
            "plan_days": settings.history_plan_days,
            "max_reject_pct": settings.history_max_reject_pct,
            "max_reject_run": settings.history_max_reject_run,
            "write_batch": settings.history_write_batch,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


@dataclass
class Progress:
    """What a download has done, kept across runs: history calls per IST day (the daily budget
    holds across restarts), the period each symbol was loaded for, the earliest date the plan
    serves once a call was refused for it, and the plan depth (`plan_days`) that date and the
    last run's period were learned under (a file from before it was recorded was written under
    the Standard plan's 365 days). `path` None keeps it in memory only."""

    path: Path | None = None
    calls: dict[str, int] = field(default_factory=dict)
    done: dict[str, tuple[date, date]] = field(default_factory=dict)
    available_from: date | None = None
    window: tuple[date, date] | None = None  # the last run's period, which a resume continues
    recent: list[datetime] = field(default_factory=list)  # the last minute's call instants
    failed: dict[str, str] = field(default_factory=dict)  # symbols whose last attempt failed
    plan_days: int = LEGACY_PLAN_DAYS

    @classmethod
    def load(cls, path: Path) -> Progress:
        """The progress stored at `path`; empty when there is none. An unreadable file raises
        `history_bad_progress`: guessing would lose the day's call count."""
        if not path.is_file():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            calls = {str(day): int(n) for day, n in raw.get("calls", {}).items()}
            done = {
                str(symbol): (date.fromisoformat(span[0]), date.fromisoformat(span[1]))
                for symbol, span in raw.get("done", {}).items()
            }
            floor = raw.get("available_from")
            window = raw.get("window")
            return cls(
                path,
                calls,
                done,
                None if floor is None else date.fromisoformat(floor),
                None
                if window is None
                else (date.fromisoformat(window[0]), date.fromisoformat(window[1])),
                [as_utc(datetime.fromisoformat(at)) for at in raw.get("recent", [])],
                {str(symbol): str(code) for symbol, code in raw.get("failed", {}).items()},
                int(raw.get("plan_days", LEGACY_PLAN_DAYS)),
            )
        except (OSError, ValueError, TypeError, AttributeError, IndexError) as exc:
            raise MinuteHistoryError("history_bad_progress") from exc

    def save(self) -> None:
        """Write the progress whole through a temporary file and an atomic replace."""
        if self.path is None:
            return
        body = {
            "calls": dict(sorted(self.calls.items())),
            "done": {s: [a.isoformat(), b.isoformat()] for s, (a, b) in sorted(self.done.items())},
            "available_from": None
            if self.available_from is None
            else self.available_from.isoformat(),
            "window": None if self.window is None else [d.isoformat() for d in self.window],
            "recent": [as_utc(at).isoformat() for at in self.recent],
            "failed": dict(sorted(self.failed.items())),
            "plan_days": self.plan_days,
        }
        text = json.dumps(body)
        atomic_replace(self.path, lambda tmp: tmp.write_text(text, encoding="utf-8"))

    def covers(self, symbol: str, start: date, end: date) -> bool:
        span = self.done.get(symbol)
        return span is not None and span[0] <= start and span[1] >= end

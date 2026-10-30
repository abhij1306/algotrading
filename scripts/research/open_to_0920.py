"""Measure the move from the day's open to 09:20 on the five-minute years: the estimate a
backtest uses for an order at 09:20 on a day without five-minute bars (`engine/fill_0920.py`
`OPEN_TO_0920`, decisions.md "Orders at 09:20").

    venv/bin/python scripts/research/open_to_0920.py [--universe "NIFTY 500"] \
        [--from 2021-10-26] [--to 2024-12-31] [--min-adv-cr 10] [--min-price 50]

For every session in the window with a stored `bars_5m` day, every point-in-time member of the
universe that is liquid as a rotation buys it (the seed's universe stage: series EQ or BE, the 20
sessions before the day averaging at least `--min-adv-cr` crore traded, close times volume where
no traded value is stored, and the previous close at least `--min-price`) and whose five-minute session a backtest admits (`intraday.session_admitted`: it fits
its daily bar, lacks no refused bucket, lies in no known coverage gap) gives one move:
the price at 09:20 (`fill_0920.price_0920`, the 09:15 bar's close) over the day's raw open, less
1. It prints one JSON report: the count, the median, quartiles and mean of the move, the
buy-side median (the move: a buy pays it) and the sell-side median (minus the move: a sale loses
it), over all member-days and over rebalance sessions only (the first session of each month, when
a monthly rotation trades), and the constants to set (`suggested`: each side's median, never
favourable, so at least 0). Read only: the database in a read-only transaction, the store's
files. Membership is by the ids the universe stores (a chain's older ISIN is not joined).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

CRORE = Decimal(10_000_000)
ADV_SESSIONS = 20
SERIES_IN = frozenset({"EQ", "BE"})  # the seed's universe stage (`series_in`): what it can buy


def turnover(
    traded_value: Decimal | None, close: Decimal | None, volume: int | None
) -> Decimal | None:
    """A session's traded value as the universe stage reads it (`indicators.traded_values`):
    the reported one, else close times volume; None without either."""
    if traded_value is not None:
        return traded_value
    if close is not None and volume is not None:
        return close * volume
    return None


def adverse_medians(moves: Sequence[float]) -> dict[str, Any]:
    """The summary of `moves` (09:20 price over the open, less 1): count, median, quartiles,
    mean, the buy side's median (the move) and the sell side's (minus the move), and the
    suggested constants (each side's median, at least 0: an estimate never helps the order)."""
    if not moves:
        return {"count": 0}
    ordered = sorted(moves)
    q1, median, q3 = statistics.quantiles(ordered, n=4) if len(ordered) > 1 else (ordered[0],) * 3
    buy = statistics.median(ordered)
    sell = statistics.median(-m for m in ordered)
    return {
        "count": len(ordered),
        "median": round(median, 6),
        "q1": round(q1, 6),
        "q3": round(q3, 6),
        "mean": round(statistics.fmean(ordered), 6),
        "median_abs": round(statistics.median(abs(m) for m in ordered), 6),
        "buy_side_median": round(buy, 6),
        "sell_side_median": round(sell, 6),
        "suggested": {"buy": round(max(buy, 0.0), 4), "sell": round(max(sell, 0.0), 4)},
    }


def rebalance_sessions(days: Sequence[date]) -> set[date]:
    """The first session of each month among `days` (sorted): when a monthly rotation trades."""
    out: set[date] = set()
    seen: set[tuple[int, int]] = set()
    for day in days:
        if (day.year, day.month) not in seen:
            seen.add((day.year, day.month))
            out.add(day)
    return out


class Liquidity:
    """Each instrument's last `ADV_SESSIONS` traded values and its last close, fed one session
    at a time: `liquid` judges a name on the sessions before the one about to be fed."""

    def __init__(self, min_value: Decimal, min_price: Decimal) -> None:
        self.min_value, self.min_price = min_value, min_price
        self.values: dict[str, deque[Decimal]] = defaultdict(lambda: deque(maxlen=ADV_SESSIONS))
        self.closes: dict[str, Decimal] = {}

    def liquid(self, name: str) -> bool:
        values = self.values.get(name)
        close = self.closes.get(name)
        if values is None or len(values) < ADV_SESSIONS or close is None:
            return False
        return sum(values, Decimal(0)) / ADV_SESSIONS >= self.min_value and close >= self.min_price

    def feed(self, rows: Iterable[tuple[str, Decimal, Decimal]]) -> None:
        """One session's (instrument, close, traded value)."""
        for name, close, value in rows:
            self.values[name].append(value)
            self.closes[name] = close


def moves_of(
    day: date,
    sessions: Mapping[str, Sequence[Any]],
    opens: Mapping[str, Decimal],
    names: Iterable[str],
    admits: Callable[[str, Sequence[Any], Decimal], bool],
) -> list[float]:
    """The moves of `names` on `day`: the 09:20 price of each one whose five-minute session is
    admitted as a backtest admits it (`admits`: `intraday.session_admitted`, through
    `Admission`) over its open, less 1."""
    from app.hat.engine.fill_0920 import price_0920

    out = []
    for name in names:
        bars, open_ = sessions.get(name), opens.get(name)
        if not bars or open_ is None or open_ <= 0 or not admits(name, bars, open_):
            continue
        found = price_0920(day, bars)
        if found is not None:
            out.append(float(found[0] / open_ - 1))
    return out


def _decimal(value: object) -> Decimal | None:
    from app.hat.money import exact

    return None if value is None else exact(value, floats=True)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the data
    from sqlalchemy import select, text

    from app.database import db_session_scope
    from app.hat.backtest.intraday import Admission, session_bars
    from app.hat.data.bars_store import BarStore
    from app.hat.data.store import DATASET_BARS, DataStore
    from app.hat.engine.portfolio.universe import members_on
    from app.hat.engine.portfolio.universe_service import membership_rows
    from app.hat.models import HatEvent, HatUniverse
    from app.hat.settings import operator_settings

    p = argparse.ArgumentParser(prog="open_to_0920.py", description=__doc__)
    p.add_argument("--universe", default="NIFTY 500")
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2021, 10, 26))
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=date(2024, 12, 31))
    p.add_argument("--min-adv-cr", type=Decimal, default=Decimal(10))
    p.add_argument("--min-price", type=Decimal, default=Decimal(50))
    args = p.parse_args(argv)
    settings = operator_settings()
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    five = BarStore(store.root)
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        universe = session.scalars(
            select(HatUniverse).where(HatUniverse.name == args.universe)
        ).first()
        if universe is None:
            sys.exit(f"no universe named {args.universe!r}")
        rows = membership_rows(session, universe.id)
    days = sorted(d for d in store.days(DATASET_BARS) if d <= args.end)
    liquidity = Liquidity(args.min_adv_cr * CRORE, args.min_price)
    rebalance = rebalance_sessions([d for d in days if d >= args.start])
    admission = Admission.read(five)
    every: list[float] = []
    first_of_month: list[float] = []
    sessions_read = 0
    for day in days:
        daily = store.read_bars(day)
        ids = [str(i) for i in daily["instrument_id"].to_list()]
        isins = dict(zip(ids, daily["isin"].to_list(), strict=True))
        series = dict(zip(ids, daily["series"].to_list(), strict=True))
        opens = {
            i: o for i, v in zip(ids, daily["open"].to_list(), strict=True) if (o := _decimal(v))
        }
        if day >= args.start and five.path(day).is_file():
            members = {str(m) for m in members_on(rows, day)}
            wanted = sorted(
                n
                for n in members & set(opens)
                if series.get(n) in SERIES_IN and liquidity.liquid(n)
            )
            sessions = session_bars(five, day, dict.fromkeys(wanted, Decimal(1)))
            found = moves_of(
                day,
                sessions,
                opens,
                wanted,
                lambda n, bars, o, day=day, isins=isins: admission.admits(
                    n, day, bars, o, isins.get(n)
                ),
            )
            every.extend(found)
            sessions_read += 1
            if day in rebalance:
                first_of_month.extend(found)
        liquidity.feed(
            (i, c, v)
            for i, close, value, volume in zip(
                ids,
                daily["close"].to_list(),
                daily["traded_value"].to_list(),
                daily["volume"].to_list(),
                strict=True,
            )
            if (c := _decimal(close)) is not None
            and (v := turnover(_decimal(value), c, volume)) is not None
        )
    report = {
        "universe": args.universe,
        "window": [args.start.isoformat(), args.end.isoformat()],
        "liquidity": {"min_adv_cr": str(args.min_adv_cr), "min_price": str(args.min_price)},
        "sessions_with_5m": sessions_read,
        "all_member_days": adverse_medians(every),
        "rebalance_sessions": adverse_medians(first_of_month),
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

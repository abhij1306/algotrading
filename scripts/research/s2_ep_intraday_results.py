"""The S2 EP intraday-results report (plan B7, research only, no trigger): when results filings
become public, and how a stock moved after a filing made during market hours.

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \
        venv/bin/python scripts/research/s2_ep_intraday_results.py \
        --strategy-version <id> --universe <name> [--from 2022-01-01] [--to 2024-12-31]

It loads the market of the window as a backtest run of that version and universe does
(`load_market` with the results store), so the instruments, the price basis and the filings
are a run's. A filing is the first row of an (instrument, quarter) public in the window (a
later revision is not a new announcement), with the growth of its quarter as known then.

- Counts filings by when they became public (IST): `pre_open` (before 09:15), `market`
  (09:15 to 15:30) and `post_close` (from 15:30).
- For a market-hours filing on a session with stored 5-minute bars: the price at the filing is
  the open of the first bar starting at or after it (the first price that could react), and the
  move runs from it to the session's close and to the close 5 and 20 sessions later, split by
  the `earnings_catalyst` growth rule at its defaults (pass, fail, undefined).

Never reads after `--to`, which may not pass the in-sample end (2024-12-31): no hold-out read.
Writes `reports/s2-ep/b7/` at the repo root (gitignored): `filings.csv` and `report.md`.
Reads the database only.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from sqlalchemy import text  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.backtest.data import MarketView, load_market, symbol_on  # noqa: E402
from app.hat.backtest.intraday import session_bars  # noqa: E402
from app.hat.backtest.runs import RunRequest, _sleeve_of  # noqa: E402
from app.hat.data.bars import Bar  # noqa: E402
from app.hat.data.store import DataStore  # noqa: E402
from app.hat.engine.criteria.live import (  # noqa: E402
    EarningsCatalystParams,
    ResultEvent,
    growth_passes,
)
from app.hat.engine.strategy.config import parse_config  # noqa: E402
from app.hat.models import HatEvent  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402
from app.hat.time import IST  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

IN_SAMPLE_END = date(2024, 12, 31)
OPEN, CLOSE = time(9, 15), time(15, 30)
PRE_OPEN, MARKET, POST_CLOSE = "pre_open", "market", "post_close"
HORIZONS = (("close", 0), ("5d", 5), ("20d", 20))
GROUPS = ("pass", "fail", "undefined")


def public_bucket(public_at: datetime) -> str:
    """When a filing became public, by its IST time of day."""
    at = public_at.astimezone(IST).time()
    if at < OPEN:
        return PRE_OPEN
    return MARKET if at < CLOSE else POST_CLOSE


def first_filings(events: Iterable[ResultEvent]) -> list[ResultEvent]:
    """The first public row of each quarter (`period_end`), oldest first."""
    first: dict[date, ResultEvent] = {}
    for event in sorted(events, key=lambda e: e.public_at):
        first.setdefault(event.period_end, event)
    return sorted(first.values(), key=lambda e: e.public_at)


def growth_group(event: ResultEvent, params: EarningsCatalystParams) -> str:
    passed = growth_passes(event, params)
    return "undefined" if passed is None else "pass" if passed else "fail"


def price_after(bars: Sequence[Bar], at: datetime) -> Decimal | None:
    """The open of the first bar starting at or after `at` (bars in time order)."""
    return next((b.open for b in bars if b.start >= at), None)


def move_pct(start: Decimal | None, end: Decimal | None) -> float | None:
    if start is None or end is None or start <= 0:
        return None
    return float((end / start - 1) * 100)


def later_day(days: Sequence[date], day: date, sessions: int) -> date | None:
    """The session `sessions` after `day` among `days` (sorted), None past their end or for a
    day not among them."""
    try:
        at = days.index(day) + sessions
    except ValueError:
        return None
    return days[at] if at < len(days) else None


def summary(values: Sequence[float]) -> dict[str, float | int | None]:
    """n, mean, median, quartiles and the share above zero (%) of `values`."""
    if not values:
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None, "up_pct": None}
    quartiles = statistics.quantiles(values, n=4) if len(values) > 1 else [values[0]] * 3
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "p25": round(quartiles[0], 2),
        "p75": round(quartiles[2], 2),
        "up_pct": round(100 * sum(1 for v in values if v > 0) / len(values), 1),
    }


@dataclass(frozen=True)
class FilingRow:
    instrument_id: str
    symbol: str
    public_at: str
    period_end: date
    bucket: str
    growth: str
    sales_yoy_pct: float | None
    eps_yoy_pct: float | None
    price_at: float | None = None
    move_close_pct: float | None = None
    move_5d_pct: float | None = None
    move_20d_pct: float | None = None


def report_md(rows: Sequence[FilingRow], start: date, end: date) -> str:
    """The report: counts by bucket, then the market-hours moves by growth group."""
    counts = Counter(r.bucket for r in rows)
    lines = [
        f"# S2 EP intraday results (B7), {start}..{end}",
        "",
        f"Filings: {len(rows)}",
        "",
        "| public_at (IST) | filings |",
        "| --- | ---: |",
        *(f"| {b} | {counts.get(b, 0)} |" for b in (PRE_OPEN, MARKET, POST_CLOSE)),
        "",
        "Market-hours filings: move (%) from the first 5-minute open at or after public_at.",
        "",
        "| growth | horizon | n | mean | median | p25 | p75 | up % |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    market = [r for r in rows if r.bucket == MARKET]
    for group in GROUPS:
        for name, _ in HORIZONS:
            values = [
                v
                for r in market
                if r.growth == group and (v := getattr(r, f"move_{name}_pct")) is not None
            ]
            s = summary(values)
            lines.append(
                f"| {group} | {name} | {s['n']} | {s['mean']} | {s['median']} | {s['p25']} |"
                f" {s['p75']} | {s['up_pct']} |"
            )
    priced = sum(1 for r in market if r.price_at is not None)
    lines += ["", f"Market-hours filings with a 5-minute price at public_at: {priced}"]
    return "\n".join(lines) + "\n"


def _pct(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value * 100), 2)


def _rows(
    view: MarketView,
    results: Mapping[str, Sequence[ResultEvent]],
    intraday_of: Callable[[date, set[str]], Mapping[str, Sequence[Bar]]],
    symbol: Callable[[str, date], str],
    start: date,
    end: date,
) -> list[FilingRow]:
    params = EarningsCatalystParams()
    days = list(view.days)
    rows: list[FilingRow] = []
    by_day: dict[date, list[tuple[str, ResultEvent]]] = defaultdict(list)
    plain: list[tuple[str, ResultEvent]] = []
    for instrument_id, events in results.items():
        for event in first_filings(events):
            day = event.public_at.astimezone(IST).date()
            if not start <= day <= end:
                continue
            if public_bucket(event.public_at) == MARKET and day in days:
                by_day[day].append((instrument_id, event))
            else:
                plain.append((instrument_id, event))
    for instrument_id, event in plain:
        day = event.public_at.astimezone(IST).date()
        rows.append(_row(instrument_id, event, symbol(instrument_id, day), params))
    for day in sorted(by_day):
        bars = intraday_of(day, {i for i, _ in by_day[day]})
        for instrument_id, event in by_day[day]:
            row = _row(instrument_id, event, symbol(instrument_id, day), params)
            session = bars.get(instrument_id, ())
            price = price_after(session, event.public_at)
            moves = {}
            for name, sessions in HORIZONS:
                later = day if sessions == 0 else later_day(days, day, sessions)
                close = None
                if sessions == 0 and session:
                    close = session[-1].close
                elif later is not None and (bar := view.bar(instrument_id, later)) is not None:
                    close = bar.close
                moves[f"move_{name}_pct"] = move_pct(price, close)
            rows.append(
                FilingRow(
                    **{**asdict(row), "price_at": None if price is None else float(price), **moves}
                )
            )
    return sorted(rows, key=lambda r: r.public_at)


def _row(
    instrument_id: str, event: ResultEvent, symbol: str, params: EarningsCatalystParams
) -> FilingRow:
    return FilingRow(
        instrument_id,
        symbol,
        event.public_at.astimezone(IST).isoformat(),
        event.period_end,
        public_bucket(event.public_at),
        growth_group(event, params),
        _pct(event.sales_yoy),
        _pct(event.eps_yoy),
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the database
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy-version", type=uuid.UUID, required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2022, 1, 1))
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=IN_SAMPLE_END)
    args = parser.parse_args(argv)
    if args.end > IN_SAMPLE_END:
        raise SystemExit(f"--to {args.end} is after the in-sample end {IN_SAMPLE_END}")
    settings = operator_settings()
    out_dir = PROJECT_ROOT / "reports" / "s2-ep" / "b7"
    out_dir.mkdir(parents=True, exist_ok=True)
    request = RunRequest(
        args.start, args.end, universe=args.universe, version_id=args.strategy_version
    )
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        version, universe, _ = _sleeve_of(session, request)
        config = parse_config(version.config)
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
        data, _ = load_market(
            store,
            session,
            universe_version_id=universe.id,
            index=config.regime.index,
            start=args.start,
            end=args.end,
            results=True,
        )
    if max(data.days) > args.end:
        raise SystemExit("loaded data after --to")
    if data.intraday is None:
        raise SystemExit("no bars_5m store under the data root")
    view = MarketView(data, config.regime.index)
    intraday = data.intraday

    def intraday_of(day: date, instruments: set[str]) -> dict[str, tuple[Bar, ...]]:
        scales = {
            i: s for i in instruments if (s := view.price_scale(i, day)) is not None and s > 0
        }
        return session_bars(intraday, day, scales)

    rows = _rows(
        view,
        data.results,
        intraday_of,
        lambda i, d: symbol_on(data, i, d),
        args.start,
        args.end,
    )
    with (out_dir / "filings.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FilingRow.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(r) for r in rows)
    report = report_md(rows, args.start, args.end)
    (out_dir / "report.md").write_text(report)
    print(report)
    print(f"wrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

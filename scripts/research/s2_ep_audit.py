"""The S2 EP audit (plan C4) of one in-sample run with the `episodic_pivot` playbook: sampled
entries and near-miss rejections, each with its EP log and two charts.

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \
        venv/bin/python scripts/research/s2_ep_audit.py --run-id <V7 run id> [--seed N]

- entries: `min(20, n)` of the run's EP trades (`playbook` episodic_pivot), drawn with `--seed`;
- near misses: `min(10, n)` of the run's `ep_candidates.csv` rows that passed the gap and the
  setup and failed a trigger gate or an entry filter (`near_miss`), drawn with the same seed.

The seed is printed. Each case lists symbol, date, the results filing's public_at, sales_yoy,
eps_yoy, gap %, the day volume at the trigger over ADV20, the trigger time and, for an entry,
entry, stop, exit and R (the sum of its legs' `r_net`). Each case gets a 5-minute chart of its
day (the opening-range high, and the entry and stop of an entry) and a daily chart with
SMA10/20/50, from the market loaded over the run's period only (`load_market`, in-sample, as the
run read it; the snapshot must match). Writes `reports/s2-ep/audit/` at the repo root
(gitignored): `cases.csv`, `audit.md` and `charts/*.png`. Needs matplotlib, which is not a
backend requirement. Reads the database only.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.hat.backtest import ep_log  # noqa: E402
from app.hat.data.bars import Bar  # noqa: E402

IN_SAMPLE_END = date(2024, 12, 31)
ENTRY_SAMPLE, NEAR_SAMPLE = 20, 10
DEFAULT_SEED = 20261028
EP = "episodic_pivot"
SMAS = (10, 20, 50)
DAILY_BEFORE, DAILY_AFTER = 120, 20  # sessions drawn before the case day and after its exit
CASE_COLUMNS = (
    "kind",
    "symbol",
    "date",
    "results_public_at",
    "sales_yoy_pct",
    "eps_yoy_pct",
    "gap_pct",
    "vol_vs_adv20",
    "trigger_time",
    "entry",
    "stop",
    "exit",
    "exit_date",
    "r",
    "failed_gate",
    "reason",
    "instrument_id",
)


@dataclass(frozen=True)
class Case:
    kind: str  # entry | near_miss
    symbol: str
    date: date
    results_public_at: str
    sales_yoy_pct: str
    eps_yoy_pct: str
    gap_pct: str
    vol_vs_adv20: str
    trigger_time: str
    entry: float | None = None
    stop: float | None = None
    exit: float | None = None
    exit_date: date | None = None
    r: float | None = None
    failed_gate: str = ""
    reason: str = ""
    instrument_id: str = ""


def _ep(row: Mapping[str, str], key: str) -> str:
    return row.get(key) or ""


def entry_cases(rows: Iterable[Mapping[str, str]]) -> list[Case]:
    """One case per EP trade of trades.csv rows (its legs joined): the entry and stop of its
    first leg, the exit of its last, R the sum of the legs' `r_net`; by trade id."""
    legs: dict[int, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("playbook") == EP:
            legs[int(row["trade_id"])].append(row)
    out = []
    for _, group in sorted(legs.items()):
        group.sort(key=lambda r: int(r["leg"]))
        first, last = group[0], group[-1]
        out.append(
            Case(
                "entry",
                first["symbol"],
                date.fromisoformat(first["entry_date"]),
                *(_ep(first, k) for k in CASE_COLUMNS[3:9]),
                float(first["entry_price"]),
                float(first["initial_stop"]),
                float(last["exit_price"]),
                date.fromisoformat(last["exit_date"]),
                round(sum(float(r["r_net"]) for r in group), 3),
                instrument_id=first["instrument_id"],
            )
        )
    return out


def near_miss(row: Mapping[str, str], gates: Sequence[str], setup: Iterable[str]) -> bool:
    """A candidate that passed the gap and the hard setup and failed a later gate before
    `filled` (a trigger gate or an entry filter; a refusal at the fill is not the detector's)."""
    failed = row.get("failed_gate") or ""
    if failed not in gates or failed == ep_log.FILLED:
        return False
    passed = [gates.index(g) for g in (ep_log.GAP_UP, *setup) if g in gates]
    return gates.index(failed) > max(passed, default=0)


def near_cases(
    rows: Iterable[Mapping[str, str]], gates: Sequence[str], setup: Sequence[str]
) -> list[Case]:
    return [
        Case(
            "near_miss",
            row["symbol"],
            date.fromisoformat(row["trade_date"]),
            *(_ep(row, k) for k in CASE_COLUMNS[3:9]),
            failed_gate=row["failed_gate"],
            reason=row.get("reason") or "",
            instrument_id=row["instrument_id"],
        )
        for row in rows
        if row.get("playbook") == EP and near_miss(row, gates, setup)
    ]


def sample(cases: Sequence[Case], n: int, seed: int) -> list[Case]:
    """`min(n, len)` of `cases` drawn with `seed`, in their order."""
    picked = set(
        random.Random(seed).sample(range(len(cases)), min(n, len(cases)))  # noqa: S311  # NOSONAR - a seeded, reproducible draw
    )
    return [c for i, c in enumerate(cases) if i in picked]


def sma(values: Sequence[float], length: int) -> list[float | None]:
    """The simple moving average of `values` at each point (None before `length` values)."""
    out: list[float | None] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        out.append(total / length if i >= length - 1 else None)
    return out


def opening_range_high(bars: Sequence[Bar]) -> float | None:
    """The high of the session's first 5-minute bar (`price_breaks_level` with `bar: 5m`)."""
    return float(bars[0].high) if bars else None


def markdown(cases: Sequence[Case], seed: int, pools: Mapping[str, int]) -> str:
    lines = [
        "# S2 EP audit (C4)",
        "",
        f"Seed: {seed}. Pools: {pools['entry']} EP trades, {pools['near_miss']} near misses.",
        "",
        "| " + " | ".join(CASE_COLUMNS[:-1]) + " |",
        "| " + " | ".join("---" for _ in CASE_COLUMNS[:-1]) + " |",
    ]
    for case in cases:
        row = asdict(case)
        lines.append(
            "| " + " | ".join("" if row[c] is None else str(row[c]) for c in CASE_COLUMNS[:-1])
        )
    return "\n".join(lines) + "\n"


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _charts(  # pragma: no cover - drawing
    out: Path, case: Case, intraday: Sequence[Bar], daily: Sequence[Mapping[str, object]]
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stem = f"{case.kind}_{case.date}_{case.symbol}"
    fig, ax = plt.subplots(figsize=(12, 6))
    for i, bar in enumerate(intraday):
        ax.vlines(i, float(bar.low), float(bar.high), color="#555555", linewidth=0.8)
        ax.hlines(float(bar.close), i, i + 0.4, color="#555555", linewidth=0.8)
    orh = opening_range_high(intraday)
    marks = (
        ("ORH", orh, "#1f77b4"),
        ("entry", case.entry, "#2ca02c"),
        ("stop", case.stop, "#d62728"),
    )
    for label, level, colour in marks:
        if level is not None:
            ax.axhline(
                level, color=colour, linewidth=1, linestyle="--", label=f"{label} {level:.2f}"
            )
    ticks = list(range(0, len(intraday), 6))
    ax.set_xticks(ticks, [intraday[i].start.strftime("%H:%M") for i in ticks])
    ax.set_title(f"{case.symbol} {case.date} 5-minute ({case.kind}, trigger {case.trigger_time})")
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out / f"{stem}_5m.png", dpi=110)
    plt.close(fig)

    closes = [float(r["close"]) for r in daily]  # type: ignore[arg-type]
    dates = [r["date"] for r in daily]
    fig, ax = plt.subplots(figsize=(14, 6))
    for i, r in enumerate(daily):
        ax.vlines(i, float(r["low"]), float(r["high"]), color="#555555", linewidth=0.8)  # type: ignore[arg-type]
    for length, colour in zip(SMAS, ("#1f77b4", "#ff7f0e", "#d62728"), strict=True):
        ax.plot(
            range(len(closes)), sma(closes, length), color=colour, linewidth=1, label=f"SMA{length}"
        )
    for label, day in (("day", case.date), ("exit", case.exit_date)):
        if day in dates:
            ax.axvline(dates.index(day), color="#999999", linestyle=":", label=label)
    ticks = list(range(0, len(dates), 20))
    ax.set_xticks(ticks, [str(dates[i]) for i in ticks])
    ax.set_title(f"{case.symbol} daily, SMA10/20/50 ({case.kind} {case.date})")
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out / f"{stem}_daily.png", dpi=110)
    plt.close(fig)


def daily_span(days: Sequence[date], day: date, last: date) -> tuple[date, int]:
    """The last session a case's daily chart draws (`DAILY_AFTER` after `last`, its exit) and how
    many bars to draw up to it: `DAILY_BEFORE` before `day` plus the SMA warm-up."""
    at = days.index(last) if last in days else len(days) - 1
    until = days[min(at + DAILY_AFTER, len(days) - 1)]
    held = at - days.index(day) if day in days else 0
    return until, DAILY_BEFORE + max(SMAS) + held + DAILY_AFTER


def _draw(out: Path, case: Case, data: Any, view: Any) -> None:  # noqa: ANN401  # pragma: no cover - drawing
    """A case's charts from the run's market (`MarketData`, `MarketView`)."""
    import polars as pl

    from app.hat.backtest.intraday import session_bars

    scale = view.price_scale(case.instrument_id, case.date)
    intraday: Sequence[Bar] = ()
    if data.intraday is not None and scale:
        found = session_bars(data.intraday, case.date, {case.instrument_id: scale})
        intraday = found.get(case.instrument_id, ())
    until, count = daily_span(list(view.days), case.date, case.exit_date or case.date)
    frame = data.bars.filter(
        (pl.col("instrument_id") == case.instrument_id) & (pl.col("date") <= until)
    ).tail(count)
    _charts(out, case, intraday, frame.select("date", "high", "low", "close").to_dicts())


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the database
    from sqlalchemy import text

    from app.database import db_session_scope
    from app.hat.backtest.data import MarketView, load_market
    from app.hat.backtest.run_files import read_config, read_run
    from app.hat.backtest.runs import _replayed
    from app.hat.data.store import DataStore
    from app.hat.models import HatEvent
    from app.hat.settings import operator_settings
    from app.paths import PROJECT_ROOT

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    print(f"seed {args.seed}")
    settings = operator_settings()
    root = settings.data_root
    run, config_doc = read_run(root, args.run_id), read_config(root, args.run_id)
    if run is None or config_doc is None:
        raise SystemExit(f"no run {args.run_id} under {root}")
    target, spec = _replayed(config_doc)
    if spec.end > IN_SAMPLE_END:
        raise SystemExit(f"the run ends {spec.end}, after the in-sample period")
    playbook = spec.config.playbook(EP)
    gates = ep_log.ep_gates(playbook)
    setup = [i.type for i in playbook.setup.criteria if i.enabled and i.mode == "hard"]
    folder = root / "backtests" / "runs" / args.run_id
    entries = entry_cases(_read(folder / "trades.csv"))
    candidates = folder / "ep_candidates.csv"
    nears = near_cases(_read(candidates), gates, setup) if candidates.is_file() else []
    cases = sample(entries, ENTRY_SAMPLE, args.seed) + sample(nears, NEAR_SAMPLE, args.seed)
    print(f"entries: {len(entries)} EP trades; near misses: {len(nears)}; sampled {len(cases)}")
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        store = DataStore(root, memory_limit=settings.duckdb_memory)
        data, snapshot = load_market(
            store,
            session,
            universe_version_id=target.universe_version_id,
            index=spec.config.regime.index,
            start=spec.start,
            end=spec.end,
        )
    if snapshot.get("content_digest") != run["data"]["data_snapshot_id"]:
        raise SystemExit("the store's data is not the run's snapshot")
    if max(data.days) > IN_SAMPLE_END:
        raise SystemExit("loaded data after the in-sample period")
    view = MarketView(data, spec.config.regime.index)
    out = PROJECT_ROOT / "reports" / "s2-ep" / "audit"
    (out / "charts").mkdir(parents=True, exist_ok=True)
    for case in cases:
        _draw(out / "charts", case, data, view)
    with (out / "cases.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CASE_COLUMNS))
        writer.writeheader()
        writer.writerows(asdict(c) for c in cases)
    report = markdown(cases, args.seed, {"entry": len(entries), "near_miss": len(nears)})
    (out / "audit.md").write_text(report)
    print(report)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

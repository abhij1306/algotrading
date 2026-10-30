"""Diff a run against its rerun, trade by trade (docs/backtests/process.md, Rerun diff):

    python -m app.hat.backtest.rundiff BEFORE AFTER [--before-root DIR] [--after-root DIR]
        [--out DIR]

reads both run folders only (BEFORE under `--before-root`, AFTER under `--after-root`, each the
data root's by default) and prints: the identity of each (engine commit, data snapshot, window),
the headline metrics before, after and their change, the config paths whose values differ, and
the trade diff's counts. Trades are paired on (instrument, playbook, entry date, exit date), then
on (instrument, playbook, entry date, leg), then what is left on (instrument, playbook, signal
date, leg) as a moved entry (`diff_trades`); a pair is the same
trade or changed in named fields (`TRADE_FIELDS`). `--out` writes `trade_diff.csv` (one row per
trade, paired or not, with its before and after fields) and `config_diff.csv`. The first
divergence is the earliest entry date of any trade that is not the same: what changed before it
is in neither run's trades.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import folder

__all__ = [
    "HEADLINE",
    "SIZING_FIELDS",
    "TRADE_FIELDS",
    "Pair",
    "TradeDiff",
    "config_diff",
    "diff_trades",
    "flatten",
    "headline_rows",
    "report",
]

# (label, run.json block, name): the headline metrics, before and after
HEADLINE = (
    ("CAGR %", "metrics", "cagr_pct"),
    ("MAR", "metrics", "mar"),
    ("max DD %", "metrics", "max_dd_pct"),
    ("longest DD sessions", "metrics", "longest_dd_sessions"),
    ("trades", "metrics", "trades"),
    ("win %", "metrics", "win_pct"),
    ("avg R", "metrics", "expectancy_r"),
    ("invested %", "metrics", "invested_pct"),
    ("NIFTY 500 TRI CAGR %", "metrics", "benchmark_cagr_pct"),
    ("fees", "metrics", "fees"),
    ("final equity", "metrics", "final_equity"),
)
# the trade fields a pair compares; prices and money within half a paisa are equal
TRADE_FIELDS = (
    "entry_date",
    "entry_price",
    "initial_stop",
    "qty",
    "exit_date",
    "exit_price",
    "exit_reason",
    "fees",
    "net_pnl",
)
_TOLERANCE = 0.005
SIZING_FIELDS = frozenset({"qty", "fees", "net_pnl"})
# config.json keys that name the run rather than configure it
_IDENTITY_KEYS = frozenset({"run_id", "data_snapshot_id", "data_snapshot", "data_audit"})


@dataclass(frozen=True)
class Pair:
    """A trade in both runs (`before` and `after` rows), how it was paired (`entry` or `moved`)
    and the `TRADE_FIELDS` that differ (none: the same trade)."""

    before: Mapping[str, str]
    after: Mapping[str, str]
    how: str
    changed: tuple[str, ...]


@dataclass
class TradeDiff:
    pairs: list[Pair] = field(default_factory=list)
    only_before: list[Mapping[str, str]] = field(default_factory=list)
    only_after: list[Mapping[str, str]] = field(default_factory=list)

    @property
    def same(self) -> list[Pair]:
        return [p for p in self.pairs if not p.changed]

    @property
    def changed(self) -> list[Pair]:
        return [p for p in self.pairs if p.changed]

    @property
    def sizing_only(self) -> list[Pair]:
        """Changed pairs whose entry and exit are the same: only the size and its money differ
        (`SIZING_FIELDS`), as when an earlier difference moved the equity the run sizes from."""
        return [p for p in self.changed if set(p.changed) <= SIZING_FIELDS]

    def pnl_change(self) -> dict[str, float]:
        """The net P&L the after run gained over the before run, by bucket: trades only before
        (lost), only after, the changed pairs' differences, and the total."""

        def pnl(row: Mapping[str, str]) -> float:
            return _num(row.get("net_pnl")) or 0.0

        parts = {
            "only_before": -sum(pnl(r) for r in self.only_before),
            "only_after": sum(pnl(r) for r in self.only_after),
            "sizing_only": sum(pnl(p.after) - pnl(p.before) for p in self.sizing_only),
            "entry_or_exit": sum(
                pnl(p.after) - pnl(p.before)
                for p in self.changed
                if not set(p.changed) <= SIZING_FIELDS
            ),
        }
        return {**parts, "total": sum(parts.values())}

    def field_counts(self) -> Counter[str]:
        """How many pairs differ in each field."""
        return Counter(name for p in self.pairs for name in p.changed)

    def first_divergence(self) -> str | None:
        """The earliest entry date of a trade not the same in both runs (ISO text), or None."""
        dates = [r.get("entry_date") or "" for r in (*self.only_before, *self.only_after)]
        dates += [
            min(p.before.get("entry_date") or "", p.after.get("entry_date") or "")
            for p in self.changed
        ]
        return min((d for d in dates if d), default=None)


def _differs(a: str | None, b: str | None) -> bool:
    if a == b:
        return False
    try:
        return abs(float(a or "") - float(b or "")) > _TOLERANCE
    except ValueError:
        return True


def _instrument(row: Mapping[str, str]) -> str:
    return row.get("instrument_id") or row.get("symbol") or ""


def _keyed(
    rows: Iterable[Mapping[str, str]], fields: Sequence[str]
) -> dict[tuple[str, ...], list[Mapping[str, str]]]:
    found: dict[tuple[str, ...], list[Mapping[str, str]]] = {}
    for row in rows:
        key = (_instrument(row), *(row.get(f) or "" for f in fields))
        found.setdefault(key, []).append(row)
    return found


def _pair_on(
    before: list[Mapping[str, str]],
    after: list[Mapping[str, str]],
    fields: Sequence[str],
    how: str,
) -> tuple[list[Pair], list[Mapping[str, str]], list[Mapping[str, str]]]:
    """Pair rows equal on the instrument and `fields`, in file order within a key."""
    theirs = _keyed(after, fields)
    pairs, left = [], []
    for row in before:
        key = (_instrument(row), *(row.get(f) or "" for f in fields))
        bucket = theirs.get(key)
        if bucket:
            other = bucket.pop(0)
            changed = tuple(f for f in TRADE_FIELDS if _differs(row.get(f), other.get(f)))
            pairs.append(Pair(row, other, how, changed))
        else:
            left.append(row)
    rest = [r for bucket in theirs.values() for r in bucket]
    return pairs, left, rest


def diff_trades(
    before: Sequence[Mapping[str, str]], after: Sequence[Mapping[str, str]]
) -> TradeDiff:
    """Pair the `trades.csv` rows of two runs: a leg on its entry and exit dates (a leg added
    or dropped before it renumbers the later legs, as a rotation trim does), then on its entry
    and leg number, then a moved entry on its signal and leg number."""
    pairs, left, rest = _pair_on(
        list(before), list(after), ("playbook", "entry_date", "exit_date"), "entry"
    )
    more, left, rest = _pair_on(left, rest, ("playbook", "entry_date", "leg"), "entry")
    moved, left, rest = _pair_on(left, rest, ("playbook", "signal_date", "leg"), "moved")
    pairs += more + moved
    order = {id(r): n for n, r in enumerate(after)}
    rest.sort(key=lambda r: order[id(r)])
    return TradeDiff(pairs, left, rest)


def flatten(value: Any, prefix: str = "") -> dict[str, str]:  # noqa: ANN401 - any JSON
    """The scalar leaves of a JSON value by dotted path (list items by index)."""
    if isinstance(value, Mapping):
        items: Iterable[tuple[str, Any]] = ((str(k), v) for k, v in value.items())
    elif isinstance(value, list):
        items = ((str(n), v) for n, v in enumerate(value))
    else:
        return {prefix: "null" if value is None else str(value)}
    found: dict[str, str] = {}
    for key, item in items:
        found.update(flatten(item, f"{prefix}.{key}" if prefix else key))
    return found


def config_diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[tuple[str, str | None, str | None]]:
    """The `config.json` paths whose values differ (absent: None), the identity keys left out."""
    mine = flatten({k: v for k, v in before.items() if k not in _IDENTITY_KEYS})
    theirs = flatten({k: v for k, v in after.items() if k not in _IDENTITY_KEYS})
    return [
        (path, mine.get(path), theirs.get(path))
        for path in sorted(mine.keys() | theirs.keys())
        if mine.get(path) != theirs.get(path)
    ]


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def headline_rows(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[tuple[str, object, object, float | None]]:
    """(label, before, after, after - before) of each `HEADLINE` metric, and the CAGR over the
    benchmark's."""
    rows: list[tuple[str, object, object, float | None]] = []
    for label, block, name in HEADLINE:
        a = (before.get(block) or {}).get(name)
        b = (after.get(block) or {}).get(name)
        x, y = _num(a), _num(b)
        rows.append((label, a, b, None if x is None or y is None else y - x))
    excess = []
    for run in (before, after):
        m = run.get("metrics") or {}
        c, t = _num(m.get("cagr_pct")), _num(m.get("benchmark_cagr_pct"))
        excess.append(None if c is None or t is None else c - t)
    x, y = excess
    rows.append(("CAGR over TRI, pts", x, y, None if x is None or y is None else y - x))
    return rows


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.4g}"
    return str(value)


def _identity(run: Mapping[str, Any]) -> str:
    i, d = run.get("identity") or {}, run.get("data") or {}
    return (
        f"{i.get('run_id')} engine {str(i.get('engine_commit'))[:8]} "
        f"data {str(d.get('data_snapshot_id'))[:12]} {d.get('period_start')}..{d.get('period_end')}"
        f" status {i.get('status')}"
    )


def report(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    trades: TradeDiff,
    configs: Sequence[tuple[str, str | None, str | None]],
) -> str:
    """The Markdown report of a rerun diff."""
    lines = [f"before: {_identity(before)}", f"after:  {_identity(after)}", ""]
    lines += ["| metric | before | after | change |", "|---|---|---|---|"]
    lines += [
        f"| {label} | {_cell(a)} | {_cell(b)} | {_cell(d)} |"
        for label, a, b, d in headline_rows(before, after)
    ]
    moved = sum(p.how == "moved" for p in trades.pairs)
    fields = ", ".join(f"{k} {n}" for k, n in trades.field_counts().most_common()) or "none"
    lines += [
        "",
        f"trades: same {len(trades.same)}, changed {len(trades.changed)} (moved entry {moved}),"
        f" only before {len(trades.only_before)}, only after {len(trades.only_after)};"
        f" of the changed, sizing only (qty, fees, P&L) {len(trades.sizing_only)}",
        "net P&L change: " + ", ".join(f"{k} {_cell(v)}" for k, v in trades.pnl_change().items()),
        f"changed fields: {fields}",
        f"first divergence: {trades.first_divergence() or 'none'}",
        "",
        f"config paths that differ: {len(configs)}",
    ]
    lines += [f"- `{path}`: {a} -> {b}" for path, a, b in configs]
    return "\n".join(lines) + "\n"


def trade_diff_csv(trades: TradeDiff) -> str:
    """One row per trade: its status (same, changed, only_before, only_after), the fields that
    changed, and the identifying and `TRADE_FIELDS` before and after."""
    ids = ("symbol", "playbook", "signal_date", "leg")
    cols = ["status", "how", "changed", *ids]
    cols += [f"{f}_{side}" for f in TRADE_FIELDS for side in ("before", "after")]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(cols)

    def row(
        status: str,
        how: str,
        changed: str,
        b: Mapping[str, str] | None,
        a: Mapping[str, str] | None,
    ) -> list[str]:
        base = b if b is not None else a or {}
        cells = [status, how, changed, *(base.get(i) or "" for i in ids)]
        for f in TRADE_FIELDS:
            cells += [(b or {}).get(f) or "", (a or {}).get(f) or ""]
        return cells

    rows = [
        row("changed" if p.changed else "same", p.how, ";".join(p.changed), p.before, p.after)
        for p in trades.pairs
    ]
    rows += [row("only_before", "", "", r, None) for r in trades.only_before]
    rows += [row("only_after", "", "", None, r) for r in trades.only_after]
    # entry date order, before's date first
    rows.sort(key=lambda r: r[cols.index("entry_date_before")] or r[cols.index("entry_date_after")])
    writer.writerows(rows)
    return out.getvalue()


def _load(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    run = folder.read_run(root, run_id)
    if run is None:
        raise SystemExit(f"rundiff: no run {run_id} under {root}")
    text = folder.read_csv(root, run_id, "trades")
    rows = [] if text is None else list(csv.DictReader(io.StringIO(text)))
    return run, folder.read_config(root, run_id) or {}, rows


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the command line
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest.rundiff")
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--before-root", type=Path, default=None)
    parser.add_argument("--after-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    root = None
    if args.before_root is None or args.after_root is None:
        root = operator_settings().data_root
    run_b, config_b, rows_b = _load(args.before_root or root, args.before)
    run_a, config_a, rows_a = _load(args.after_root or root, args.after)
    trades = diff_trades(rows_b, rows_a)
    configs = config_diff(config_b, config_a)
    sys.stdout.write(report(run_b, run_a, trades, configs))
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "trade_diff.csv").write_text(trade_diff_csv(trades), encoding="utf-8")
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["path", "before", "after"])
        writer.writerows(configs)
        (args.out / "config_diff.csv").write_text(out.getvalue(), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

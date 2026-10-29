"""The backtest engine's lookahead and determinism checks on real data (accuracy plan W2 B2),
outside the test suite: each run is the engine in memory (`loop.run_backtest`) on the market the
loader reads for its window, so nothing is published and no trial is recorded.

`truncation`: a portfolio run from `--from` to T and the same run to T+N (`--extra` sessions
later) must make the same decisions up to T: the same watchlists and signals (taken or refused,
sized the same) through T, the same skips and funnel counts by session and the same EP log
decisions through T, the same fills before T, every fill of T the longer run makes also in the
shorter one (whose extra fills on T are only its period-end closes), the same rotation orders
through T, the ETF overlay's rows and the equity curve before T. A difference is a read of
something stamped after the decision (or a loader that reads past its window) and is printed.

One difference is known and reported apart, `delisting_known_later`: an instrument the registry
delists after T whose last bar is on or before T. The longer run closes it at that last close
(`MarketData.last_traded`, counted `delisted_exit` in its hindsight reads); the shorter one cannot
know it is delisted, and the cash that exit frees can change the later decisions of other names.
The comparison is then exact up to the session before the first such exit, and the report says
`diverged_after_delisting_hindsight_on` that day: the later days are neither passed nor failed.

`fingerprint`: one run's resolved-config hash, trade-list hash (as `run.json` `binding` hashes
it), and hashes of its fills, signals and equity curve, to show a change left a run's results
unchanged (or how it changed them).

    python scripts/research/bt_truncation.py truncation --portfolio P.yaml --from 2017-07-01 \
        --at 2018-06-29 [--extra 63] [--out report.json]
    python scripts/research/bt_truncation.py fingerprint --portfolio P.yaml --from D --to D

The portfolio file is the one `python -m app.hat.backtest --portfolio` runs (`portfolio.py`);
its window is replaced by the command line's. Hold-out windows (2025 on) are refused. Run from
the repo root or `backend/` with `HAT_DATA_ROOT` and the `DB_*` variables set; the database is
read in a read-only transaction.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.backtest import etf_data  # noqa: E402
from app.hat.backtest.data import MarketData, load_market, symbol_on  # noqa: E402
from app.hat.backtest.loop import run_backtest  # noqa: E402
from app.hat.backtest.overrides import read_policy_file  # noqa: E402
from app.hat.backtest.portfolio import parse_portfolio  # noqa: E402
from app.hat.backtest.report import DELISTED, END_OF_PERIOD, BacktestResult  # noqa: E402
from app.hat.backtest.runs import _configs, _spec, _target, resolved_of  # noqa: E402
from app.hat.backtest.spec import REGIME_MULT_POLICY, RunSpec  # noqa: E402
from app.hat.backtest.standard import trade_list_hash  # noqa: E402
from app.hat.confighash import config_hash  # noqa: E402
from app.hat.data.store import DATASET_BARS, DataStore  # noqa: E402
from app.hat.models import HatEvent  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

HOLD_OUT_START = date(2025, 1, 1)  # never run: the hold-out
MAX_LISTED = 20
REPORTS = PROJECT_ROOT / "reports" / "bt-verify"  # where `--out` writes (gitignored)
_REPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json")


def _spec_of(
    session: Any,  # noqa: ANN401
    doc: Mapping[str, Any],
    start: date,
    end: date,
) -> tuple[RunSpec, Any]:
    """The run spec of the portfolio `doc` over `start..end`, and its target."""
    if end >= HOLD_OUT_START:
        raise SystemExit("refused: the window reaches the 2025 hold-out")
    window = {"window": {"from": start.isoformat(), "to": end.isoformat()}}
    request = parse_portfolio({**doc, **window}, read_policy_file)
    target = _target(session, request)
    return _spec(target, request, request.conventions), target


def _run(
    session: Any,  # noqa: ANN401
    store: DataStore,
    doc: Mapping[str, Any],
    start: date,
    end: date,
) -> tuple[RunSpec, MarketData, BacktestResult, float]:
    """One run of the portfolio `doc` over `start..end` in memory, timed."""
    began = time.monotonic()
    spec, target = _spec_of(session, doc, start, end)
    data, _ = load_market(
        store,
        session,
        universe_version_id=target.universe_version_id,
        index=spec.config.regime.index,
        start=start,
        end=end,
        regime_inputs=spec.conventions.regime_mult == REGIME_MULT_POLICY,
        results=any(p.preopen for c in _configs(spec) for p in c.playbooks),
    )
    etf = etf_data.load(store.root, start, end) if spec.overlay is not None else None
    result = run_backtest(spec, data, etf)
    return spec, data, result, time.monotonic() - began


def _text(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return "" if value is None else str(value)


def _digest(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def _fill_key(f: Any) -> str:  # noqa: ANN401
    return ",".join(
        _text(v) for v in (f.day, f.instrument_id, f.purpose, f.side, f.qty, f.price, f.fees)
    )


def _signal_key(s: Any) -> str:  # noqa: ANN401
    return ",".join(
        _text(v)
        for v in (
            s.trade_date,
            s.playbook,
            s.instrument_id,
            s.state,
            s.reason,
            s.trigger_price,
            s.stop_price,
            s.qty,
            s.entry_price,
            s.scan_date,
        )
    )


def _watch_key(w: Any) -> str:  # noqa: ANN401
    return ",".join(
        _text(v)
        for v in (w.trade_date, w.playbook, w.instrument_id, w.score, w.pivot, w.planned_stop)
    )


def _rebalance_key(row: Mapping[str, Any]) -> str:
    return json.dumps({k: _text(v) for k, v in row.items()}, sort_keys=True)


def fingerprint(spec: RunSpec, data: MarketData, result: BacktestResult) -> dict[str, Any]:
    """The run's resolved hash, trade-list hash and content hashes (see the module docstring)."""
    trades = {(t.instrument_id, t.entry_date, t.exit_date, t.playbook): t for t in result.trades}
    keys = [(symbol_on(data, i, e), e, x) for i, e, x, _ in trades]
    return {
        "resolved_hash": config_hash(resolved_of(spec)),
        "trade_list_hash": trade_list_hash(keys),
        "trades": len(result.trades),
        "fills_hash": _digest(_fill_key(f) for f in result.fills),
        "fills": len(result.fills),
        "signals_hash": _digest(_signal_key(s) for s in result.signals),
        "signals": len(result.signals),
        "equity_hash": _digest(f"{d},{_text(e)}" for d, e in result.equity),
        "final_equity": _text(result.equity[-1][1]) if result.equity else None,
        "rebalances_hash": _digest(_rebalance_key(r) for r in result.rebalances),
        "ledger": result.ledger,
        "hindsight": dict(result.hindsight),
        "skips": dict(result.skips),
    }


def _multiset_diff(a: Iterable[str], b: Iterable[str]) -> tuple[list[str], list[str]]:
    """(in a and not b, in b and not a), counted with multiplicity."""
    ca, cb = Counter(a), Counter(b)
    return sorted((ca - cb).elements()), sorted((cb - ca).elements())


# the EP log's decision columns (the rest are forward returns, read after the period)
EP_DECISION = ("trade_date", "instrument_id", "playbook", "basis", "reached", "failed_gate")
EP_DECISION += ("reason", "signal_state")


def delisted_later(short: BacktestResult, long: BacktestResult, at: date) -> dict[str, date]:
    """The instruments the longer run closes as delisted on or before `at` and the shorter one
    does not (the module docstring), at that exit's day."""
    seen = {(t.instrument_id, t.exit_date) for t in short.trades if t.exit_reason == DELISTED}
    found: dict[str, date] = {}
    for t in long.trades:
        if (
            t.exit_reason == DELISTED
            and t.exit_date <= at
            and (t.instrument_id, t.exit_date) not in seen
        ):
            found[t.instrument_id] = min(t.exit_date, found.get(t.instrument_id, t.exit_date))
    return found


def compare(short: BacktestResult, long: BacktestResult, at: date) -> dict[str, dict[str, Any]]:
    """The decisions of the run to `at` against those of the longer run, up to `at`; with a
    delisting known later (the module docstring), exactly up to the session before the first
    such exit, and reported diverged from it on."""
    checks: dict[str, dict[str, Any]] = {}
    later = delisted_later(short, long, at)
    cut = min(later.values(), default=None)
    checks["delisting_known_later"] = {
        "pass": True,
        "instruments": {k: v.isoformat() for k, v in sorted(later.items())},
        "diverged_after_delisting_hindsight_on": None if cut is None else cut.isoformat(),
    }

    def through(day: date) -> bool:  # a decision of `day` is compared
        return day <= at if cut is None else day < cut

    def before(day: date) -> bool:  # a fill, an equity or overlay row of `day` is compared
        return day < at if cut is None else day < cut

    def record(name: str, diff: tuple[list[str], list[str]], compared: int) -> None:
        only_short, only_long = diff
        checks[name] = {
            "compared": compared,
            "pass": not only_short and not only_long,
            "only_short": list(only_short[:MAX_LISTED]),
            "only_long": list(only_long[:MAX_LISTED]),
            "differences": len(only_short) + len(only_long),
        }

    for name, rows_s, rows_l, key, day in (
        ("watchlists", short.watchlists, long.watchlists, _watch_key, lambda r: r.trade_date),
        ("signals", short.signals, long.signals, _signal_key, lambda r: r.trade_date),
    ):
        a = [key(r) for r in rows_s if through(day(r))]
        b = [key(r) for r in rows_l if through(day(r))]
        record(name, _multiset_diff(a, b), len(a))
    for name, rows_s, rows_l in (
        ("skips_funnel", short.decisions, long.decisions),
        ("ep_log", _ep(short), _ep(long)),
    ):
        a = [",".join(map(_text, r)) for r in rows_s if through(r[0])]
        b = [",".join(map(_text, r)) for r in rows_l if through(r[0])]
        record(name, _multiset_diff(a, b), len(a))
    before_s = [_fill_key(f) for f in short.fills if before(f.day)]
    before_l = [_fill_key(f) for f in long.fills if before(f.day)]
    record("fills_before_t", _multiset_diff(before_s, before_l), len(before_s))
    if cut is None:
        checks["fills_on_t"] = _fills_on(short, long, at)
    else:
        checks["fills_on_t"] = {"compared": 0, "pass": True, "not_compared": "diverged"}
    reb_s = [_rebalance_key(r) for r in short.rebalances if through(_day(r))]
    reb_l = [_rebalance_key(r) for r in long.rebalances if through(_day(r))]
    # the shorter run's period-end sales of its rotation holdings are its own
    reb_s = [k for k in reb_s if '"reason": "end_of_period"' not in k]
    reb_l = [k for k in reb_l if '"reason": "end_of_period"' not in k]
    record("rebalances", _multiset_diff(reb_s, reb_l), len(reb_s))
    eq_s = [f"{d},{_text(e)}" for d, e in short.equity if before(d)]
    eq_l = [f"{d},{_text(e)}" for d, e in long.equity if before(d)]
    record("equity_before_t", _multiset_diff(eq_s, eq_l), len(eq_s))
    ov_s = [",".join(r) for r in short.overlay_rows if r and before(date.fromisoformat(r[0]))]
    ov_l = [",".join(r) for r in long.overlay_rows if r and before(date.fromisoformat(r[0]))]
    record("overlay_before_t", _multiset_diff(ov_s, ov_l), len(ov_s))
    return checks


def _day(row: Mapping[str, Any]) -> date:
    found = row.get("date")
    return found if isinstance(found, date) else date.fromisoformat(str(found))


def _fills_on(short: BacktestResult, long: BacktestResult, at: date) -> dict[str, Any]:
    """Every fill of `at` the longer run makes also in the shorter one, whose extra fills are
    only its period-end sales: at most one per position it closed there, of its instrument."""
    on_s = [_fill_key(f) for f in short.fills if f.day == at]
    on_l = [_fill_key(f) for f in long.fills if f.day == at]
    extra_s, missing = _multiset_diff(on_s, on_l)
    closed = Counter(
        t.instrument_id
        for t in short.trades
        if t.exit_date == at and t.exit_reason == END_OF_PERIOD
    )
    stray = []
    for key in extra_s:
        instrument_id, side = key.split(",")[1], key.split(",")[3]
        if side == "sell" and closed[instrument_id] > 0:
            closed[instrument_id] -= 1
        else:
            stray.append(key)
    return {
        "compared": len(on_l),
        "pass": not missing and not stray,
        "only_long": missing[:MAX_LISTED],
        "short_period_end_sells": len(extra_s) - len(stray),
        "short_extra_not_period_end": stray[:MAX_LISTED],
        "differences": len(missing) + len(stray),
    }


def _ep(result: BacktestResult) -> list[tuple[Any, ...]]:
    return [tuple(r.get(c) for c in EP_DECISION) for r in result.ep_candidates]


def _sessions_after(store: DataStore, at: date, count: int) -> date:
    days = sorted(d for d in store.fingerprints(DATASET_BARS) if d > at)
    if len(days) < count:
        raise SystemExit("refused: the store ends before T+N")
    return days[count - 1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("truncation", "fingerprint"))
    parser.add_argument("--portfolio", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--at", type=date.fromisoformat, help="T (truncation)")
    parser.add_argument("--extra", type=int, default=63, help="N sessions after T (truncation)")
    parser.add_argument("--to", type=date.fromisoformat, help="the end (fingerprint)")
    parser.add_argument("--out", help=f"a report file name, written under {REPORTS}")
    args = parser.parse_args(argv)
    if args.out is not None and not _REPORT_NAME.fullmatch(args.out):
        raise SystemExit("refused: --out is a bare file name ending .json")
    doc = yaml.safe_load(args.portfolio.read_text(encoding="utf-8"))
    settings = operator_settings()
    report: dict[str, Any] = {"portfolio": doc.get("portfolio"), "mode": args.mode}
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
        if args.mode == "fingerprint":
            spec, data, result, took = _run(session, store, doc, args.start, args.to)
            report |= {
                "from": args.start.isoformat(),
                "to": args.to.isoformat(),
                "seconds": round(took),
            }
            report |= fingerprint(spec, data, result)
        else:
            later = _sessions_after(store, args.at, args.extra)
            if later >= HOLD_OUT_START:
                raise SystemExit("refused: T+N reaches the 2025 hold-out")
            spec, data, short, took_s = _run(session, store, doc, args.start, args.at)
            short_print = fingerprint(spec, data, short)
            del data, spec
            gc.collect()
            spec, data, long, took_l = _run(session, store, doc, args.start, later)
            del data, spec
            gc.collect()
            checks = compare(short, long, args.at)
            report |= {
                "from": args.start.isoformat(),
                "t": args.at.isoformat(),
                "t_plus_n": later.isoformat(),
                "seconds": [round(took_s), round(took_l)],
                "short": short_print,
                "checks": checks,
                "pass": all(c["pass"] for c in checks.values()),
            }
    text_out = json.dumps(report, indent=1, default=str)
    if args.out is not None:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / args.out).write_text(text_out + "\n", encoding="utf-8")
    print(text_out)
    return 0 if report.get("pass", True) else 1


if __name__ == "__main__":
    sys.exit(main())

"""A backtest run's folder (docs/backtests/result-standard.md): `HAT_DATA_ROOT/backtests/runs/
<run_id>/` with `run.json` (validated against `run.schema.json`), `config.json` (what
`--rerun` replays) and `trades.csv`, `equity.csv`, `refusals.csv`, `signals.csv`, `stocks.csv`.

`build` turns a run's result into those documents (pure); `publish` writes them into a hidden
temporary folder and renames it into place, so a folder that exists is complete; the readers
serve the API and a variant's base. A failed or stopped run publishes `run.json` and
`config.json` only. Nothing here touches the database.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from app.paths import BACKTEST_SCHEMA

from ..money import exact
from . import ep_log
from .ep_log import EP_CANDIDATE_COLUMNS, EP_COLUMNS
from .overlay import OVERLAY_CSV_HEADER
from .report import REBALANCE_COLUMNS, BacktestResult, LegRow, SignalRow, TradeRow
from .run_files import JSON_DOCS, is_run_id, read_config, read_doc, read_run, run_dir, runs_dir
from .signal_features import CRORE, SETUP_COLUMNS, STOCK_COLUMNS
from .spec import BASIS_5M, BacktestError
from .standard import (
    FORMAT_VERSION,
    NOT_A_SIGNAL,
    BookSession,
    BookTrade,
    binding,
    book_metrics,
    breadth_bucket,
    daily_returns,
    drawdowns,
    exit_code,
    leg_shares,
    mean_of,
    money_of,
    pct_of,
    refusal_code,
    rounded,
    sessions_held,
)

__all__ = [
    "CSV_FILES",
    "JSON_DOCS",
    "RunInputs",
    "build",
    "failed_run",
    "is_run_id",
    "list_runs",
    "publish",
    "read_config",
    "read_csv",
    "read_doc",
    "read_returns",
    "read_run",
    "run_dir",
    "runs_dir",
    "trade_keys",
    "validate",
]

# `ep_candidates` only in a run with a pre-open playbook (`ep_log`)
# `overlay` only in a run with the ETF overlay (its columns `overlay.OVERLAY_CSV_HEADER`)
# `rebalances` only in a run with a rotation strategy (`report.REBALANCE_COLUMNS`)
CSV_FILES = (
    "trades",
    "equity",
    "refusals",
    "signals",
    "stocks",
    "ep_candidates",
    "overlay",
    "rebalances",
)
COMPLETE, FAILED, STOPPED = "complete", "failed", "stopped_early"
ADV_LIMIT_PCT = 5
STOPOUT_DAYS = 90
_ZERO = Decimal(0)
_HUNDRED = Decimal(100)

CONTEXT_COLUMNS = (
    "ladder_level",
    "open_positions_at_entry",
    "open_risk_pct_at_entry",
    "index_regime_on",
    "breadth_pct",
)
# a rotation trade's rank (1 the best) and score on its entry night and its exit night
ROTATION_COLUMNS = ("rank_entry", "score_entry", "rank_exit", "score_exit")
TRADE_COLUMNS = (
    "run_id",
    "trade_id",
    "leg",
    "symbol",
    "industry",
    "signal_date",
    "entry_date",
    "entry_price",
    "initial_stop",
    "risk_per_share",
    "qty",
    "risk_pct_equity",
    "exit_date",
    "exit_price",
    "exit_reason",
    "hold_sessions",
    "gross_pnl",
    "fees",
    "net_pnl",
    "r_net",
    "mfe_r",
    "mae_r",
    "mfe_date",
    "mcap_cr",
    "cap_bucket",
    "index_membership",
    "adv_20d_cr",
    "liquidity_bucket",
    "position_pct_adv",
    "price",
    "listing_age_years",
    "prior_trades_symbol",
    "prior_stopouts_symbol_90d",
    *CONTEXT_COLUMNS,
    *SETUP_COLUMNS,
    "instrument_id",
    "playbook",
    "engine_exit_reason",
    "auction_risk",
    *EP_COLUMNS,
    *ROTATION_COLUMNS,
)
EQUITY_COLUMNS = (
    "date",
    "equity",
    "cash",
    "position_value",
    "invested_pct",
    "open_positions",
    "open_risk_pct",
    "drawdown_pct",
    "ladder_level",
    "index_regime_on",
    "breadth_pct",
    "benchmark_tri",
)
_FEATURES = ("industry", *STOCK_COLUMNS, *CONTEXT_COLUMNS, *SETUP_COLUMNS, *EP_COLUMNS)
REFUSAL_COLUMNS = (
    "date",
    "symbol",
    "reason",
    "would_be_entry",
    "would_be_stop",
    "fwd_r_20d_gross",
    "fwd_r_60d_gross",
    *_FEATURES,
)
SIGNAL_COLUMNS = (
    "date",
    "symbol",
    "setup_score",
    "taken",
    "refusal_reason",
    "trade_id",
    *(c for c in _FEATURES if c != "setup_score"),
)
STOCK_ROW_COLUMNS = (
    "symbol",
    "industry",
    "cap_bucket_mode",
    "signals",
    "trades",
    "wins",
    "net_pnl",
    "total_r",
    "expectancy_r",
    "best_trade_r",
    "worst_trade_r",
    "stopouts",
    "avg_hold_sessions",
    "first_trade",
    "last_trade",
)


def read_csv(root: Path, run_id: str, name: str) -> str | None:
    """One of the run's `CSV_FILES` as text; None without it (a failed run has none)."""
    folder = run_dir(root, run_id)
    if folder is None or name not in CSV_FILES:
        return None
    try:
        return (folder / f"{name}.csv").read_text(encoding="utf-8")
    except OSError:
        return None


def trade_keys(root: Path, run_id: str) -> list[tuple[str, date, date]] | None:
    """The run's (symbol, entry date, exit date) per trade (its legs by `trade_id`, the last
    sale's date), what binding compares; None without a readable `trades.csv`."""
    text = read_csv(root, run_id, "trades")
    if text is None:
        return None
    found: dict[str, tuple[str, date, date]] = {}
    try:
        for r in csv.DictReader(io.StringIO(text)):
            symbol, entry = r["symbol"], date.fromisoformat(r["entry_date"])
            exit_day = date.fromisoformat(r["exit_date"])
            known = found.get(r["trade_id"])
            found[r["trade_id"]] = (symbol, entry, max(exit_day, known[2] if known else exit_day))
    except (KeyError, ValueError):
        return None
    return list(found.values())


def read_returns(root: Path, run_id: str) -> dict[date, float] | None:
    """A run's daily returns from its `equity.csv` and its start capital; None when it has no
    readable equity file."""
    run, text = read_run(root, run_id), read_csv(root, run_id, "equity")
    start = exact(((run or {}).get("config") or {}).get("start_capital"), floats=True)
    if text is None or start is None:
        return None
    try:
        curve = [
            (date.fromisoformat(r["date"]), exact(r["equity"]))
            for r in csv.DictReader(io.StringIO(text))
        ]
    except (KeyError, ValueError):
        return None
    if any(value is None for _, value in curve):
        return None
    return daily_returns([(d, v) for d, v in curve if v is not None], start)


def list_runs(root: Path) -> list[dict[str, Any]]:
    """Every published run's `run.json`, newest first (run ids sort by time)."""
    base = runs_dir(root)
    if not base.is_dir():
        return []
    names = sorted((p.name for p in base.iterdir() if is_run_id(p.name)), reverse=True)
    return [doc for doc in (read_run(root, n) for n in names) if doc is not None]


# --- schema ----------------------------------------------------------------------------------


@cache
def _validator() -> Any:  # noqa: ANN401 - jsonschema's validator
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - only a writer needs it

    return Draft202012Validator(json.loads(BACKTEST_SCHEMA.read_text(encoding="utf-8")))


def validate(doc: Mapping[str, Any]) -> list[str]:
    """The schema errors of a `run.json` document (empty: valid), each `path: message`."""
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in _validator().iter_errors(doc)
    ]


# --- writing ---------------------------------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


def _csv(columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return out.getvalue()


def publish(
    root: Path,
    run_id: str,
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    csvs: Mapping[str, str] | None = None,
    *,
    docs: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path:
    """Write the run's folder: everything into a hidden temporary folder beside it, `run.json`
    validated first (`backtest_result_invalid`, nothing published), then one rename. `docs` are
    the folder's other JSON files by name (`JSON_DOCS`: the run and data audits, the evidence,
    the ledger)."""
    folder = run_dir(root, run_id)
    if folder is None:
        raise BacktestError("backtest_bad_run_id")
    if validate(run):
        raise BacktestError("backtest_result_invalid")
    folder.parent.mkdir(parents=True, exist_ok=True)
    tmp = folder.with_name(f".{run_id}.{uuid.uuid4().hex}.tmp")
    tmp.mkdir()
    try:
        (tmp / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
        (tmp / "config.json").write_text(json.dumps(config, indent=1), encoding="utf-8")
        for name, text in (csvs or {}).items():
            (tmp / f"{name}.csv").write_text(text, encoding="utf-8")
        for name, doc in (docs or {}).items():
            if name not in JSON_DOCS:
                raise BacktestError("backtest_bad_run_file")
            (tmp / f"{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return folder


# --- building --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInputs:
    """What a run's documents are built from besides its result: its `identity`, `data` and
    `config` blocks as run.json holds them, its start capital and policy cap (the refusal
    split), the NIFTY 500 TRI closes, the base run's trades (None: no base), the DSR and the PBO."""

    identity: Mapping[str, Any]
    data: Mapping[str, Any]
    config: Mapping[str, Any]
    start_capital: Decimal
    policy_max_positions: int
    benchmark: Sequence[tuple[date, Decimal]] = ()
    base_trades: Sequence[tuple[str, date, date]] | None = None
    dsr_prob: float | None = None
    dsr_n_trials: int | None = None
    dsr_n_trials_effective: int | None = None  # the trial count the DSR's floor scored it at
    pbo: float | None = None  # the family's probability of backtest overfitting (None: unscored)
    rf_pct: float = 0.0


def _bucket(trades: Sequence[_Trade]) -> dict[str, Any]:
    rs = [t.row.r_multiple for t in trades]
    return {
        "trades": len(trades),
        "win_pct": rounded(sum(1 for r in rs if r > 0) / len(rs) * 100) if rs else None,
        "expectancy_r": rounded(mean_of(rs)),
        "avg_hold_sessions": rounded(mean_of([t.hold for t in trades])),
        "net_pnl": money_of(sum((t.row.realized_pnl for t in trades), _ZERO)),
    }


def _group(trades: Iterable[_Trade], key: Any) -> dict[str, Any]:  # noqa: ANN401 - a callable
    groups: dict[str, list[_Trade]] = defaultdict(list)
    for t in trades:
        name = key(t)
        if name is not None:
            groups[str(name)].append(t)
    return {k: _bucket(v) for k, v in sorted(groups.items())}


@dataclass(frozen=True)
class _Trade:
    """A closed trade with its signal (the features at entry) and its spec fields."""

    trade_id: int
    row: TradeRow
    signal: SignalRow | None
    exit: str
    features: Mapping[str, Any]
    hold: int  # sessions from the entry to the last sale (`standard.sessions_held`)


def _closes_on(closes: Sequence[tuple[date, Decimal]]) -> dict[date, Decimal]:
    return dict(closes)


def _before(closes: Sequence[tuple[date, Decimal]], day: date) -> Decimal | None:
    """The last close before `day`."""
    found = [c for d, c in closes if d < day]
    return found[-1] if found else None


def _yearly(
    result: BacktestResult,
    start: Decimal,
    trades: Sequence[_Trade],
    benchmark: Sequence[tuple[date, Decimal]],
) -> list[dict[str, Any]]:
    """Per calendar year: the mark-to-market return of the last close over the year before's
    (the first over the start capital), the year's deepest fall from a peak starting at that
    base, the benchmark's return over the same closes, its entries decided on the stored
    5-minute bars and on the daily bar (`entry_bars_by_year`), and the closed trades on each
    basis named: those entered that year with their expectancy, those exited that year with
    theirs, and the exits' realized P&L. A trade held over a year end counts by its entry in one
    year and by its exit in the next, while its open profit marks both years' return."""
    out = []
    bases = result.metrics.get("entry_bars_by_year") or {}
    equity = list(result.equity)
    if not equity:
        return out
    bench = _closes_on(benchmark)
    base = start
    bench_base = _before(benchmark, equity[0][0])
    for year in sorted({d.year for d, _ in equity}):
        days = [(d, v) for d, v in equity if d.year == year]
        year_dd = drawdowns(days, base)
        entries = [t for t in trades if t.row.entry_date.year == year]
        exits = [t for t in trades if t.row.exit_date.year == year]
        last_bench = bench.get(days[-1][0])
        entered = bases.get(str(year)) or {}
        out.append(
            {
                "year": year,
                "return_pct": rounded((days[-1][1] / base - 1) * 100) if base > 0 else None,
                "max_dd_pct": rounded(year_dd.max_pct),
                "trades_entered": len(entries),
                "expectancy_r_by_entry": rounded(mean_of([t.row.r_multiple for t in entries])),
                "trades_exited": len(exits),
                "expectancy_r_by_exit": rounded(mean_of([t.row.r_multiple for t in exits])),
                "realized_pnl_by_exit": money_of(sum((t.row.realized_pnl for t in exits), _ZERO)),
                "benchmark_return_pct": rounded((last_bench / bench_base - 1) * 100)
                if last_bench is not None and bench_base
                else None,
                "entries_5m": entered.get(BASIS_5M, 0),
                "entries_daily": sum(n for basis, n in entered.items() if basis != BASIS_5M),
            }
        )
        base = days[-1][1]
        bench_base = last_bench
    return out


def _trades(result: BacktestResult, days: Sequence[date]) -> list[_Trade]:
    """The closed trades in entry order, numbered from 1, each with the signal it entered on and
    its hold in the run's sessions `days`."""
    signals = {(s.instrument_id, s.trade_date): s for s in result.signals if s.qty > 0}
    rows = sorted(result.trades, key=lambda t: (t.entry_date, t.instrument_id, t.exit_date))
    out = []
    for n, row in enumerate(rows, start=1):
        signal = signals.get((row.instrument_id, row.entry_date))
        out.append(
            _Trade(
                n,
                row,
                signal,
                exit_code(
                    row.exit_reason,
                    row.initial_stop,
                    row.final_stop,
                    row.rules_fired,
                    row.exit_sma,
                ),
                {} if signal is None else signal.features,
                sessions_held(days, row.entry_date, row.exit_date),
            )
        )
    return out


def _context(signal: SignalRow | None) -> dict[str, Any]:
    if signal is None:
        return {}
    return {
        "ladder_level": signal.ladder_level,
        "open_positions_at_entry": signal.open_positions,
        "open_risk_pct_at_entry": pct_of(signal.open_risk, signal.equity),
        "index_regime_on": signal.regime_on,
        "breadth_pct": rounded(signal.breadth_pct),
    }


def _history(trades: Sequence[_Trade], symbol: str, day: date) -> tuple[int, int]:
    """(the symbol's trades closed before `day`, its initial-stop exits in the
    `STOPOUT_DAYS` calendar days before it)."""
    before = [t for t in trades if t.features.get("symbol") == symbol and t.row.exit_date < day]
    since = day - timedelta(days=STOPOUT_DAYS)
    return len(before), sum(
        1 for t in before if t.exit == "initial_stop" and t.row.exit_date >= since
    )


def _position_pct_adv(t: _Trade) -> float | None:
    adv = exact(t.features.get("adv_20d_cr"), floats=True, positive=True)
    if adv is None:
        return None
    return rounded(t.row.entry_price * t.row.qty / (adv * CRORE) * _HUNDRED)


def _legs_of(row: TradeRow) -> tuple[LegRow, ...]:
    """The trade's sales; one closing leg of the whole position for a trade with none."""
    if row.legs:
        return row.legs
    price = row.entry_price if row.exit_price is None else row.exit_price
    return (LegRow(row.exit_date, row.qty, price, _ZERO, row.exit_reason),)


def _trade_rows(
    run_id: str, trades: Sequence[_Trade], days: Sequence[date]
) -> list[dict[str, Any]]:
    """One row per leg (a sale) of each trade, numbered from 1 under the trade's `trade_id`:
    its shares, exit, and its share of the trade's P&L, fees and R (`leg_shares`); the trade's
    entry, risk and features on every leg."""
    out = []
    for t in trades:
        row, signal = t.row, t.signal
        risk = row.entry_price - row.initial_stop
        prior, stopouts = _history(
            trades, t.features.get("symbol") or row.instrument_id, row.entry_date
        )
        legs = _legs_of(row)
        shares = leg_shares(
            [(leg.qty, leg.price, leg.fees) for leg in legs],
            row.entry_price,
            row.initial_stop,
            row.realized_pnl,
            row.fees,
            row.r_multiple,
        )
        for n, (leg, share) in enumerate(zip(legs, shares, strict=True), start=1):
            last = n == len(legs)
            out.append(
                {
                    **t.features,
                    **_context(signal),
                    "run_id": run_id,
                    "trade_id": t.trade_id,
                    "leg": n,
                    "symbol": t.features.get("symbol") or row.instrument_id,
                    "signal_date": None if signal is None else signal.scan_date,
                    "entry_date": row.entry_date,
                    "entry_price": row.entry_price,
                    "initial_stop": row.initial_stop,
                    "risk_per_share": risk,
                    "qty": leg.qty,
                    "risk_pct_equity": pct_of(
                        risk * row.qty, None if signal is None else signal.equity
                    ),
                    "exit_date": leg.day,
                    "exit_price": round(leg.price, 4),
                    "exit_reason": t.exit
                    if last
                    else exit_code(
                        leg.rule, row.initial_stop, row.final_stop, row.rules_fired, row.exit_sma
                    ),
                    "hold_sessions": sessions_held(days, row.entry_date, leg.day),
                    "gross_pnl": share.gross,
                    "fees": share.fees,
                    "net_pnl": share.net,
                    "r_net": share.r,
                    "mfe_r": row.mfe_r,
                    "mae_r": row.mae_r,
                    "mfe_date": row.mfe_date,
                    "position_pct_adv": _position_pct_adv(t),
                    "prior_trades_symbol": prior,
                    "prior_stopouts_symbol_90d": stopouts,
                    "instrument_id": row.instrument_id,
                    "playbook": row.playbook,
                    "engine_exit_reason": leg.rule if not last else row.exit_reason,
                    "auction_risk": row.auction_risk if last else None,
                    **(_rotation(row, last) if row.entry_rank is not None else {}),
                }
            )
    return out


def _rotation(row: TradeRow, last: bool) -> dict[str, Any]:
    """A rotation trade's `ROTATION_COLUMNS`: its entry's on every leg, its exit's on the last."""
    return {
        "rank_entry": row.entry_rank,
        "score_entry": row.score,
        "rank_exit": row.exit_rank if last else None,
        "score_exit": row.exit_score if last else None,
    }


def _equity_rows(
    result: BacktestResult, start: Decimal, bench: Mapping[date, Decimal]
) -> list[dict[str, Any]]:
    out = []
    peak = start
    for s in result.sessions:
        peak = max(peak, s.equity)
        out.append(
            {
                "date": s.day,
                "equity": s.equity,
                "cash": s.cash,
                "position_value": s.position_value,
                "invested_pct": pct_of(s.position_value, s.equity),
                "open_positions": s.open_positions,
                "open_risk_pct": pct_of(s.open_risk, s.equity),
                "drawdown_pct": rounded((1 - s.equity / peak) * _HUNDRED) if peak > 0 else None,
                "ladder_level": s.ladder_level,
                "index_regime_on": s.regime_on,
                "breadth_pct": rounded(s.breadth_pct),
                "benchmark_tri": bench.get(s.day),
            }
        )
    return out


def _metrics(
    result: BacktestResult, trades: Sequence[_Trade], signals: int, inputs: RunInputs
) -> dict[str, Any]:
    book = book_metrics(
        [
            BookTrade(
                t.features.get("symbol") or t.row.instrument_id,
                t.row.r_multiple,
                t.row.realized_pnl,
                t.row.fees,
                t.hold,
                t.row.exit_date,
                t.row.mfe_r,
                t.row.mae_r,
            )
            for t in trades
        ],
        list(result.equity),
        [BookSession(s.day, s.equity, s.position_value, s.open_positions) for s in result.sessions],
        inputs.start_capital,
        date.fromisoformat(inputs.data["period_start"]),
        inputs.benchmark,
        inputs.rf_pct,
    )
    adv = [p for p in (_position_pct_adv(t) for t in trades) if p is not None]
    return {
        **book,
        "signals": signals,
        "dsr_prob": rounded(inputs.dsr_prob, 6),
        "dsr_n_trials": inputs.dsr_n_trials,
        "dsr_n_trials_effective": inputs.dsr_n_trials_effective,
        "pbo": rounded(inputs.pbo, 6),
        "max_position_pct_adv": rounded(max(adv)) if adv else None,
        "trades_over_5pct_adv": sum(1 for p in adv if p > ADV_LIMIT_PCT),
    }


def _signal_rows(
    signals: Sequence[SignalRow],
    codes: Mapping[int, str],
    trade_of: Mapping[tuple[str, date], int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(signals.csv rows, refusals.csv rows)."""
    out, refused = [], []
    for n, s in enumerate(signals):
        base = {**s.features, **_context(s)}
        symbol = s.features.get("symbol") or s.instrument_id
        code = codes.get(n)
        out.append(
            {
                **base,
                "date": s.trade_date,
                "symbol": symbol,
                "taken": s.qty > 0,
                "refusal_reason": code,
                "trade_id": trade_of.get((s.instrument_id, s.trade_date)),
            }
        )
        if code is not None:
            refused.append(
                {
                    **base,
                    "date": s.trade_date,
                    "symbol": symbol,
                    "reason": code,
                    "would_be_entry": s.entry_price,
                    "would_be_stop": s.stop_price,
                    "fwd_r_20d_gross": None if s.fwd_r_20d is None else round(s.fwd_r_20d, 6),
                    "fwd_r_60d_gross": None if s.fwd_r_60d is None else round(s.fwd_r_60d, 6),
                }
            )
    return out, refused


def _stock_rows(signals: Sequence[SignalRow], trades: Sequence[_Trade]) -> list[dict[str, Any]]:
    by_signal = Counter(s.features.get("symbol") or s.instrument_id for s in signals)
    industry = {
        s.features.get("symbol") or s.instrument_id: s.features.get("industry") for s in signals
    }
    traded: dict[str, list[_Trade]] = defaultdict(list)
    for t in trades:
        traded[t.features.get("symbol") or t.row.instrument_id].append(t)
    out = []
    for symbol in sorted(set(by_signal) | set(traded)):
        mine = traded.get(symbol, [])
        rs = [t.row.r_multiple for t in mine]
        total = sum(rs, _ZERO)
        out.append(
            {
                "symbol": symbol,
                "industry": industry.get(symbol),
                "cap_bucket_mode": None,
                "signals": by_signal.get(symbol, 0),
                "trades": len(mine),
                "wins": sum(1 for r in rs if r > 0),
                "net_pnl": sum((t.row.realized_pnl for t in mine), _ZERO),
                "total_r": total,
                "expectancy_r": rounded(total / len(rs)) if rs else None,
                "best_trade_r": max(rs) if rs else None,
                "worst_trade_r": min(rs) if rs else None,
                "stopouts": sum(1 for t in mine if t.exit == "initial_stop"),
                "avg_hold_sessions": rounded(mean_of([t.hold for t in mine])),
                "first_trade": min((t.row.entry_date for t in mine), default=None),
                "last_trade": max((t.row.entry_date for t in mine), default=None),
            }
        )
    return out


def _by_playbook(
    signals: Sequence[SignalRow], codes: Mapping[int, str], trades: Sequence[_Trade]
) -> dict[str, Any]:
    """Per playbook: its signals, its refusals by code and its trades' bucket."""
    names = sorted({s.playbook for s in signals} | {t.row.playbook for t in trades})
    out: dict[str, Any] = {}
    for name in names:
        refused = Counter(code for n, code in codes.items() if signals[n].playbook == name)
        out[name] = {
            "signals": sum(1 for s in signals if s.playbook == name),
            "refusals": dict(sorted(refused.items())),
            "trades": _bucket([t for t in trades if t.row.playbook == name]),
        }
    return out


def build(
    run_id: str, result: BacktestResult, inputs: RunInputs
) -> tuple[dict[str, Any], dict[str, str]]:
    """A completed run's `run.json` document and its CSV files' text, by name. A refusal
    reason the standard has no code for fails the run (`standard.refusal_code`)."""
    signals = [s for s in result.signals if s.reason not in NOT_A_SIGNAL]
    codes = {
        n: refusal_code(s.reason or "", s.open_positions or 0, inputs.policy_max_positions)
        for n, s in enumerate(signals)
        if s.qty == 0
    }
    days = [s.day for s in result.sessions]
    trades = _trades(result, days)
    trade_of = {(t.row.instrument_id, t.row.entry_date): t.trade_id for t in trades}
    signal_rows, refusal_rows = _signal_rows(signals, codes, trade_of)
    keys = [
        (t.features.get("symbol") or t.row.instrument_id, t.row.entry_date, t.row.exit_date)
        for t in trades
    ]
    bench = _closes_on(inputs.benchmark)

    def regime(t: _Trade) -> str:
        on = None if t.signal is None else t.signal.regime_on
        if on is None:
            return "index_unknown"
        return "index_on" if on else "index_off"

    def liquidity(t: _Trade) -> str | None:
        return t.features.get("liquidity_bucket")

    by_regime = {
        **_group(trades, regime),
        **_group(
            trades, lambda t: breadth_bucket(None if t.signal is None else t.signal.breadth_pct)
        ),
    }
    doc = {
        "format_version": FORMAT_VERSION,
        "identity": dict(inputs.identity),
        "data": dict(inputs.data),
        "config": dict(inputs.config),
        "metrics": _metrics(result, trades, len(signals), inputs),
        "refusals": dict(sorted(Counter(codes.values()).items())),
        "binding": binding(keys, inputs.base_trades),
        "breakdowns": {
            "by_year": _yearly(result, inputs.start_capital, trades, inputs.benchmark),
            "by_regime": by_regime,
            "by_exit_reason": _group(trades, lambda t: t.exit),
            "by_industry": _group(trades, lambda t: t.features.get("industry")),
            "by_cap_bucket": _group(trades, lambda t: t.features.get("cap_bucket")),
            "by_liquidity_bucket": _group(trades, liquidity),
            "by_playbook": _by_playbook(signals, codes, trades),
            **_episodic(result, trades),
        },
        "funnel": dict(result.funnel),
    }
    csvs = {
        "trades": _csv(TRADE_COLUMNS, _trade_rows(run_id, trades, days)),
        "equity": _csv(EQUITY_COLUMNS, _equity_rows(result, inputs.start_capital, bench)),
        "refusals": _csv(REFUSAL_COLUMNS, refusal_rows),
        "signals": _csv(SIGNAL_COLUMNS, signal_rows),
        "stocks": _csv(STOCK_ROW_COLUMNS, _stock_rows(signals, trades)),
    }
    if result.ep_candidates:
        csvs["ep_candidates"] = _csv(EP_CANDIDATE_COLUMNS, result.ep_candidates)
    if result.rebalances:
        csvs["rebalances"] = _csv(REBALANCE_COLUMNS, result.rebalances)
    if result.overlay:
        doc["overlay"] = dict(result.overlay)
        csvs["overlay"] = _csv(
            OVERLAY_CSV_HEADER,
            (dict(zip(OVERLAY_CSV_HEADER, row, strict=True)) for row in result.overlay_rows),
        )
    return doc, csvs


def _episodic(result: BacktestResult, trades: Sequence[_Trade]) -> dict[str, Any]:
    """The C3 breakdowns of each pre-open playbook's trades (`ep_log.BREAKDOWNS`, by exit
    reason with its average hold), under `episodic_pivot`; nothing for a run without one (no EP
    log)."""
    names = sorted({str(r["playbook"]) for r in result.ep_candidates})
    if not names:
        return {}
    out: dict[str, Any] = {}
    for name in names:
        mine = [t for t in trades if t.row.playbook == name]
        block: dict[str, Any] = {"trades": _bucket(mine)}
        for key, column, rule in ep_log.BREAKDOWNS:

            def value(t: _Trade, column: str = column, rule: Any = rule) -> str | None:  # noqa: ANN401
                cell = t.features.get(column)
                if rule is not None:
                    return rule(cell)
                return "unknown" if cell is None else cell

            block[key] = _group(mine, value)
        block["by_exit_reason"] = _group(mine, lambda t: t.exit)
        out[name] = block
    return {"episodic_pivot": out}


EMPTY_METRICS: dict[str, Any] = {"trades": 0, "signals": 0}


def failed_run(inputs: RunInputs) -> dict[str, Any]:
    """The `run.json` of a run that did not complete (its `identity.status` and `error` say
    why): no metrics, refusals or breakdowns."""
    required = _validator().schema["properties"]["metrics"]["required"]
    return {
        "format_version": FORMAT_VERSION,
        "identity": dict(inputs.identity),
        "data": dict(inputs.data),
        "config": dict(inputs.config),
        "metrics": {k: EMPTY_METRICS.get(k) for k in required},
        "refusals": {},
        "binding": {"trades_changed": None, "trade_list_hash": None, "no_op": None},
        "breakdowns": {
            "by_year": [],
            "by_regime": {},
            "by_exit_reason": {},
            "by_industry": {},
            "by_cap_bucket": {},
            "by_liquidity_bucket": {},
            "by_playbook": {},
        },
    }

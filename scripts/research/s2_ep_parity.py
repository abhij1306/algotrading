"""S2 episodic-pivot parity (accuracy plan W2 B6), two modes on one portfolio file (p6):

`reconcile`: the engine's EP candidates (its EP log: every gap of the scan floor or more by a
member passing the bot's universe) against the EP study's gap events (`ep_study_events`, the
study's own definition), event by event over the window, each difference with its cause:

- `membership`: not a NIFTY 500 member on the day (engine: point-in-time intervals by chain);
- `adjustment`: the gap clears 6% on one price basis only (the study's dividend-adjusted open
  over its previous row, the engine's split-adjusted open over its previous stored close);
- `prev_close_basis`: the previous close is another session (the study's previous joined row,
  the engine's previous stored bar; the engine also needs a bar on the previous session);
- `liquidity`: the 20-session traded value (engine: the 20 bars through the previous session,
  all present, at least Rs 10 cr; study: the 20 rows before the day, at least 15, at least Rs 10
  cr);
- `adr_gate`: the bot universe's ADR20 at least 4% (engine only, a config rule);
- `history`: fewer than 64 earlier bars (both sides, counted differently);
- `ex_date` / `locked`: the study drops corporate-action ex-dates and days frozen all day;
- `series` / `not_stock`: series or ISIN class (engine only).
Earnings are reconciled on the common gaps: the study's flag (a filing whose first 09:15 open
on or after it is the day, or a results board meeting that session or the one before) against
the engine's `earnings_catalyst` (the newest filing public after the previous session's 09:15
and by the trigger, with a defined sales growth), each difference with its cause.

`parity`: a separate, simple implementation of the EP rules (`s2_ep_independent.EpBook`) reproduces
the engine's candidates (with the gate each failed) and its trades.

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \\
        venv/bin/python scripts/research/s2_ep_parity.py {reconcile,parity} \\
        --portfolio p6-ep-a.yaml --from 2022-01-01 --to 2024-12-31 [--fresh] [--out NAME] \\
        [--board-meetings DIR]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
import parity_common as pc  # noqa: E402
import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.backtest.data import MarketData, symbol_on  # noqa: E402
from app.hat.data.isin import ordinary_equity  # noqa: E402
from app.hat.data.store import DataStore  # noqa: E402
from app.hat.models import HatEvent  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402

GAP_MIN = 6.0
ADR_MIN = 4.0
TV_MIN = 1e8
HISTORY = 64


# ---------------------------------------------------------------------------------------------
# the engine's view of one instrument-day, recomputed from the loaded market


def engine_inputs(data: MarketData) -> pd.DataFrame:
    """Per (chain id, session): the price-basis gap, the previous stored session, whether the
    previous market session has a bar, the turnover and ADR20 through the previous bar, the
    count of earlier bars since the series break."""
    bars = data.bars.select(
        "instrument_id",
        "date",
        "open",
        "high",
        "low",
        "close",
        "traded_value",
        "volume",
        "div_factor",
    ).to_pandas()
    bars = bars.sort_values(["instrument_id", "date"]).reset_index(drop=True)
    g = bars.groupby("instrument_id", sort=False)
    bars["prev_date"] = g.date.shift(1)
    bars["prev_close"] = g.close.shift(1)
    gap_days = (pd.to_datetime(bars.date) - pd.to_datetime(bars.prev_date)).dt.days
    bars["seg"] = (gap_days.isna() | (gap_days > 92)).astype(int)
    bars["seg"] = bars.groupby("instrument_id").seg.cumsum()
    gs = bars.groupby(["instrument_id", "seg"], sort=False)
    bars["n_before"] = gs.cumcount()  # bars before this one in its segment
    tr_close = bars.close * bars.div_factor
    tv = bars.traded_value.where(bars.traded_value.notna(), tr_close * bars.volume)
    bars["tv"] = tv
    bars["adr1"] = 100 * (bars.high / bars.low - 1)
    # the windows end on the previous bar: shift by one inside the segment
    roll = lambda s, n: s.rolling(n, min_periods=n).mean()  # noqa: E731
    bars["tv20_prev"] = gs.tv.transform(lambda s: roll(s, 20).shift(1))
    bars["adr20_prev"] = gs.adr1.transform(lambda s: roll(s, 20).shift(1))
    bars["gap_engine"] = 100 * (bars.open / bars.prev_close - 1)
    days = list(data.days)
    prev_session = {d: days[n - 1] for n, d in enumerate(days) if n}
    bars["prev_session"] = bars.date.map(prev_session)
    bars["bar_on_prev"] = bars.prev_date == bars.prev_session
    return bars


def member_on(data: MarketData) -> Any:  # noqa: ANN401
    spans: dict[str, list[tuple[date, date | None]]] = {}
    for m in data.members:
        spans.setdefault(str(m.instrument_id), []).append((m.effective_from, m.effective_to))

    def on(iid: str, day: date) -> bool:
        return any(f <= day and (t is None or day < t) for f, t in spans.get(iid, ()))

    return on


def series_on(points: Sequence[tuple[date, str]], day: date) -> str | None:
    found = None
    for first, series in points:
        if first <= day:
            found = series
    return found


def engine_cause(row: Mapping[str, Any], data: MarketData, on: Any) -> str:  # noqa: ANN401
    """Why the engine's gap scan did not log a study event (its first failing condition)."""
    iid, day = row["iid"], row["day"]
    if not on(iid, day):
        return "membership"
    if row.get("gap_engine") is None or (
        isinstance(row["gap_engine"], float) and math.isnan(row["gap_engine"])
    ):
        return "data_gap"
    if not row["bar_on_prev"]:
        return "prev_close_basis"
    if row["gap_engine"] < GAP_MIN:
        return "adjustment"
    prev = row["prev_session"]
    if series_on(data.series.get(iid, ()), prev) not in ("EQ", "BE"):
        return "series"
    if not ordinary_equity(data.isins.get(iid)):
        return "not_stock"
    tv = row["tv20_prev"]
    if tv is None or (isinstance(tv, float) and math.isnan(tv)) or tv < TV_MIN:
        return "liquidity"
    adr = row["adr20_prev"]
    if adr is None or (isinstance(adr, float) and math.isnan(adr)) or adr < ADR_MIN:
        return "adr_gate"
    if row["n_before"] < HISTORY:
        return "history"
    return "unexplained"


def study_cause(row: Mapping[str, Any]) -> str:
    """Why the study has no event on a day the engine logged (its first failing condition)."""
    if row is None:
        return "no_study_row"
    if not row["member"]:
        return "membership"
    if row["exdate"]:
        return "ex_date"
    if row["locked"]:
        return "locked"
    if not (row["adv20"] >= TV_MIN):
        return "liquidity"
    if not row["ret63_ok"]:
        return "history"
    if not (row["gap"] >= GAP_MIN / 100):
        return "adjustment"
    return "unexplained"


def reconcile(
    data: MarketData,
    result: Any,  # noqa: ANN401
    start: date,
    end: date,
    board_meetings: Path,
) -> dict[str, Any]:
    import ep_study_events

    settings = operator_settings()
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        panel = ep_study_events.study_events(
            store, session, start, end, board_meetings, GAP_MIN / 100
        )
    panel["day"] = panel.d.dt.date
    panel["iid"] = panel.cid.astype(str)
    study = panel[panel.event].set_index(["iid", "day"])
    rows = panel.set_index(["iid", "day"])
    rows = rows[~rows.index.duplicated()]
    eng = pd.DataFrame(list(result.ep_candidates))
    eng["day"] = pd.to_datetime(eng.trade_date).dt.date
    eng["iid"] = eng.instrument_id.astype(str)
    eng = eng.set_index(["iid", "day"])
    inputs = engine_inputs(data)
    inputs["day"] = pd.to_datetime(inputs.date).dt.date
    inputs["iid"] = inputs.instrument_id.astype(str)
    inputs = inputs.set_index(["iid", "day"])
    on = member_on(data)
    out: dict[str, Any] = {"study_events": len(study), "engine_candidates": len(eng)}
    by_year = Counter()
    for key in study.index:
        by_year[("study", key[1].year)] += 1
    for key in eng.index:
        by_year[("engine", key[1].year)] += 1
    out["by_year"] = {f"{s}_{y}": n for (s, y), n in sorted(by_year.items())}
    study_only, engine_only = [], []
    for key in study.index.difference(eng.index):
        found = inputs.loc[key].to_dict() if key in inputs.index else {}
        cause = (
            engine_cause({**found, "iid": key[0], "day": key[1]}, data, on) if found else "data_gap"
        )
        study_only.append((key, cause, float(study.loc[key, "gap"]) * 100, found.get("gap_engine")))
    for key in eng.index.difference(study.index):
        found = rows.loc[key].to_dict() if key in rows.index else None
        engine_only.append((key, study_cause(found), float(eng.loc[key, "gap_pct"])))
    out["study_only_by_cause"] = dict(Counter(c for _, c, *_ in study_only).most_common())
    out["engine_only_by_cause"] = dict(Counter(c for _, c, *_ in engine_only).most_common())

    def sym(key: tuple[str, date]) -> str:
        return f"{key[1]} {symbol_on(data, key[0], key[1])}"

    out["study_only"] = [
        (sym(k), c, round(g, 3), None if e is None else round(float(e), 3))
        for k, c, g, e in study_only
    ]
    out["engine_only"] = [(sym(k), c, round(g, 3)) for k, c, g in engine_only]
    # earnings on the common gaps
    common = study.index.intersection(eng.index)
    earn = []
    for key in common:
        s = study.loc[key]
        e = eng.loc[key]
        engine_earn = not (e["failed_gate"] == "earnings_catalyst")
        engine_earn = engine_earn and e["reached"] not in ("gap_up", "neglect", "gap_scan")
        earn.append(
            (key, bool(s["E"]), bool(s["earn"]), bool(s["earn_bm"]), engine_earn, e["reason"])
        )
    out["common"] = len(common)
    out["earnings"] = {
        "study_E": sum(1 for x in earn if x[1]),
        "engine_catalyst": sum(1 for x in earn if x[4]),
        "both": sum(1 for x in earn if x[1] and x[4]),
    }
    out["earnings_study_only"] = [
        (sym(k), "board_meeting_only" if bm and not f else "filing", reason)
        for k, E, f, bm, ee, reason in earn
        if E and not ee
    ]
    out["earnings_engine_only"] = [
        (sym(k), reason) for k, E, f, bm, ee, reason in earn if ee and not E
    ]
    out["study_earnings_events"] = int(study.E.sum())
    out["study_earnings_by_year"] = {
        int(y): int(n)
        for y, n in study[study.E]
        .reset_index()
        .groupby(pd.to_datetime(study[study.E].reset_index().day).dt.year)
        .size()
        .items()
    }

    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("reconcile", "parity"))
    parser.add_argument("--portfolio", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--board-meetings",
        type=Path,
        help="reconcile: the folder of the study's board_meetings_*.csv files",
    )
    args = parser.parse_args(argv)
    text_ = args.portfolio.read_text(encoding="utf-8")
    spec, data, result = pc.engine_and_market(
        yaml.safe_load(text_), text_, args.start, args.end, fresh=args.fresh
    )
    if args.mode == "reconcile":
        if args.board_meetings is None:
            parser.error("reconcile needs --board-meetings")
        report = reconcile(data, result, args.start, args.end, args.board_meetings)
    else:
        import s2_ep_independent

        report = s2_ep_independent.run_and_compare(spec, data, result)
    summary = {k: v for k, v in report.items() if not isinstance(v, list)}
    print(json.dumps(summary, indent=1, default=str))
    if args.out:
        pc.REPORTS.mkdir(parents=True, exist_ok=True)
        (pc.REPORTS / args.out).write_text(json.dumps(report, indent=1, default=str) + "\n")
    return 0 if report.get("passed", True) else 1


if __name__ == "__main__":
    sys.exit(main())

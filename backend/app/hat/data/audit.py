"""The data audit a backtest runs before it starts (docs/backtests/process.md): deterministic
checks of the market a run reads, over its window and universe, written as one JSON document with
a digest, a status (`pass`, `warn` or `fail`) and the findings.

    python -m app.hat.data.audit --from YYYY-MM-DD --to YYYY-MM-DD --universe <id|name>
        [--index NAME] [--refresh]

The checks (each a pure function of plain inputs; `gather` reads them):
- `calendar`: the store's sessions in the window against the NSE trading calendar: a stored day
  that is not a session, or a session the store has no bars for, fails.
- `missing_sessions`: each member's sessions without a bar while it is a member and listed (from
  its first to its last bar), gap counts and the longest gap per symbol.
- `survivorship`: the point-in-time universe on sample dates (each year's first session and the
  window's first) holds members that later left the index or delisted, and each such member has
  bars on its member days. A window of a year or more whose samples hold no leaver fails: that
  universe is today's list.
- `corporate_actions`: the adjusted bars around each change of the adjustment factor (an ex-date
  of a split, bonus, rights issue or demerger), and every other session, have no close-to-close
  move beyond `JUMP_PCT`.
- `lookahead`: a sample of the stored features of day D recomputed from the bars dated on or
  before D alone (`features.compute_features`), and a sample of the stored breadth of D recounted
  from D's closes and stored SMA50s; and every session's gate inputs, which the engine reads from
  the session before (the scan, the regime gate and the breadth gate of trade date D read D-1).
- `unique_keys`: one bar per (instrument, date) from one ISIN in an ingest run, one features row
  per (instrument, date), one close per index and date.
- `bars_5m`: every stored 5-minute day (the whole store, not the run's window): its bars start on
  the 5-minute grid inside the IST session of their own day, it has a raw daily bar (its ISIN's
  or its chain's), and its last close is within `CLOSE_5M_PCT` of the daily close; a day off by
  the product of the stored price factors after it was stored on the adjusted basis
  (`bar_5m_adjusted_basis`). Findings warn
  while a run reads no 5-minute bars, and fail when it does.
- `industries`: the share of the run's member-days with no industry in force
  (`industry_history`): above `INDUSTRY_UNKNOWN_PCT` it warns. run.json `data` carries the share.
  The audit is shared by a sweep's variants, so the cap is judged per run, not here: a run whose
  policy caps positions per industry below its open positions while any member-day is unknown
  is refused (`industry_cap_refused`), as every unknown name would count as one industry.
- `demergers`: each demerger on a member chain from the history's first session to the period's
  end whose ex-day open gap and close gap (over NSE's previous close) differ by more than
  `DEMERGER_GAP_PCT`, and whose factor is not the close gap (no correction took it there), warns;
  the summary counts the member demergers in the period with no `announced_on` (the run cannot
  exit before them, `demerger_notice.exit_day`), which run.json `data` carries too.
- `verify`: the data verification report covering the window (`data/verify`, `status_for`): no
  report, a blocking check of it that failed or did not cover the window, or data changed since
  it was judged warns `verify_report_missing`, `verify_blocking_failed`,
  `verify_blocking_not_run` or `verify_report_stale`, and the run is unverified (run.json
  `data.verified`); a run is not refused for it.
- `golden`: the newest golden sample (`data/golden.py`, docs/backtests/golden-sample.md) looked up
  in the current stores: a drifted or missing value fails, as does an unreadable golden file; an
  unresolvable key, a recorded mismatch still open, or no golden file yet warns.

A run's audit is kept under `HAT_DATA_ROOT/backtests/data_audits/` by its data snapshot, window,
use of 5-minute bars, golden file and verification report (`cache_key`), so the variants of a
sweep, and later runs on the same data, read it instead of recomputing it. A run refuses to start on
`fail` (`backtest_data_audit_failed`). The owner explains a `warn` once, beside it, as
`<digest>.explained.md` (`explanation_of`); the run audit and the review read it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction, HatInstrument, HatMarket, HatTradingCalendar
from . import golden
from .bars_store import BarStore
from .chains import stored_adjustment
from .features import ABOVE_MARGIN, compute_features
from .industry_history import unknown_member_days
from .isin import ORDINARY_EQUITY
from .sessions import SESSION_CLOSE, SESSION_OPEN
from .store import (
    DATASET_FEATURES,
    FEATURE_NAMES,
    Adjustment,
    DataStore,
    latest_rows,
    newest,
)

if TYPE_CHECKING:  # the market a run reads; `gather` takes it from the caller
    from ..backtest.data import MarketData

__all__ = [
    "AUDIT_VERSION",
    "FAIL",
    "PASS",
    "WARN",
    "audit_doc",
    "cache_key",
    "cached",
    "check_bars_5m",
    "check_breadth",
    "check_calendar",
    "check_corporate_actions",
    "check_demergers",
    "check_features",
    "check_golden",
    "check_industries",
    "check_gate_inputs",
    "check_missing_sessions",
    "check_survivorship",
    "check_result",
    "check_unique_keys",
    "check_verify",
    "gather",
    "industry_cap_refused",
    "read_cached",
    "run_counts",
]

AUDIT_VERSION = "4"  # a change to a check or a threshold changes it, so no stale audit is read
PASS, WARN, FAIL, SKIPPED = "pass", "warn", "fail", "skipped"
_RANK = {PASS: 0, SKIPPED: 0, WARN: 1, FAIL: 2}
# Findings listed per check; the counts cover them all.
MAX_LISTED = 25
# A member missing more than this share of its listed member sessions warns: an NSE suspension
# of a NIFTY 500 stock is rare and short, so more points to an ingest gap. Over the universe as a
# whole, more than `MISSING_FAIL_PCT` fails: the run would read a different market.
MISSING_WARN_PCT = 5.0
MISSING_FAIL_PCT = 2.0
# A leaver with bars on fewer of its member days than this warns (its gaps bias the run).
SURVIVOR_COVER_PCT = 95.0
SURVIVOR_MIN_DAYS = 365  # a window this long holds index changes; none means today's list
# NSE's widest daily price band for an equity is 20% of the previous close (on an ex-date, of the
# adjusted base price), so an adjusted close-to-close move beyond it between two adjacent sessions
# is an action the adjustment missed, or one of the few stocks whose band was relaxed that day.
JUMP_PCT = 20.0
# The price ratios (new over old) of the common splits and bonuses: 2, 3, 4, 5 and 10 for 1
# (a 1:1 bonus halves too), and a raw move within the slack of one (the day's own move on top) is
# shaped like an action. A 1:2 bonus's 2/3 is left out: on NIFTY 500 2016-2026 every
# unadjusted move near -33% was a real crash; a 1:4 bonus's 4/5 is inside the band.
ACTION_RATIOS = (1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10)
ACTION_RATIO_SLACK = 0.03
FACTOR_STEP = 1e-6  # a relative change of the adjustment factor this large is an ex-date
# Feature samples: sessions spread over the window, instruments per session.
SAMPLE_DAYS = 12
SAMPLE_INSTRUMENTS = 8
BREADTH_DAYS = 8
# NSE's daily close is the last half hour's weighted average, so a day's last 5-minute close
# differs from it a little; more than this is a day on another price basis or a broken day.
CLOSE_5M_PCT = 5.0
INDUSTRY_UNKNOWN_PCT = 5.0
# A demerger factor is the ex-day open gap (CA6); a close gap this far from it marks a thin open.
DEMERGER_GAP_PCT = 15.0
CLOSE_GAP_TOLERANCE = 0.01  # a factor this close to the close gap was corrected to it
FEATURE_TOLERANCE = 1e-9  # relative; a rolling mean's last bits depend on where its frame starts
# The features of one instrument's own rows; the percentile columns rank the whole market.
PER_INSTRUMENT = tuple(n for n in FEATURE_NAMES if n not in {"rs_pct", "mom_rank"})


def check_result(
    name: str, summary: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]
) -> dict:
    """A check's result: its status is its worst finding's severity."""
    status = max((f["severity"] for f in findings), key=_RANK.__getitem__, default=PASS)
    return {"name": name, "status": status, "summary": dict(summary), "findings": list(findings)}


def finding(code: str, severity: str, items: Sequence[Any], **extra: object) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "count": len(items),
        "items": list(items[:MAX_LISTED]),
        **extra,
    }


def _iso(day: date) -> str:
    return day.isoformat()


# --- calendar and gaps -------------------------------------------------------------------------


def check_calendar(
    store_days: Sequence[date], trading: Iterable[date], start: date, end: date
) -> dict[str, Any]:
    """The store's sessions in `start..end` against the calendar's trading days (`trading`), up
    to the store's last day there: a stored day that is no session (a date shifted off IST, a
    holiday row) fails, as does a session the store has none of. A period ending after the
    store's last day warns: the run stops at the data."""
    window = [d for d in store_days if start <= d <= end]
    last = window[-1] if window else start
    sessions = {d for d in trading if start <= d <= last}
    stored = set(window)
    findings = []
    extra = sorted(stored - sessions)
    if extra:
        findings.append(finding("store_day_not_session", FAIL, [_iso(d) for d in extra]))
    missing = sorted(sessions - stored)
    if missing:
        findings.append(finding("session_missing_from_store", FAIL, [_iso(d) for d in missing]))
    if window and end > last and any(d > last for d in trading if d <= end):
        findings.append(finding("data_ends_before_period", WARN, [_iso(last)]))
    return check_result(
        "calendar",
        {"sessions": len(sessions), "stored": len(window), "store_last": _iso(last)},
        findings,
    )


def _member_days(
    sessions: Sequence[date], spans: Sequence[tuple[date, date | None]], first: date, last: date
) -> list[date]:
    """The `sessions` (sorted) inside any of `spans` ([from, to)) and inside `first..last`."""
    out: set[date] = set()
    for lo, hi in spans:
        a = bisect_left(sessions, max(lo, first))
        b = (
            bisect_right(sessions, last)
            if hi is None
            else bisect_left(sessions, min(hi, last + timedelta(days=1)))
        )
        out.update(sessions[a:b])
    return sorted(out)


def _longest_run(missing: Sequence[date], sessions: Sequence[date]) -> int:
    """The longest run of consecutive `sessions` among `missing` (both sorted)."""
    index = {d: n for n, d in enumerate(sessions)}
    best = run = 0
    previous = None
    for day in missing:
        n = index[day]
        run = run + 1 if previous is not None and n == previous + 1 else 1
        best = max(best, run)
        previous = n
    return best


def check_missing_sessions(
    sessions: Sequence[date],
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    bar_days: Mapping[str, Sequence[date]],
    names: Mapping[str, str],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Each member's sessions in `start..end` without a bar while it is a member (`spans`, its
    [from, to) intervals) and listed (from its first to its last bar, `bar_days` sorted):
    per symbol the gap count and the longest gap. A member missing more than `MISSING_WARN_PCT`
    of its sessions warns; the universe missing more than `MISSING_FAIL_PCT` fails."""
    sessions = sorted(d for d in sessions if start <= d <= end)
    total = gaps = 0
    rows = []
    for instrument_id, member_spans in sorted(spans.items()):
        days = bar_days.get(instrument_id, ())
        if not days or not sessions:
            continue  # never listed in the window: `survivorship` reads it
        wanted = _member_days(sessions, member_spans, days[0], days[-1])
        held = set(days)
        missing = [d for d in wanted if d not in held]
        total += len(wanted)
        gaps += len(missing)
        if missing:
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "symbol": names.get(instrument_id, instrument_id),
                    "member_sessions": len(wanted),
                    "missing": len(missing),
                    "missing_pct": round(100 * len(missing) / len(wanted), 2),
                    "longest_gap": _longest_run(missing, sessions),
                    "first_missing": _iso(missing[0]),
                }
            )
    rows.sort(key=lambda r: (-r["missing_pct"], r["symbol"]))
    findings = []
    heavy = [r for r in rows if r["missing_pct"] > MISSING_WARN_PCT]
    if heavy:
        findings.append(finding("member_sessions_missing", WARN, heavy))
    share = 100 * gaps / total if total else 0.0
    if share > MISSING_FAIL_PCT:
        findings.append(finding("universe_sessions_missing", FAIL, [round(share, 3)]))
    summary = {
        "member_sessions": total,
        "missing": gaps,
        "missing_pct": round(share, 4),
        "symbols_with_gaps": len(rows),
        "worst": rows[:MAX_LISTED],
    }
    return check_result("missing_sessions", summary, findings)


def _member_on(spans: Sequence[tuple[date, date | None]], day: date) -> bool:
    return any(lo <= day and (hi is None or hi > day) for lo, hi in spans)


def _left_by(spans: Sequence[tuple[date, date | None]], day: date, end: date) -> bool:
    """Whether every membership interval open after `day` ends by `end`."""
    return all(hi is not None and hi <= end for _, hi in spans if hi is None or hi > day)


def _year_starts(sessions: Sequence[date]) -> list[date]:
    """The first of sorted `sessions` and each later year's first session."""
    return [d for n, d in enumerate(sessions) if n == 0 or d.year != sessions[n - 1].year]


def check_survivorship(
    sessions: Sequence[date],
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    bar_days: Mapping[str, Sequence[date]],
    last_traded: Mapping[str, date],
    names: Mapping[str, str],
    start: date,
    end: date,
) -> dict[str, Any]:
    """The point-in-time universe on each sample date (the window's first session and each later
    year's first): how many of its members later left the index (an interval ending by `end`)
    or delisted (`last_traded`), and whether each leaver has bars on its member days up to its
    last traded session. A leaver without a bar there fails, one below `SURVIVOR_COVER_PCT`
    warns, and a window of `SURVIVOR_MIN_DAYS` or more whose samples hold no leaver fails."""
    sessions = sorted(d for d in sessions if start <= d <= end)
    if not sessions:
        return check_result("survivorship", {"samples": []}, [])
    rows, leavers = [], set()
    for day in _year_starts(sessions):
        members = [i for i, s in spans.items() if _member_on(s, day)]
        left = [i for i in members if i in last_traded or _left_by(spans[i], day, end)]
        leavers.update(left)
        rows.append({"date": _iso(day), "members": len(members), "later_left": len(left)})
    empty, thin = [], []
    for instrument_id in sorted(leavers):
        stop = min(end, last_traded.get(instrument_id, end))
        wanted = _member_days(sessions, spans[instrument_id], start, stop)
        held = set(bar_days.get(instrument_id, ()))
        have = sum(1 for d in wanted if d in held)
        row = {
            "instrument_id": instrument_id,
            "symbol": names.get(instrument_id, instrument_id),
            "member_sessions": len(wanted),
            "with_bars": have,
        }
        if wanted and have == 0:
            empty.append(row)
        elif wanted and 100 * have / len(wanted) < SURVIVOR_COVER_PCT:
            thin.append(row)
    findings = []
    if empty:
        findings.append(finding("leaver_without_bars", FAIL, empty))
    if thin:
        findings.append(finding("leaver_bars_thin", WARN, thin))
    if not leavers and (end - start).days >= SURVIVOR_MIN_DAYS:
        findings.append(finding("no_leavers", FAIL, [r["date"] for r in rows]))
    summary = {"samples": rows, "leavers": len(leavers)}
    return check_result("survivorship", summary, findings)


# --- corporate actions -------------------------------------------------------------------------


def jumps(
    days: Sequence[date],
    closes: Sequence[float],
    raw: Sequence[float],
    sessions: Mapping[date, int],
) -> list[dict[str, Any]]:
    """One instrument's adjusted close-to-close moves beyond `JUMP_PCT` between bars of adjacent
    sessions (`sessions`: each store session's position), each with whether the adjustment
    factor (adjusted over raw close) changed there, i.e. an ex-date."""
    out = []
    for n in range(1, len(days)):
        a, b = closes[n - 1], closes[n]
        if not (a > 0 and b > 0 and raw[n - 1] > 0 and raw[n] > 0):
            continue
        if sessions.get(days[n], -2) - sessions.get(days[n - 1], -9) != 1:
            continue  # a gap: the move spans sessions the bars do not show
        move = 100 * (b / a - 1)
        if abs(move) <= JUMP_PCT:
            continue
        before, after = a / raw[n - 1], b / raw[n]
        out.append(
            {
                "date": _iso(days[n]),
                "move_pct": round(move, 2),
                "raw_move_pct": round(100 * (raw[n] / raw[n - 1] - 1), 2),
                "ex_date": abs(after / before - 1) > FACTOR_STEP,
            }
        )
    return out


def action_shaped(raw_move_pct: float) -> bool:
    """Whether a raw close-to-close move is within `ACTION_RATIO_SLACK` of a split's or bonus's
    price ratio (`ACTION_RATIOS`)."""
    ratio = 1 + raw_move_pct / 100
    return any(abs(ratio / r - 1) <= ACTION_RATIO_SLACK for r in ACTION_RATIOS)


def _factor_changes(closes: Sequence[float], raw: Sequence[float]) -> tuple[bool, int]:
    """Whether every adjustment factor (adjusted over raw close) of a priced day is a positive
    number, and how many times the factor changes (its ex-dates)."""
    factors = [c / r if r > 0 else math.nan for c, r in zip(closes, raw, strict=True)]
    valid = all(math.isfinite(f) and f > 0 for f, c in zip(factors, closes, strict=True) if c > 0)
    changes = sum(
        1
        for n in range(1, len(factors))
        if factors[n - 1] > 0 and abs(factors[n] / factors[n - 1] - 1) > FACTOR_STEP
    )
    return valid, changes


def check_corporate_actions(
    series: Mapping[str, tuple[Sequence[date], Sequence[float], Sequence[float]]],
    sessions: Sequence[date],
    skipped: Iterable[tuple[str, date]],
    names: Mapping[str, str],
) -> dict[str, Any]:
    """Each instrument's adjusted series ((dates, adjusted closes, raw closes), sorted) around
    every change of its adjustment factor. An adjusted move beyond `JUMP_PCT` on an ex-date is
    an action the factor did not absorb (warn). On any other session such a move is counted and
    the largest listed (most are real moves of stocks without a fixed band), and it warns only
    when its raw move is action-shaped (`action_shaped`): a split or bonus with no factor. A
    factor that is not a positive number fails. A jump the run already leaves alone (`skipped`,
    the open review jumps) is counted, not listed."""
    position = {d: n for n, d in enumerate(sorted(sessions))}
    known = set(skipped)
    ex_dates = 0
    bad, at_ex, off_ex, shaped, excused = [], [], [], [], 0
    for instrument_id, (days, closes, raw) in sorted(series.items()):
        symbol = {"instrument_id": instrument_id, "symbol": names.get(instrument_id, instrument_id)}
        valid, changes = _factor_changes(closes, raw)
        if not valid:
            bad.append(symbol)
        ex_dates += changes
        for jump in jumps(days, closes, raw, position):
            if (instrument_id, date.fromisoformat(jump["date"])) in known:
                excused += 1
                continue
            row = {**symbol, **jump}
            if jump["ex_date"]:
                at_ex.append(row)
                continue
            off_ex.append(row)
            if action_shaped(jump["raw_move_pct"]):
                shaped.append(row)
    findings = [
        finding(code, severity, items, **extra)
        for code, severity, items, extra in (
            ("bad_adjustment_factor", FAIL, bad, {}),
            ("ex_date_jump", WARN, at_ex, {"threshold_pct": JUMP_PCT}),
            ("action_shaped_jump", WARN, shaped, {}),
        )
        if items
    ]
    off_ex.sort(key=lambda r: -abs(r["move_pct"]))
    summary = {
        "instruments": len(series),
        "ex_dates": ex_dates,
        "jumps_at_ex_date": len(at_ex),
        "jumps_elsewhere": len(off_ex),
        "jumps_already_skipped": excused,
        "threshold_pct": JUMP_PCT,
        "largest_elsewhere": off_ex[:MAX_LISTED],
    }
    return check_result("corporate_actions", summary, findings)


# --- lookahead ---------------------------------------------------------------------------------


def _same(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    x, y = float(a), float(b)  # type: ignore[arg-type]
    if math.isnan(x) or math.isnan(y):
        return math.isnan(x) and math.isnan(y)
    return abs(x - y) <= FEATURE_TOLERANCE * max(1.0, abs(x), abs(y))


def check_features(
    samples: Sequence[tuple[str, date, Mapping[str, Any] | None, Mapping[str, Any] | None]],
) -> list[dict[str, Any]]:
    """Findings of the feature samples (instrument, day, stored row, row recomputed from the
    bars dated on or before the day): a per-instrument feature that differs read a later bar,
    or was written from other bars (fail); a member day with a bar but no stored row was never
    computed, and the run reads no features for it (fail)."""
    wrong, missing = [], []
    for instrument_id, day, stored, fresh in samples:
        if stored is None:
            missing.append({"instrument_id": instrument_id, "date": _iso(day)})
            continue
        off = (
            ["row"]
            if fresh is None
            else [n for n in PER_INSTRUMENT if not _same(stored.get(n), fresh.get(n))]
        )
        if off:
            wrong.append({"instrument_id": instrument_id, "date": _iso(day), "features": off})
    return [
        finding(code, FAIL, items)
        for code, items in (("feature_not_point_in_time", wrong), ("feature_row_missing", missing))
        if items
    ]


def recount_breadth(
    rows: Iterable[tuple[str | None, float | None, float | None]],
) -> tuple[float | None, int]:
    """(breadth %, stocks with an SMA50) from one day's (ISIN, close, stored SMA50) rows, as the
    features job counts it: the ordinary equity rows above their SMA50 (by the job's margin)
    over those that have one."""
    equity = re.compile(ORDINARY_EQUITY)
    with_sma = above = 0
    for isin, close, sma in rows:
        if not isin or not equity.match(isin) or sma is None or close is None:
            continue
        with_sma += 1
        above += close > sma * (1.0 + ABOVE_MARGIN)
    return (100.0 * above / with_sma if with_sma else None), with_sma


def check_breadth(
    samples: Sequence[tuple[date, float | None, float | None, int]],
) -> list[dict[str, Any]]:
    """Findings of the breadth samples (day, stored %, recounted %, stocks counted): a stored
    breadth more than one stock away from the recount was not counted from that day (fail)."""
    wrong = []
    for day, stored, fresh, counted in samples:
        if stored is None and fresh is None:
            continue
        if stored is None or fresh is None or abs(stored - fresh) > 100.0 / max(counted, 1) + 1e-9:
            wrong.append({"date": _iso(day), "stored": stored, "recounted": fresh})
    return [finding("breadth_not_point_in_time", FAIL, wrong)] if wrong else []


def check_gate_inputs(
    sessions: Sequence[date],
    index_days: Iterable[date],
    breadth_days: Iterable[date],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """The engine reads trade date D's scan, regime gate and breadth gate from the session
    before D: each session of `start..end` whose previous session has no regime index close or
    no breadth warns (the gate turns off that day, as live's would)."""
    ordered = sorted(sessions)
    closes, breadth = set(index_days), set(breadth_days)
    no_close, no_breadth = [], []
    for n, day in enumerate(ordered):
        if n == 0 or not start <= day <= end:
            continue
        prev = ordered[n - 1]
        if prev not in closes:
            no_close.append(_iso(day))
        if prev not in breadth:
            no_breadth.append(_iso(day))
    findings = []
    if no_close:
        findings.append(finding("no_index_close_before", WARN, no_close))
    if no_breadth:
        findings.append(finding("no_breadth_before", WARN, no_breadth))
    return findings


# --- keys and 5-minute bars --------------------------------------------------------------------


def check_unique_keys(
    bar_dupes: Sequence[Mapping[str, Any]],
    feature_keys: Iterable[tuple[str, date]],
    index_keys: Iterable[tuple[str, date]],
) -> dict[str, Any]:
    """One bar per (instrument, date) of one ISIN within an ingest run (`bar_dupes`: the keys
    that break it, from the store), one features row per (instrument, date) and one close per
    (index, date): any duplicate fails (the loader would keep one of them silently)."""
    findings = []
    if bar_dupes:
        findings.append(finding("duplicate_bar", FAIL, list(bar_dupes)))
    for code, keys in (
        ("duplicate_feature_row", feature_keys),
        ("duplicate_index_close", index_keys),
    ):
        seen: set[tuple[str, date]] = set()
        dupes = []
        for key in keys:
            if key in seen:
                dupes.append([key[0], _iso(key[1])])
            seen.add(key)
        if dupes:
            findings.append(finding(code, FAIL, dupes))
    return check_result("unique_keys", {"daily_dates": "date (IST trade date, no time)"}, findings)


@dataclass(frozen=True)
class Day5m:
    """One instrument's stored 5-minute day off its daily bar: its bars off the IST 5-minute
    grid, its last close, the raw daily close (None without a daily bar), the product of its
    chain's stored price factors with an ex-date after the day, and whether it is a member-day
    of the run in its period."""

    instrument_id: str
    day: date
    off_grid: int
    last_close: float
    daily_close: float | None
    later_factor: float
    member: bool


def basis_of(day: Day5m) -> str | None:
    """Why a 5-minute day is off its daily bar: `off_grid`, `no_daily`, `adjusted_basis` (its
    last close over the daily close is the later factors' product, within `CLOSE_5M_PCT`),
    `close_off` (more than `CLOSE_5M_PCT` off otherwise), or None when it fits."""
    if day.off_grid:
        return "off_grid"
    if not day.daily_close:
        return "no_daily"
    ratio = day.last_close / day.daily_close
    if abs(ratio - 1) * 100 <= CLOSE_5M_PCT:
        return None
    if day.later_factor != 1 and abs(ratio / day.later_factor - 1) * 100 <= CLOSE_5M_PCT:
        return "adjusted_basis"
    return "close_off"


def check_bars_5m(days: Iterable[Day5m], scanned: int, *, used: bool) -> dict[str, Any]:
    """The store-wide 5-minute scan (`scanned` days, `days` the ones off their daily bar): each
    kind (`basis_of`) is a finding, its run member-days listed first. A finding warns: a run
    reading 5-minute bars (`used`) decides such a day on the daily estimate instead
    (`backtest.intraday.on_basis`, the same rule), so no run reads bars off their day."""
    severity = WARN
    found: dict[str, list[Day5m]] = defaultdict(list)
    for d in days:
        kind = basis_of(d)
        if kind is not None:
            found[kind].append(d)
    findings, by_kind = [], {}
    for kind, code in (
        ("off_grid", "bar_5m_off_ist_grid"),
        ("no_daily", "bar_5m_no_daily"),
        ("adjusted_basis", "bar_5m_adjusted_basis"),
        ("close_off", "bar_5m_close_off_daily"),
    ):
        rows = sorted(found.get(kind, ()), key=lambda d: (not d.member, d.instrument_id, d.day))
        if not rows:
            continue
        by_kind[kind] = {"days": len(rows), "member_days": sum(d.member for d in rows)}
        items = [
            {
                "instrument_id": d.instrument_id,
                "date": _iso(d.day),
                "member": d.member,
                "ratio": None if not d.daily_close else round(d.last_close / d.daily_close, 4),
                "later_factor": round(d.later_factor, 6),
            }
            for d in rows
        ]
        findings.append(finding(code, severity, items, threshold_pct=CLOSE_5M_PCT))
    summary = {"days_scanned": scanned, "off": by_kind, "used_by_run": used}
    return check_result("bars_5m", summary, findings)


# --- industries and demergers ------------------------------------------------------------------


def check_industries(
    sessions: Sequence[date],
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    history: Mapping[str, Sequence[tuple[date, str]]],
    names: Mapping[str, str],
) -> dict[str, Any]:
    """The run's member-days (`sessions` in its period, sorted) with no industry in force
    (`history`, each chain's (effective_from, industry)): more than `INDUSTRY_UNKNOWN_PCT`
    warns."""
    total, unknown, per_chain = unknown_member_days(sessions, spans, history)
    pct = round(100 * unknown / total, 3) if total else 0.0
    worst = sorted(per_chain.items(), key=lambda kv: (-kv[1], kv[0]))
    items = [{"instrument_id": i, "symbol": names.get(i, i), "member_days": n} for i, n in worst]
    findings = []
    if pct > INDUSTRY_UNKNOWN_PCT:
        findings.append(
            finding("industry_unknown", WARN, items, threshold_pct=INDUSTRY_UNKNOWN_PCT)
        )
    summary = {
        "member_days": total,
        "unknown_member_days": unknown,
        "unknown_pct": pct,
        "unknown_members": len(per_chain),
    }
    return check_result("industries", summary, findings)


INDUSTRY_CAP_REFUSED = "backtest_industry_cap_without_industries"


def industry_cap_refused(doc: Mapping[str, Any], max_per_industry: int, max_open: int) -> bool:
    """Whether a run whose policy holds at most `max_per_industry` positions per industry and
    `max_open` in all is refused on the audit `doc`: the cap binds below the open positions
    while a member-day has no industry (or the audit did not measure it), so every unknown name
    would count as one industry. An uncapped run on the same audit is never refused."""
    if max_per_industry >= max_open:
        return False
    found = next((c for c in doc.get("checks", ()) if c.get("name") == "industries"), None)
    unknown = None if found is None else (found.get("summary") or {}).get("unknown_member_days")
    return unknown != 0


@dataclass(frozen=True)
class Demerger:
    """A stored demerger of a member chain: its factor, the ex-day open and close gaps over
    NSE's previous close (None without a bar), and its `announced_on`."""

    instrument_id: str
    symbol: str
    ex_date: date
    factor: float
    open_gap: float | None
    close_gap: float | None
    announced_on: date | None


def check_demergers(rows: Iterable[Demerger], start: date, end: date) -> dict[str, Any]:
    """A demerger whose open and close gaps differ by more than `DEMERGER_GAP_PCT` and whose
    factor is not within `CLOSE_GAP_TOLERANCE` of the close gap (a thin open the factor still
    takes) warns. The summary counts the demergers with an ex-date in `start..end` and no
    `announced_on`: undated, a run holds through them."""
    thin, undated, checked = [], [], 0
    for d in sorted(rows, key=lambda d: (d.ex_date, d.symbol)):
        row = {"instrument_id": d.instrument_id, "symbol": d.symbol, "ex_date": _iso(d.ex_date)}
        if start <= d.ex_date <= end and d.announced_on is None:
            undated.append(row)
        if not d.open_gap or not d.close_gap:
            continue
        checked += 1
        apart = abs(d.open_gap / d.close_gap - 1) * 100
        if apart > DEMERGER_GAP_PCT and abs(d.factor / d.close_gap - 1) > CLOSE_GAP_TOLERANCE:
            thin.append(
                row
                | {
                    "factor": round(d.factor, 6),
                    "open_gap": round(d.open_gap, 6),
                    "close_gap": round(d.close_gap, 6),
                }
            )
    findings = []
    if thin:
        findings.append(finding("demerger_thin_open", WARN, thin, threshold_pct=DEMERGER_GAP_PCT))
    summary = {
        "gaps_checked": checked,
        "undated_member_demergers": len(undated),
        "undated": undated[:MAX_LISTED],
    }
    return check_result("demergers", summary, findings)


# --- golden sample ---------------------------------------------------------------------------


def check_golden(result: Mapping[str, Any] | None, problem: str | None = None) -> dict[str, Any]:
    """The golden sample check's result (`golden.check`) as an audit check: drifted or missing
    values fail, unresolvable keys and recorded mismatches still open warn. Without a result,
    `problem` says why: no golden file yet warns, an unreadable or invalid one fails."""
    if result is None:
        code = problem or "golden_file_missing"
        severity = WARN if code == "golden_file_missing" else FAIL
        return check_result("golden", {"file": None}, [finding(code, severity, [])])
    shown = ("id", "kind", "risk", "status", "value", "store_value", "note")

    def listed(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{k: r[k] for k in shown if k in r} for r in rows]

    drift = [o for o in result["drift"] if o["status"] != golden.UNRESOLVED]
    unresolved = [o for o in result["drift"] if o["status"] == golden.UNRESOLVED]
    still_open = [o for o in result["mismatches"] if o["status"] == golden.OPEN]
    findings = []
    if drift:
        findings.append(finding("golden_drift", FAIL, listed(drift)))
    if unresolved:
        findings.append(finding("golden_key_unresolved", WARN, listed(unresolved)))
    if still_open:
        findings.append(finding("golden_mismatch_open", WARN, listed(still_open)))
    summary = {
        "file": result["golden_file"],
        "digest": result["golden_digest"],
        "created": result["golden_created"],
        "items": sum(result["counts"].values()),
        "counts": result["counts"],
        "mismatch_counts": result["mismatch_counts"],
    }
    return check_result("golden", summary, findings)


def _golden(
    store: DataStore, session: Session, con: duckdb.DuckDBPyConnection, chain: Mapping[str, str]
) -> dict[str, Any]:
    path = golden.newest(store.root)
    if path is None:
        return check_golden(None)
    try:
        _, result = golden.run_check(path, store, session, con, chain)
    except golden.GoldenError as exc:
        return check_golden(None, exc.code)
    return check_golden(result)


def check_verify(status: Mapping[str, Any]) -> dict[str, Any]:
    """The window's data verification (`verify.window_status`): no report, a blocking check that
    failed or did not cover the window, or a report on data since changed, warns; the summary
    says whether the window is verified."""
    findings = []
    if status.get("report") is None:
        findings.append(finding("verify_report_missing", WARN, list(status.get("not_run") or ())))
    else:
        if status.get("failed"):
            findings.append(finding("verify_blocking_failed", WARN, list(status["failed"])))
        if status.get("not_run"):
            findings.append(finding("verify_blocking_not_run", WARN, list(status["not_run"])))
        if status.get("stale"):
            findings.append(finding("verify_report_stale", WARN, [status.get("report")]))
    summary = {
        "verified": bool(status.get("verified")),
        "report": status.get("report"),
        "digest": status.get("digest"),
        "created": status.get("created"),
        "checks": status.get("checks") or {},
    }
    return check_result("verify", summary, findings)


# --- the document ------------------------------------------------------------------------------


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def audit_doc(checks: Sequence[Mapping[str, Any]], scope: Mapping[str, Any]) -> dict[str, Any]:
    """The audit document: its scope (data snapshot, window, universe), the checks, the worst
    status and the digest (sha256 of everything else, so the same data audits to the same
    digest)."""
    status = max((c["status"] for c in checks), key=_RANK.__getitem__, default=PASS)
    body = {
        "audit_version": AUDIT_VERSION,
        "scope": dict(scope),
        "status": status,
        "checks": [dict(c) for c in checks],
    }
    return {**body, "digest": hashlib.sha256(_canonical(body).encode()).hexdigest()}


def cache_key(
    snapshot_id: str,
    start: date,
    end: date,
    *,
    used_5m: bool,
    golden_digest: str | None,
    verify_digest: str | None = None,
) -> str:
    """The audit's name under `data_audits/`: the audit version, the data snapshot (which covers
    the universe's members and every bar, feature and close the run reads), the window, whether
    the run reads 5-minute bars, the sha256 of the golden
    file the `golden` check reads (`golden.current`), so a new golden file is checked, and the
    digest of the verification report covering the window, so a new report is read."""
    text = f"{AUDIT_VERSION}|{snapshot_id}|{start}|{end}|{used_5m}|{golden_digest}|{verify_digest}"
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def audits_dir(root: Path) -> Path:
    return root / "backtests" / "data_audits"


def explanation_of(root: Path, digest: object) -> Path | None:
    """The owner's explanation of a data audit's warnings, written once per audit digest as
    `data_audits/<digest>.explained.md` (one line per finding); None when there is none."""
    if not isinstance(digest, str) or not digest.isalnum():
        return None
    path = audits_dir(root) / f"{digest}.explained.md"
    return path if path.is_file() else None


def read_cached(root: Path, key: str) -> dict[str, Any] | None:
    try:
        found = json.loads((audits_dir(root) / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (
        found if isinstance(found, dict) and found.get("audit_version") == AUDIT_VERSION else None
    )


def cached(
    root: Path, key: str, compute: Callable[[], dict[str, Any]], *, refresh: bool = False
) -> tuple[dict[str, Any], Path]:
    """The audit under `key`, computed and written (atomically) when it is absent or `refresh`."""
    path = audits_dir(root) / f"{key}.json"
    found = None if refresh else read_cached(root, key)
    if found is None:
        found = compute()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{key}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(found, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return found, path


# --- reading the inputs ------------------------------------------------------------------------

# Bars of one (chain id, date) from more than one ISIN, or more than once in one ingest run.
_DUPES_SQL = """
SELECT instrument_id, date, count(*) AS n, count(DISTINCT isin) AS isins,
       count(DISTINCT ingest_run_id) AS runs
FROM bars_daily_cap_adj
WHERE date BETWEEN ? AND ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
GROUP BY instrument_id, date
HAVING count(DISTINCT isin) > 1 OR count(*) > count(DISTINCT ingest_run_id)
ORDER BY instrument_id, date
"""
# The bars the features job reads (`features._COLUMNS_SQL`), for the sampled instruments.
_FEATURE_BARS_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date, isin,
       CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value,
       CAST(div_factor AS DOUBLE) AS div_factor, ingest_run_id
FROM bars_daily_adj
WHERE date <= ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""
# Every instrument's ISIN and price-basis close of one day (the latest ingest run's row).
_DAY_CLOSES_SQL = """
SELECT CAST(instrument_id AS VARCHAR), isin, CAST(close AS DOUBLE)
FROM bars_daily_cap WHERE date = ?
"""
# One year's stored 5-minute days off their raw daily bar (bars off the IST 5-minute grid of
# their own day's session, no daily bar, or a last close more than ? off the daily close), with
# the count of days scanned on every row (the first row is always kept to carry it). IST is
# UTC+5:30, so a 5-minute grid instant is a multiple of 300 s in epoch time too. The daily
# close is the latest ingest run's (`newest`), aggregated over the year's raw rows rather than
# the `bars_daily_raw_latest` view, which ranks the whole store; it is the day's bar of the same
# ISIN, else of its chain (a split's new ISIN may hold 5-minute days the old one has the bars of).
_BARS_5M_SQL = f"""
WITH day AS (
    SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date,
           arg_max(CAST(close AS DOUBLE), bar_start) AS last_close,
           count(*) FILTER (WHERE epoch_us(bar_start) % 300000000 <> 0
               OR CAST(timezone('Asia/Kolkata', bar_start) AS DATE) <> date
               OR CAST(timezone('Asia/Kolkata', bar_start) AS TIME) < CAST(? AS TIME)
               OR CAST(timezone('Asia/Kolkata', bar_start) AS TIME) >= CAST(? AS TIME)) AS off_grid
    FROM read_parquet(?, hive_partitioning = true)
    GROUP BY ALL
), daily AS (
    SELECT CAST(r.instrument_id AS VARCHAR) AS instrument_id, r.date,
           CAST({newest("close")} AS DOUBLE) AS close
    FROM bars_daily_raw r
    WHERE r.date BETWEEN ? AND ?
    GROUP BY ALL
), chained AS (
    SELECT COALESCE(l.chain_id, r.instrument_id) AS chain_id, r.date,
           arg_max(r.close, r.instrument_id) AS close
    FROM daily r LEFT JOIN lineage l ON l.instrument_id = r.instrument_id
    GROUP BY ALL
), joined AS (
    SELECT d.*, COALESCE(own.close, other.close) AS close
    FROM day d
    LEFT JOIN lineage l ON l.instrument_id = d.instrument_id
    LEFT JOIN daily own ON own.instrument_id = d.instrument_id AND own.date = d.date
    LEFT JOIN chained other
           ON other.chain_id = COALESCE(l.chain_id, d.instrument_id) AND other.date = d.date
)
SELECT instrument_id, date, off_grid, last_close, close, count(*) OVER () AS scanned
FROM joined
QUALIFY off_grid > 0 OR close IS NULL OR abs(last_close / close - 1) * 100 > ?
    OR row_number() OVER () = 1
"""  # noqa: S608 - `newest` is a constant expression
# The raw open, close and NSE previous close of the demergers' ex-days (the latest ingest run's
# row, `newest`, over those days alone).
_EX_DAY_SQL = f"""
SELECT CAST(r.instrument_id AS VARCHAR), r.date, CAST({newest("open")} AS DOUBLE),
       CAST({newest("close")} AS DOUBLE), CAST({newest("prev_close")} AS DOUBLE)
FROM bars_daily_raw r
WHERE r.date IN (SELECT unnest(CAST(? AS DATE[])))
  AND CAST(r.instrument_id AS VARCHAR) IN (SELECT unnest(CAST(? AS VARCHAR[])))
GROUP BY ALL
"""  # noqa: S608 - `newest` is a constant expression


def _spread(items: Sequence[Any], count: int) -> list[Any]:
    """`count` items spread evenly over `items` (all of them when fewer), first and last kept."""
    if len(items) <= count:
        return list(items)
    step = (len(items) - 1) / (count - 1)
    return [items[round(n * step)] for n in range(count)]


def _hashed(ids: Iterable[str], salt: str, count: int) -> list[str]:
    """`count` of `ids` in a fixed pseudo-random order (sha256 of id and salt)."""
    return sorted(ids, key=lambda i: hashlib.sha256(f"{i}|{salt}".encode()).hexdigest())[:count]


def trading_days(session: Session, first: date, last: date, market_code: str = "NSE") -> list[date]:
    """The calendar's trading days in `first..last` (`hat_trading_calendar`)."""
    market = session.scalar(select(HatMarket).where(HatMarket.code == market_code))
    if market is None:
        return []
    return list(
        session.scalars(
            select(HatTradingCalendar.day).where(
                HatTradingCalendar.market_id == market.id,
                HatTradingCalendar.is_trading.is_(True),
                HatTradingCalendar.day.between(first, last),
            )
        )
    )


def _names(market: MarketData) -> dict[str, str]:
    return {i: points[-1][1] for i, points in market.symbols.items() if points}


def _feature_samples(
    con: duckdb.DuckDBPyConnection,
    market: MarketData,
    sessions: Sequence[date],
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    bar_days: Mapping[str, Sequence[date]],
) -> list:
    """(instrument, day, stored row or None, recomputed row) for `SAMPLE_INSTRUMENTS` member
    days with a bar on each of `SAMPLE_DAYS` sessions: drawn from the members, not the stored
    rows, so a missing row is sampled too."""
    features = market.features
    held = {i: set(days) for i, days in bar_days.items()}
    picks: list[tuple[str, date]] = []
    for day in _spread(sessions, SAMPLE_DAYS):
        ids = [i for i, s in spans.items() if _member_on(s, day) and day in held.get(i, ())]
        picks.extend((i, day) for i in _hashed(ids, _iso(day), SAMPLE_INSTRUMENTS))
    if not picks:
        return []
    wanted = sorted({i for i, _ in picks})
    bars = latest_rows(con.execute(_FEATURE_BARS_SQL, [max(d for _, d in picks), wanted]).pl())
    bars = bars.drop("ingest_run_id").sort("instrument_id", "date")
    out = []
    for instrument_id, day in picks:
        own = bars.filter((pl.col("instrument_id") == instrument_id) & (pl.col("date") <= day))
        fresh, _ = compute_features(own) if own.height else (pl.DataFrame(), None)
        fresh_row = fresh.filter(pl.col("date") == day).to_dicts() if fresh.height else []
        stored = features.filter(
            (pl.col("date") == day) & (pl.col("instrument_id").cast(pl.String) == instrument_id)
        ).to_dicts()
        out.append(
            (instrument_id, day, stored[0] if stored else None, fresh_row[0] if fresh_row else None)
        )
    return out


def _breadth_samples(
    store: DataStore, con: duckdb.DuckDBPyConnection, market: MarketData, sessions: Sequence[date]
) -> list:
    """(day, stored breadth, recount, stocks counted) on `BREADTH_DAYS` sessions."""
    out = []
    for day in _spread([d for d in sessions if d in market.breadth], BREADTH_DAYS):
        part = store.read_partition(DATASET_FEATURES, day)
        sma = dict(
            zip(
                part["instrument_id"].cast(pl.String).to_list(),
                part["sma50"].to_list(),
                strict=True,
            )
        )
        rows = [
            (isin, close, sma.get(i))
            for i, isin, close in con.execute(_DAY_CLOSES_SQL, [day]).fetchall()
        ]
        fresh, counted = recount_breadth(rows)
        out.append((day, float(market.breadth[day]), fresh, counted))
    return out


def later_factors(adjustment: Adjustment) -> dict[str, tuple[list[date], list[float]]]:
    """Each chain's stored price-factor ex-dates (sorted) and, at each, the product of its
    factors from that ex-date on: the factor a price of a day before it carries on the adjusted
    basis."""
    by_chain: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for f in adjustment.factors:
        by_chain[str(f.instrument_id)].append((f.ex_date, float(f.factor)))
    out = {}
    for chain, rows in by_chain.items():
        rows.sort()
        products, running = [], 1.0
        for _, factor in reversed(rows):
            running *= factor
            products.append(running)
        out[chain] = ([d for d, _ in rows], products[::-1])
    return out


def _later(factors: Mapping[str, tuple[list[date], list[float]]], chain: str, day: date) -> float:
    days, products = factors.get(chain, ([], []))
    at = bisect_right(days, day)
    return products[at] if at < len(products) else 1.0


def _bars_5m(
    root: Path,
    con: duckdb.DuckDBPyConnection,
    adjustment: Adjustment,
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    start: date,
    end: date,
) -> tuple[list[Day5m], int]:
    """Every stored 5-minute day off its daily bar (`_BARS_5M_SQL`) and the days scanned."""
    years = sorted({d.year for d in BarStore(root).days()})
    folder = BarStore(root).partition(date(2000, 1, 1)).parent
    rows, scanned = [], 0
    for year in years:  # a year at a time: the whole store does not fit DuckDB's memory limit
        found = con.execute(
            _BARS_5M_SQL,
            [
                SESSION_OPEN.isoformat(),
                SESSION_CLOSE.isoformat(),
                str(folder / f"date={year}-*" / "*.parquet"),
                date(year, 1, 1),
                date(year, 12, 31),
                CLOSE_5M_PCT,
            ],
        ).fetchall()
        rows.extend(found)
        scanned += found[0][5] if found else 0
    factors = later_factors(adjustment)
    out = []
    for instrument_id, day, off_grid, last_close, daily_close, _ in rows:
        chain = adjustment.lineage.get(instrument_id, instrument_id)
        member = start <= day <= end and any(
            lo <= day and (hi is None or day < hi) for lo, hi in spans.get(chain, ())
        )
        out.append(
            Day5m(
                instrument_id,
                day,
                off_grid,
                last_close,
                daily_close,
                _later(factors, chain, day),
                member,
            )
        )
    return out, scanned


def _demergers(
    session: Session,
    con: duckdb.DuckDBPyConnection,
    adjustment: Adjustment,
    spans: Mapping[str, Sequence[tuple[date, date | None]]],
    first: date,
    end: date,
) -> list[Demerger]:
    """The stored demergers of the member chains with an ex-date in `first..end`, with the
    ex-day gaps of the chain's bar that day (any of its ISINs)."""
    actions = session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatInstrument.symbol,
            HatCorporateAction.ex_date,
            HatCorporateAction.adjustment_factor,
            HatCorporateAction.announced_on,
        )
        .join(HatInstrument, HatInstrument.id == HatCorporateAction.instrument_id)
        .where(
            HatCorporateAction.demerger.is_(True), HatCorporateAction.ex_date.between(first, end)
        )
    ).all()
    lineage = adjustment.lineage
    wanted = [a for a in actions if lineage.get(str(a[0]), str(a[0])) in spans]
    chain_ids = {i for i in {*lineage, *lineage.values()} if lineage.get(i, i) in spans}
    gaps: dict[tuple[str, date], tuple[float, float]] = {}
    if wanted:
        ids = sorted(chain_ids | {str(a[0]) for a in wanted})
        days = sorted({a[2] for a in wanted})
        for i, day, open_, close, prev in con.execute(_EX_DAY_SQL, [days, ids]).fetchall():
            if prev:
                gaps[(lineage.get(i, i), day)] = (open_ / prev, close / prev)
    out = []
    for instrument_id, symbol, ex_date, factor, announced_on in wanted:
        chain = lineage.get(str(instrument_id), str(instrument_id))
        open_gap, close_gap = gaps.get((chain, ex_date), (None, None))
        out.append(
            Demerger(chain, symbol, ex_date, float(factor), open_gap, close_gap, announced_on)
        )
    return out


def _history(market: MarketData) -> dict[str, list[tuple[date, str]]]:
    """The run's industry classifications per chain; one plain name is in force throughout."""
    out = {}
    for chain, found in market.industries.items():
        out[chain] = [(date.min, found)] if isinstance(found, str) else sorted(found)
    return out


def gather(
    store: DataStore,
    session: Session,
    market: MarketData,
    snapshot: Mapping[str, Any],
    start: date,
    end: date,
    *,
    used_5m: bool = False,
    verified: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The audit document of the market a run of `start..end` reads (`backtest.data.load_market`
    and its snapshot): reads what the checks need beyond it (the calendar, the store's duplicate
    keys, the sampled bars, features and breadth, the 5-minute store and the demergers) and runs
    every check. `verified` is the window's verification status (`verify.status_for`), read
    here when not given."""
    if verified is None:
        from .verify import status_for  # noqa: PLC0415 - verify imports this module

        verified = status_for(store.root, session, start, end, used_5m=used_5m)
    sessions = sorted(market.days)
    in_window = [d for d in sessions if start <= d <= end]
    names = _names(market)
    spans: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for m in market.members:
        spans[str(m.instrument_id)].append((m.effective_from, m.effective_to))
    frame = market.bars.select("instrument_id", "date", "close", "raw_close").sort(
        "instrument_id", "date"
    )
    bar_days: dict[str, list[date]] = {}
    series: dict[str, tuple[list[date], list[float], list[float]]] = {}
    for key, part in frame.partition_by("instrument_id", as_dict=True).items():
        days = part["date"].to_list()
        bar_days[str(key[0])] = days
        series[str(key[0])] = (
            days,
            part["close"].fill_null(0.0).to_list(),
            part["raw_close"].fill_null(0.0).to_list(),
        )
    adjustment = stored_adjustment(session)
    with store.connect(adjustment) as con:
        dupes = [
            {"instrument_id": str(i), "date": _iso(d), "rows": n, "isins": isins, "runs": runs}
            for i, d, n, isins, runs in con.execute(
                _DUPES_SQL, [sessions[0], end, sorted(bar_days)]
            ).fetchall()
        ]
        feature_samples = _feature_samples(con, market, in_window, spans, bar_days)
        breadth_samples = _breadth_samples(store, con, market, in_window)
        bars_5m, scanned_5m = _bars_5m(store.root, con, adjustment, spans, start, end)
        demergers = _demergers(session, con, adjustment, spans, sessions[0], end)
        golden_check = _golden(store, session, con, adjustment.lineage)
    regime_index = snapshot.get("index")
    lookahead = [
        *check_features(feature_samples),
        *check_breadth(breadth_samples),
        *check_gate_inputs(
            sessions,
            (d for d, _ in market.index_closes.get(str(regime_index), ())),
            market.breadth,
            start,
            end,
        ),
    ]
    checks = [
        check_calendar(sessions, trading_days(session, start, end), start, end),
        check_missing_sessions(sessions, spans, bar_days, names, start, end),
        check_survivorship(sessions, spans, bar_days, market.last_traded, names, start, end),
        check_corporate_actions(series, sessions, market.skipped, names),
        check_result(
            "lookahead",
            {
                "feature_samples": len(feature_samples),
                "breadth_samples": len(breadth_samples),
                "features_compared": list(PER_INSTRUMENT),
                "engine_reads": "trade date D: scan, features, regime and breadth gates of D-1",
            },
            lookahead,
        ),
        check_unique_keys(
            dupes,
            market.features.select(pl.col("instrument_id").cast(pl.String), "date").iter_rows(),
            ((name, d) for name, points in market.index_closes.items() for d, _ in points),
        ),
        check_bars_5m(bars_5m, scanned_5m, used=used_5m)
        if scanned_5m
        else check_result("bars_5m", {"days_scanned": 0, "used_by_run": used_5m}, [])
        | {"status": SKIPPED},
        check_industries(in_window, spans, _history(market), names),
        check_demergers(demergers, start, end),
        check_verify(verified),
        golden_check,
    ]
    scope = {
        "data_snapshot_id": snapshot.get("content_digest"),
        "universe": (snapshot.get("membership") or {}).get("universe_version_id"),
        "index": (snapshot.get("membership") or {}).get("index"),
        "period_start": _iso(start),
        "period_end": _iso(end),
        "members": snapshot.get("members"),
    }
    return audit_doc(checks, scope)


# --- the command line --------------------------------------------------------------------------


def run_counts(doc: Mapping[str, Any]) -> dict[str, Any]:
    """What run.json's `data` carries of the audit beyond its status: the share of member-days
    with no industry, the undated member demergers, and whether the window is verified and by
    which report's digest (None from an audit without the check)."""
    found = {c.get("name"): c.get("summary") or {} for c in doc.get("checks", ())}
    verify = found.get("verify", {})
    return {
        "industry_unknown_pct": found.get("industries", {}).get("unknown_pct"),
        "undated_member_demergers": found.get("demergers", {}).get("undated_member_demergers"),
        "verified": verify.get("verified"),
        "verify_report": verify.get("digest"),
    }


def summary(doc: Mapping[str, Any], path: Path | None = None) -> dict[str, Any]:
    """What the command line prints: the status, digest, file and each check's status and
    finding counts."""
    return {
        "status": doc.get("status"),
        "digest": doc.get("digest"),
        "file": None if path is None else str(path),
        "checks": {
            c["name"]: {
                "status": c["status"],
                **{f["code"]: f["count"] for f in c.get("findings", ())},
            }
            for c in doc.get("checks", ())
        },
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.audit --from YYYY-MM-DD --to YYYY-MM-DD --universe <id|name>
    [--index NAME] [--refresh]: audit the market a backtest of the universe over the period
    reads (loaded as a run loads it, `backtest.data.load_market`, under a read-only
    transaction), write the audit under `HAT_DATA_ROOT/backtests/data_audits/` (or read it
    there when the same data was audited), print the summary; exit 2 on `fail`."""
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    # the market as a run reads it
    from ..backtest.data import load_market  # noqa: PLC0415
    from ..backtest.evidence import universe_live_version  # noqa: PLC0415
    from ..backtest.spec import BacktestError  # noqa: PLC0415
    from ..engine.portfolio import regime  # noqa: PLC0415
    from ..models import HatUniverse  # noqa: PLC0415
    from ..settings import operator_settings  # noqa: PLC0415
    from .verify import status_for  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.data.audit")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--index", default=regime.INDEX_NAME)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    settings = operator_settings()
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatMarket})
        try:
            named = HatUniverse.id == uuid.UUID(args.universe)
        except ValueError:
            named = HatUniverse.name == args.universe
        found = session.scalars(select(HatUniverse).where(named)).all()
        live = universe_live_version(session, found[0].id) if len(found) == 1 else None
        if live is None:
            sys.stderr.write("data audit refused: data_audit_unknown_universe\n")
            return 2
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
        try:
            market, snapshot = load_market(
                store,
                session,
                universe_version_id=live.id,
                index=args.index,
                start=args.start,
                end=args.end,
            )
        except BacktestError as exc:
            sys.stderr.write(f"data audit refused: {exc.code}\n")
            return 2
        verified = status_for(settings.data_root, session, args.start, args.end, used_5m=False)
        key = cache_key(
            str(snapshot["content_digest"]),
            args.start,
            args.end,
            used_5m=False,
            golden_digest=golden.current(settings.data_root),
            verify_digest=f"{verified.get('digest')}|{verified.get('verified')}",
        )
        doc, path = cached(
            settings.data_root,
            key,
            lambda: gather(
                store,
                session,
                market,
                snapshot,
                args.start,
                args.end,
                verified=verified,
            ),
            refresh=args.refresh,
        )
    sys.stdout.write(json.dumps(summary(doc, path), indent=2) + "\n")
    return 2 if doc.get("status") == FAIL else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

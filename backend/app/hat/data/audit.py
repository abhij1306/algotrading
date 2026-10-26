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
- `bars_5m`: on sample sessions, the 5-minute bars start on the 5-minute grid inside the IST session
  of their own day and fit the raw daily bar within the history envelope tolerance. Findings warn
  while a run reads no 5-minute bars, and fail when it does.

A run's audit is kept under `HAT_DATA_ROOT/backtests/data_audits/` by its data snapshot and window
(`cache_key`), so the variants of a sweep, and later runs on the same data, read it instead of
recomputing it. A run refuses to start on `fail` (`backtest_data_audit_failed`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatMarket, HatTradingCalendar
from ..time import IST
from .bars_store import BarStore
from .chains import stored_adjustment
from .features import compute_features
from .isin import ORDINARY_EQUITY
from .sessions import SESSION_CLOSE, SESSION_OPEN
from .store import DATASET_FEATURES, FEATURE_NAMES, DataStore, latest_rows

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
    "check_features",
    "check_gate_inputs",
    "check_missing_sessions",
    "check_survivorship",
    "check_unique_keys",
    "gather",
    "read_cached",
]

AUDIT_VERSION = "1"  # a change to a check or a threshold changes it, so no stale audit is read
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
BARS_5M_DAYS = 6
BARS_5M_INSTRUMENTS = 20
FEATURE_TOLERANCE = 1e-9  # relative; a rolling mean's last bits depend on where its frame starts
# The features of one instrument's own rows; the percentile columns rank the whole market.
PER_INSTRUMENT = tuple(n for n in FEATURE_NAMES if n not in {"rs_pct", "mom_rank"})


def _check(name: str, summary: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> dict:
    """A check's result: its status is its worst finding's severity."""
    status = max((f["severity"] for f in findings), key=_RANK.__getitem__, default=PASS)
    return {"name": name, "status": status, "summary": dict(summary), "findings": list(findings)}


def _finding(code: str, severity: str, items: Sequence[Any], **extra: object) -> dict[str, Any]:
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
        findings.append(_finding("store_day_not_session", FAIL, [_iso(d) for d in extra]))
    missing = sorted(sessions - stored)
    if missing:
        findings.append(_finding("session_missing_from_store", FAIL, [_iso(d) for d in missing]))
    if window and end > last and any(d > last for d in trading if d <= end):
        findings.append(_finding("data_ends_before_period", WARN, [_iso(last)]))
    return _check(
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
        findings.append(_finding("member_sessions_missing", WARN, heavy))
    share = 100 * gaps / total if total else 0.0
    if share > MISSING_FAIL_PCT:
        findings.append(_finding("universe_sessions_missing", FAIL, [round(share, 3)]))
    summary = {
        "member_sessions": total,
        "missing": gaps,
        "missing_pct": round(share, 4),
        "symbols_with_gaps": len(rows),
        "worst": rows[:MAX_LISTED],
    }
    return _check("missing_sessions", summary, findings)


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
        return _check("survivorship", {"samples": []}, [])
    samples = [sessions[0]]
    for day in sessions[1:]:
        if day.year != samples[-1].year:
            samples.append(day)
    rows, leavers = [], set()
    for day in samples:
        members = [
            i
            for i, s in spans.items()
            if any(lo <= day and (hi is None or hi > day) for lo, hi in s)
        ]
        left = [
            i
            for i in members
            if i in last_traded
            or all(hi is not None and hi <= end for lo, hi in spans[i] if hi is None or hi > day)
        ]
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
        findings.append(_finding("leaver_without_bars", FAIL, empty))
    if thin:
        findings.append(_finding("leaver_bars_thin", WARN, thin))
    if not leavers and (end - start).days >= SURVIVOR_MIN_DAYS:
        findings.append(_finding("no_leavers", FAIL, [r["date"] for r in rows]))
    summary = {"samples": rows, "leavers": len(leavers)}
    return _check("survivorship", summary, findings)


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
        factors = [c / r if r > 0 else math.nan for c, r in zip(closes, raw, strict=True)]
        if any(
            not (math.isfinite(f) and f > 0) for f, c in zip(factors, closes, strict=True) if c > 0
        ):
            bad.append(
                {"instrument_id": instrument_id, "symbol": names.get(instrument_id, instrument_id)}
            )
        ex_dates += sum(
            1
            for n in range(1, len(factors))
            if factors[n - 1] > 0 and abs(factors[n] / factors[n - 1] - 1) > FACTOR_STEP
        )
        for jump in jumps(days, closes, raw, position):
            if (instrument_id, date.fromisoformat(jump["date"])) in known:
                excused += 1
                continue
            row = {
                "instrument_id": instrument_id,
                "symbol": names.get(instrument_id, instrument_id),
                **jump,
            }
            if jump["ex_date"]:
                at_ex.append(row)
            else:
                off_ex.append(row)
                if action_shaped(jump["raw_move_pct"]):
                    shaped.append(row)
    findings = []
    if bad:
        findings.append(_finding("bad_adjustment_factor", FAIL, bad))
    if at_ex:
        findings.append(_finding("ex_date_jump", WARN, at_ex, threshold_pct=JUMP_PCT))
    if shaped:
        findings.append(_finding("action_shaped_jump", WARN, shaped))
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
    return _check("corporate_actions", summary, findings)


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
    or was written from other bars (fail)."""
    wrong = []
    for instrument_id, day, stored, fresh in samples:
        if stored is None or fresh is None:
            wrong.append({"instrument_id": instrument_id, "date": _iso(day), "features": ["row"]})
            continue
        off = [n for n in PER_INSTRUMENT if not _same(stored.get(n), fresh.get(n))]
        if off:
            wrong.append({"instrument_id": instrument_id, "date": _iso(day), "features": off})
    return [_finding("feature_not_point_in_time", FAIL, wrong)] if wrong else []


def recount_breadth(
    rows: Iterable[tuple[str | None, float | None, float | None]],
) -> tuple[float | None, int]:
    """(breadth %, stocks with an SMA50) from one day's (ISIN, close, stored SMA50) rows, as the
    features job counts it: the ordinary equity rows above their SMA50 (by the job's margin)
    over those that have one."""
    import re  # noqa: PLC0415 - one pattern, compiled here

    from .features import ABOVE_MARGIN  # noqa: PLC0415

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
    return [_finding("breadth_not_point_in_time", FAIL, wrong)] if wrong else []


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
        findings.append(_finding("no_index_close_before", WARN, no_close))
    if no_breadth:
        findings.append(_finding("no_breadth_before", WARN, no_breadth))
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
        findings.append(_finding("duplicate_bar", FAIL, list(bar_dupes)))
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
            findings.append(_finding(code, FAIL, dupes))
    return _check("unique_keys", {"daily_dates": "date (IST trade date, no time)"}, findings)


@dataclass(frozen=True)
class Day5m:
    """One instrument's 5-minute bars of one session (start instants, aware, and OHLC) and its
    raw daily bar (open, high, low, close), None without one."""

    instrument_id: str
    day: date
    starts: Sequence[datetime]
    bars: Sequence[tuple[float, float, float, float]]
    daily: tuple[float, float, float, float] | None


def _on_grid(start: datetime, day: date) -> bool:
    local = start.astimezone(IST)
    return (
        local.date() == day
        and SESSION_OPEN <= local.time() < SESSION_CLOSE
        and local.minute % 5 == 0
        and local.second == 0
        and local.microsecond == 0
    )


def fits_daily(
    bars: Sequence[tuple[float, float, float, float]], daily: tuple[float, ...], tol: float
) -> bool:
    """Whether one day's 5-minute bars fit its raw daily bar (open, high, low, close) as the
    history import requires (`bars_basis.check_day`): the high and the low within `tol`, the
    first open and the last close inside the day's range widened by `tol`."""
    high = max(b[1] for b in bars)
    low = min(b[2] for b in bars)
    floor, ceiling = daily[2] * (1 - tol), daily[1] * (1 + tol)
    return (
        abs(high - daily[1]) <= daily[1] * tol
        and abs(low - daily[2]) <= daily[2] * tol
        and floor <= bars[0][0] <= ceiling
        and floor <= bars[-1][3] <= ceiling
    )


def check_bars_5m(days: Sequence[Day5m], tolerance: float, *, used: bool) -> dict[str, Any]:
    """The sample's 5-minute bars: each start on the 5-minute grid inside its own day's IST
    session, and each day fitting its raw daily bar within `tolerance`. A finding fails when the
    run reads 5-minute bars (`used`) and warns otherwise."""
    severity = FAIL if used else WARN
    off_grid, misfit = [], []
    for d in days:
        bad = [s.isoformat() for s in d.starts if not _on_grid(s, d.day)]
        if bad:
            off_grid.append(
                {"instrument_id": d.instrument_id, "date": _iso(d.day), "starts": bad[:3]}
            )
        if d.bars and (d.daily is None or not fits_daily(d.bars, d.daily, tolerance)):
            misfit.append({"instrument_id": d.instrument_id, "date": _iso(d.day)})
    findings = []
    if off_grid:
        findings.append(_finding("bar_5m_off_ist_grid", severity, off_grid))
    if misfit:
        findings.append(_finding("bar_5m_off_daily", severity, misfit, tolerance=tolerance))
    summary = {"days": len(days), "bars": sum(len(d.bars) for d in days), "used_by_run": used}
    return _check("bars_5m", summary, findings)


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


def cache_key(snapshot_id: str, start: date, end: date, *, used_5m: bool) -> str:
    """The audit's name under `data_audits/`: the audit version, the data snapshot (which covers
    the universe's members and every bar, feature and close the run reads) and the window."""
    text = f"{AUDIT_VERSION}|{snapshot_id}|{start}|{end}|{used_5m}"
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def audits_dir(root: Path) -> Path:
    return root / "backtests" / "data_audits"


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
# The raw daily bars of the sampled 5-minute days.
_RAW_SQL = """
SELECT CAST(instrument_id AS VARCHAR), date, CAST(open AS DOUBLE), CAST(high AS DOUBLE),
       CAST(low AS DOUBLE), CAST(close AS DOUBLE)
FROM bars_daily_raw_latest
WHERE date = ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""


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
    store: DataStore, con: duckdb.DuckDBPyConnection, market: MarketData, sessions: Sequence[date]
) -> list:
    """(instrument, day, stored row, recomputed row) for `SAMPLE_INSTRUMENTS` instruments with
    a stored row on each of `SAMPLE_DAYS` sessions."""
    features = market.features
    picks: list[tuple[str, date]] = []
    for day in _spread(sessions, SAMPLE_DAYS):
        ids = features.filter(pl.col("date") == day)["instrument_id"].cast(pl.String).to_list()
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


def _bars_5m(
    root: Path, con: duckdb.DuckDBPyConnection, market: MarketData, sessions: Sequence[date]
) -> list[Day5m]:
    """The sampled sessions' 5-minute bars of up to `BARS_5M_INSTRUMENTS` members each, with
    their raw daily bars."""
    store = BarStore(root)
    held = set(store.days())
    out = []
    for day in _spread([d for d in sessions if d in held], BARS_5M_DAYS):
        rows: dict[str, list] = defaultdict(list)
        members = {str(m.instrument_id) for m in market.members}
        for r in store.read(day):
            if str(r.instrument_id) in members:
                rows[str(r.instrument_id)].append(r)
        picked = _hashed(rows, _iso(day), BARS_5M_INSTRUMENTS)
        daily = {
            i: (o, h, low, c)
            for i, _, o, h, low, c in con.execute(_RAW_SQL, [day, picked]).fetchall()
        }
        for instrument_id in picked:
            bars = sorted(rows[instrument_id], key=lambda r: r.bar_start)
            out.append(
                Day5m(
                    instrument_id,
                    day,
                    [r.bar_start for r in bars],
                    [(float(r.open), float(r.high), float(r.low), float(r.close)) for r in bars],
                    daily.get(instrument_id),
                )
            )
    return out


def gather(
    store: DataStore,
    session: Session,
    market: MarketData,
    snapshot: Mapping[str, Any],
    start: date,
    end: date,
    *,
    tolerance: Decimal,
    used_5m: bool = False,
) -> dict[str, Any]:
    """The audit document of the market a run of `start..end` reads (`backtest.data.load_market`
    and its snapshot): reads what the checks need beyond it (the calendar, the store's duplicate
    keys, the sampled bars, features, breadth and 5-minute bars) and runs every check."""
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
        feature_samples = _feature_samples(store, con, market, in_window)
        breadth_samples = _breadth_samples(store, con, market, in_window)
        bars_5m = _bars_5m(store.root, con, market, in_window)
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
        _check(
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
        check_bars_5m(bars_5m, float(tolerance), used=used_5m)
        if bars_5m
        else _check("bars_5m", {"days": 0, "used_by_run": used_5m}, []) | {"status": SKIPPED},
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

    from ..backtest.data import load_market  # noqa: PLC0415 - the market as a run reads it
    from ..backtest.evidence import universe_live_version  # noqa: PLC0415
    from ..backtest.spec import BacktestError  # noqa: PLC0415
    from ..engine.portfolio import regime  # noqa: PLC0415
    from ..models import HatUniverse  # noqa: PLC0415
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.data.audit")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--universe", required=True)
    parser.add_argument("--index", default=regime.INDEX_NAME)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    settings = operator_settings()
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
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
        key = cache_key(str(snapshot["content_digest"]), args.start, args.end, used_5m=False)
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
                tolerance=settings.history_envelope_tol,
            ),
            refresh=args.refresh,
        )
    sys.stdout.write(json.dumps(summary(doc, path), indent=2) + "\n")
    return 2 if doc.get("status") == FAIL else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

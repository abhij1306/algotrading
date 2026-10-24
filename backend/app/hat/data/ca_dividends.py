"""The dividend import (CA3): equity dividends from NSE's corporate-actions feed, and the value a
bonus of debentures or preference shares hands out, stored in `hat_corporate_action` as
price-neutral `dividend` rows (factor 1) with the cash per share in `cash_amount`.

Inputs, all untrusted and read as text:
- the feed: a directory of `ca_YYYY-MM.json` files, each the JSON list NSE's corporate-actions
  API (`/api/corporates-corporateActions?index=equities`) answers for one month (every purpose
  type; only dividends are read). Pulled once by the operator; no HAT code calls NSE.
- optionally the research file f (`result_f_bonus_debentures.csv`): the value per share of a
  bonus of debentures, NCRPS or preference shares (D1 keeps them price-neutral in the view). A
  high or medium confidence row is a distribution of that value, a low one goes to review.

A feed row's purpose text names one or more dividends ("Interim Dividend - Rs 5 Per Share /
Special Dividend - Rs 10 Per Share"); each is a `Payout` of a kind (interim, final, special)
and rupees per share (`payouts`). A dividend given as a percentage of the face value, a
dividend on preference shares and a text with no single amount are never guessed: review or
skipped. NSE keeps revisions as separate rows: the rows of one (ISIN, ex-date) are combined
(`combine`), identical ones once, different kinds added, and a disagreement on one kind goes to
review unless the revised rows settle it.

Each event is then checked against the raw bars: the last close before the ex-date (within
`ca_import.BAR_DAYS`) must exist, and the cash may be at most `MAX_YIELD` of it; anything else
goes to review, never into the database. The row goes on the instrument that holds that bar.
A feed dividend and a bonus distribution of the same instrument and ex-date are one row (the
amounts add up: the key is (instrument, ex-date, `dividend`)).

Against what is stored on that ex-date for the company (any ISIN of its lineage chain): the same
amount is `already`; a stored dividend without an amount (the nightly MCP lookup stores none)
gets the amount (`stored`, reason `amount_added`); another amount is a `conflict`, never
rewritten. A key the owner vacated is never re-created. `--apply` writes in one transaction and
journals each row as `corporate_action.imported`; a repeat stores nothing.

A stored amount moves the total-return series the signals read (`store.bars_daily`) and pays
the cash a backtest position earns on the ex-date; the price basis (`bars_daily_cap`), the
rescale and the GTT check read only the price-changing rows. After `--apply`, run `lineage
build` and `features --full`: the stored features' inputs digest covers the dividends.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events
from ..models import HatCorporateAction
from ..money import exact, require_exact
from .ca_import import (
    ALREADY,
    CONFLICT,
    EVENT_IMPORTED,
    REVIEW,
    SKIPPED,
    STATUSES,
    STORED,
    TEXT_MAX,
    Bar,
    CaFileError,
    Reference,
    Verdict,
    fmt4,
    gate,
    load_reference,
    nse_date,
    read_windows,
)
from .corporate_actions import TYPE_DIVIDEND, ActionRecord, load_actions, owner_vacated
from .store import DATASET_BARS, DataStore

SOURCE_FEED = "nse_ca_feed"
SOURCE_RESEARCH = "ca_research"
T_FEED = "feed"
T_BONUS = "bonus_research"
TABLES = (T_FEED, T_BONUS)
FROM_DATE = date(2011, 1, 1)
# A cash amount above half the previous close is not a dividend the bars can carry (the largest
# real ones, HINDZINC's and VEDL's specials, are under a fifth): it is a misparse or a wrong row.
MAX_YIELD = Decimal("0.5")
# A bonus of debentures or preference shares is cash to a holder only from this share of the
# previous close on (the owner's rule, CA3): below it the distribution stays price-neutral, as D1
# keeps it, because its ex-date gap is lost in a day's noise and the bars cannot place it.
BONUS_MIN_SHARE = Decimal("0.01")
MAX_FILE_BYTES = 20_000_000
MAX_FILES = 400
MAX_ROWS = 5000  # research file f
SME_SERIES = frozenset({"SM", "ST"})
# InvIT and REIT units and Indian depository receipts: their distributions are not dividends
NON_EQUITY_SERIES = frozenset({"IV", "RR", "DR"})

KIND_INTERIM = "interim"
KIND_FINAL = "final"
KIND_SPECIAL = "special"
KIND_BONUS = "bonus_instrument"
BONUS_COLUMNS = (
    "symbol",
    "isin",
    "ex_date",
    "instrument",
    "value_per_share_rs",
    "value_basis",
    "confidence",
    "source_url",
    "quote",
    "notes",
)
STORED_CONFIDENCE = frozenset({"high", "medium"})
REASON_AMOUNT_ADDED = "amount_added"

# --- parsing (pure) -----------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Payout:
    """One cash distribution of a purpose text: its kind and rupees per share."""

    kind: str
    amount: Decimal


_DIVIDEND = re.compile(r"dividend")
_MONEY = re.compile(r"(?:\b(?:rs|re|inr)\b\.?|₹)\s*(?:[-:]\s*)?(\d+(?:\.\d+)?)")
_PERCENT = re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s*%")
# where one purpose ends and the next begins: "/" (not the "/-" after an amount), "and", "&", ...
_SEP = re.compile(r"/(?!-)|\band\b|&|\+|;|,")
# a bonus of debentures or preference shares in a feed purpose (`named_bonus_dates`)
_BONUS_INSTRUMENT = re.compile(r"bonus\b.*\b(?:debentures?|deb|ncds?|n?c?rps|pref\w*)\b")
_PREFERENCE = re.compile(r"preference|\bpref\b|\bc?n?c?rps\b|\bcps\b")


_KIND = re.compile(r"\b(final|special|interim)\b")
_KIND_STOP = re.compile(r"[0-9%/]")


def _kinded(body: str) -> list[tuple[str, str]]:
    """(kind, amount) for each kind word followed by a currency amount with no digit, `%` or `/`
    between them ("Final Rs 4/- And Special Re 1/-"); a kind word inside an earlier match is
    part of it. Two plain scans, so the time is linear in the text."""
    found: list[tuple[str, str]] = []
    end = 0
    for kind in _KIND.finditer(body):
        if kind.start() < end:
            continue
        money = _MONEY.search(body, kind.end())
        stop = _KIND_STOP.search(body, kind.end())
        if money is None or (stop is not None and stop.start() < money.start()):
            continue
        found.append((kind.group(1), money.group(1)))
        end = money.end()
    return found


# "Interim Dividend 3 Per Share": an amount without the currency, only right before "per share"
_BARE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:/-\s*)?per\s*(?:equity\s*)?sh")


def _kind(words: str) -> str:
    if "special" in words:
        return KIND_SPECIAL
    if "interim" in words:
        return KIND_INTERIM
    return KIND_FINAL  # "Dividend" at an AGM, or "Final Dividend"


# another purpose in the same text ends a dividend's words ("... / Face Value Split - From Rs 10")
_OTHER_PURPOSE = re.compile(
    r"split|sub-?\s*division|rights|bonus|consolidat|scheme|buy\s*-?back|face\s*val|interest"
)


def _clause(prefix: str, body: str) -> list[Payout] | str:
    """The payouts of one "dividend" mention: `prefix` the words before it in its clause, `body`
    the words after it up to the next mention."""
    other = _OTHER_PURPOSE.search(body)
    body = body[: other.start()] if other else body
    if _PREFERENCE.search(prefix) or _PREFERENCE.search(body):
        return "preference_dividend"
    amounts = _MONEY.findall(body)
    if len(amounts) > 1:
        # "Dividend - Final Rs 4/- And Special Re 1/- Per Share": each amount named by its kind
        kinded = _kinded(body)
        if len(kinded) != len(amounts) or len({k for k, _ in kinded}) != len(kinded):
            return "several_amounts"
        found = [Payout(_kind(k), require_exact(a, "amount")) for k, a in kinded]
    elif amounts:
        found = [Payout(_kind(prefix), require_exact(amounts[0], "amount"))]
    else:
        bare = _BARE.findall(body)
        if len(bare) == 1:
            found = [Payout(_kind(prefix), require_exact(bare[0], "amount"))]
        elif "(" in prefix and ")" not in prefix.split("(")[-1]:
            return []  # "Rs 50/- Per Share (Special Dividend)": a label of the amount before
        else:
            return "percent_of_face_value" if _PERCENT.search(body) else "no_amount"
    if any(x.amount <= 0 for x in found):
        return "no_amount"
    return found


def payouts(text: str) -> tuple[Payout, ...] | str:
    """The dividends a purpose text names, or why it gives none it can trust: `no_dividend`,
    `preference_dividend` (only dividends on preference shares), `percent_of_face_value`,
    `several_amounts` (one clause with amounts not each named by its kind) or `no_amount`. One
    clause the text cannot be read for is enough: a partial sum is never returned."""
    lowered = re.sub(r"\s+", " ", text.lower())
    found = list(_DIVIDEND.finditer(lowered))
    if not found:
        return "no_dividend"
    out: list[Payout] = []
    inside: list[Payout] = []  # named in brackets: a breakdown of the amount before them
    preference = False
    for n, match in enumerate(found):
        start = found[n - 1].end() if n else 0
        stop = found[n + 1].start() if n + 1 < len(found) else len(lowered)
        before = lowered[start : match.start()]
        seps = list(_SEP.finditer(before))
        prefix = before[seps[-1].end() :] if seps else before
        parts = _clause(prefix, lowered[match.end() : stop])
        if parts == "preference_dividend":
            preference = True
        elif isinstance(parts, str):
            return parts
        elif lowered[: match.start()].count("(") > lowered[: match.start()].count(")"):
            inside.extend(parts)
        else:
            out.extend(parts)
    if inside:
        # "Dividend Rs.50/- Per Share (Special Dividend Rs.25/- ... And Final Dividend Rs.25)"
        # "Dividend-Rs.8.50 Per Share (Including Special Dividend Of Rs.2/- Per Share)"
        rest = total(out) - total(inside)
        if len(out) == 1 and rest > 0 and "includ" in lowered:
            out = [Payout(out[0].kind, rest), *inside]
        elif out and rest != 0:
            return "breakdown_disagrees"
        else:
            out = inside
    if not out:
        return "preference_dividend" if preference else "no_amount"
    return tuple(out)


def equity_row_isin(isin: str) -> bool:
    """An equity share's ISIN as the feed gives it: INE (ordinary) or IN9 (DVR shares), or none
    (matched by symbol); not an ETF unit (INF) or a debt security."""
    return not isin or (len(isin) == 12 and isin[:3] in ("INE", "IN9"))


def total(parts: Iterable[Payout]) -> Decimal:
    return sum((p.amount for p in parts), Decimal(0))


@dataclass(frozen=True)
class FeedRow:
    symbol: str
    isin: str
    series: str
    ex_date: date | None
    subject: str


def combine(rows: Sequence[FeedRow]) -> tuple[Payout, ...] | str:
    """The payouts of one (ISIN, ex-date)'s feed rows: identical rows count once, rows naming
    different kinds add up, and rows that give one kind different amounts are settled by the
    revised rows when those agree ("(Purpose Revised)"), else `rows_disagree`. A row that cannot
    be read makes the event unreadable (its reason), unless another row of it reads and the
    unreadable one names no amount at all."""
    read: dict[str, tuple[Payout, ...]] = {}
    failed: list[str] = []
    for row in rows:
        found = payouts(row.subject)
        if isinstance(found, str):
            failed.append(found)
        else:
            read[row.subject] = tuple(sorted(found))
    hard = [r for r in failed if r not in ("no_amount", "preference_dividend", "no_dividend")]
    if hard:
        return hard[0]
    if not read:
        return failed[0] if failed else "no_dividend"
    distinct = set(read.values())
    if len(distinct) == 1:
        return next(iter(distinct))
    kinds = [{p.kind for p in parts} for parts in distinct]
    if all(not (a & b) for i, a in enumerate(kinds) for b in kinds[i + 1 :]):
        return tuple(sorted(p for parts in distinct for p in parts))
    revised = {parts for subject, parts in read.items() if "revised" in subject.lower()}
    if len(revised) == 1:
        return next(iter(revised))
    return "rows_disagree"


_SOME_HOLDERS = re.compile(
    r"non[- ]?promoter|promoters? excluded|only (?:to |for )?(?:the )?public"
)


def bonus_value(row: Mapping[str, str]) -> tuple[Payout, ...] | str:
    """A research file f row's distribution, or why it is not stored: `low_confidence`,
    `some_holders_only` (not every share received it, so it is no per-share distribution:
    MBLIMITED 2023's NCRPS went to non-promoters only), `bad_value`."""
    if row["confidence"].strip().lower() not in STORED_CONFIDENCE:
        return "low_confidence"
    if _SOME_HOLDERS.search(f"{row['instrument']} {row['notes']}".lower()):
        return "some_holders_only"
    value = exact(row["value_per_share_rs"].strip(), positive=True)
    if value is None:
        return "bad_value"
    return (Payout(KIND_BONUS, value),)


def over_yield(amount: Decimal, previous_close: Decimal) -> bool:
    """True when the cash is more than `MAX_YIELD` of the previous close (or the close is not
    positive): such an amount goes to review, never into the database."""
    return previous_close <= 0 or amount > MAX_YIELD * previous_close


def bonus_counts(value: Decimal, previous_close: Decimal) -> bool:
    """True when a bonus distribution of `value` per share is at least `BONUS_MIN_SHARE` of the
    previous close, so it is stored as a dividend; below it, it stays price-neutral."""
    return previous_close > 0 and value >= BONUS_MIN_SHARE * previous_close


def describe(parts: Sequence[Payout]) -> str:
    """`interim 5 + special 10`: the stored text's summary of the payouts."""
    return " + ".join(f"{p.kind} {p.amount.normalize():f}" for p in parts)


# --- input files --------------------------------------------------------------------------------


def _cell(value: object) -> str:
    return ("" if value is None else str(value)).strip()[:TEXT_MAX]


_FEED_FILE = re.compile(r"ca_\d{4}-\d{2}\.json")


def read_feed(directory: Path) -> tuple[list[FeedRow], str]:
    """Every row of the feed files in `directory` (`ca_YYYY-MM.json`), with the digest of the
    files read. A file too large, not a JSON list, or too many files is refused (`CaFileError`);
    an element that is not an object is skipped."""
    files = sorted(p for p in directory.iterdir() if _FEED_FILE.fullmatch(p.name))
    if not files:
        raise CaFileError("feed_missing")
    if len(files) > MAX_FILES:
        raise CaFileError("feed_too_large")
    sha = hashlib.sha256()
    rows: list[FeedRow] = []
    for path in files:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CaFileError("feed_too_large")
        data = path.read_bytes()
        sha.update(path.name.encode() + b"\0" + data)
        try:
            items = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CaFileError("feed_unreadable") from exc
        if not isinstance(items, list):
            raise CaFileError("feed_unreadable")
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append(
                FeedRow(
                    _cell(item.get("symbol")).upper(),
                    _cell(item.get("isin")).upper(),
                    _cell(item.get("series")).upper(),
                    nse_date(_cell(item.get("exDate"))),
                    _cell(item.get("subject")),
                )
            )
    return rows, sha.hexdigest()


def read_bonus_file(path: Path) -> list[dict[str, str]]:
    """Research file f's rows as text; refuses a file too large, not UTF-8 or missing a
    column."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CaFileError("research_file_too_large")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaFileError("research_file_unreadable") from exc
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not set(BONUS_COLUMNS) <= set(reader.fieldnames or ()):
            raise CaFileError("research_file_columns")
        out = []
        for row in reader:
            if len(out) >= MAX_ROWS:
                raise CaFileError("research_file_too_large")
            out.append({k: _cell(row.get(k)) for k in BONUS_COLUMNS})
    except csv.Error as exc:
        raise CaFileError("research_file_unreadable") from exc
    return out


def _any_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return nse_date(raw)


# --- proposals ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """One event an input names: a feed (ISIN, ex-date) group or one research f row."""

    table: str
    rowid: str
    symbol: str
    isin: str
    ex_date: date | None
    text: str
    parts: tuple[Payout, ...] = ()
    hint: str = ""
    status: str = ""
    reason: str = ""

    def ends(self, status: str, reason: str) -> Proposal:
        return replace(self, status=status, reason=reason)


def feed_proposals(rows: Iterable[FeedRow]) -> list[Proposal]:
    """One proposal per (ISIN or symbol, ex-date) of the feed's dividend rows."""
    groups: dict[tuple[str, date | None], list[FeedRow]] = defaultdict(list)
    for row in rows:
        if _DIVIDEND.search(row.subject.lower()):
            groups[(row.isin or row.symbol, row.ex_date)].append(row)
    out = []
    for (key, ex_date), members in sorted(groups.items(), key=lambda g: (str(g[0][1]), g[0][0])):
        first = members[0]
        subjects = list(dict.fromkeys(m.subject for m in members))
        p = Proposal(
            T_FEED, key, first.symbol, first.isin, ex_date, " | ".join(subjects), hint=first.series
        )
        if ex_date is None:
            out.append(p.ends(REVIEW, "bad_ex_date"))
        elif ex_date < FROM_DATE:
            out.append(p.ends(SKIPPED, "before_window"))
        elif first.series in NON_EQUITY_SERIES or not equity_row_isin(first.isin):
            out.append(p.ends(SKIPPED, "not_equity_share"))
        else:
            parts = combine(members)
            if isinstance(parts, str):
                skip = parts in ("preference_dividend", "no_dividend")
                out.append(p.ends(SKIPPED if skip else REVIEW, parts))
            else:
                out.append(replace(p, parts=parts))
    return out


def bonus_proposals(rows: Sequence[Mapping[str, str]]) -> list[Proposal]:
    out = []
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        text = (
            f"{row['instrument'] or 'Bonus instrument'}: distribution Rs "
            f"{row['value_per_share_rs']} per share ({row['value_basis'] or 'value'}; research f "
            f"{row['confidence'].lower() or 'no confidence'})"
        )
        p = Proposal(
            T_BONUS,
            str(line),
            row["symbol"].upper(),
            row["isin"].upper(),
            _any_date(row["ex_date"]),
            text,
            hint=f"{row['source_url'][:200]} {row['notes'][:200]}".strip(),
        )
        parts = bonus_value(row)
        if p.ex_date is None:
            out.append(p.ends(REVIEW, "bad_ex_date"))
        elif isinstance(parts, str):
            out.append(p.ends(REVIEW, parts))
        else:
            out.append(replace(p, parts=parts))
    return out


# --- deciding -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredDividend:
    instrument_id: str
    ex_date: date
    amount: Decimal | None


@dataclass
class Outcome:
    proposal: Proposal
    status: str
    reason: str = ""
    detail: str = ""
    record: ActionRecord | None = None
    fill: StoredDividend | None = None  # a stored dividend whose missing amount this sets
    merged_into: Outcome | None = None  # the outcome whose row carries this one's payouts too
    evidence: dict[str, Any] = field(default_factory=dict)


def previous_bar(bars: Iterable[Bar], ex_date: date, isin: str) -> Bar | str:
    """The last bar before the ex-date (within `BAR_DAYS`), or `no_bars`, or `ambiguous_bars`
    when two instruments trade that day and the row's ISIN names neither."""
    before = [b for b in bars if b.day < ex_date]
    if not before:
        return "no_bars"
    last = max(b.day for b in before)
    found = [b for b in before if b.day == last]
    if len(found) > 1:
        found = [b for b in found if b.isin == isin]
    return found[0] if len(found) == 1 else "ambiguous_bars"


def _bar_on(bars: Iterable[Bar], day: date, isin: str) -> Bar | None:
    found = [b for b in bars if b.day == day]
    if len(found) > 1:
        found = [b for b in found if b.isin == isin]
    return found[0] if len(found) == 1 else None


NAMED_BEFORE = 7  # NSE's ex-date of a bonus may be this many days before the research date
NAMED_AFTER = 3  # (a record date) or this many after it


def bonus_ex_date(
    research_date: date, named: Iterable[date], bars: Sequence[Bar], isin: str, value: Decimal
) -> tuple[date, str] | str:
    """The ex-date of a bonus distribution of `value` per share whose research date may be the
    record date: NSE's own ex-date for the company's non-equity bonus (`named`, the feed's) within
    `NAMED_BEFORE`/`NAMED_AFTER` days, kept unless the bars' gap rules it out (`off_factor` at the
    open, unless the close confirms the gap: the open can lag a large distribution); with none,
    the one of the research date and the session before it (T+1 and T+2 settlement) whose gap the
    bars confirm (`ca_import.gate` on 1 - value / previous close). Else the reason for review:
    `several_ex_dates`, `no_bars`, `value_vs_price`, `gap_disagrees`, `ex_date_unconfirmed`,
    `ex_date_ambiguous`."""
    near = sorted(
        {
            d
            for d in named
            if research_date - timedelta(days=NAMED_BEFORE)
            <= d
            <= research_date + timedelta(days=NAMED_AFTER)
        }
    )
    if len(near) > 1:
        return "several_ex_dates"
    sessions = sorted({b.day for b in bars})
    earlier = [d for d in sessions if d < research_date]
    candidates = near or sorted({research_date, *earlier[-1:]})
    verdicts: dict[date, Verdict] = {}
    by_close: dict[date, Verdict] = {}  # the ex session's close as the witness
    for day in candidates:
        before, on = previous_bar(bars, day, isin), _bar_on(bars, day, isin)
        if isinstance(before, str) or on is None or before.close <= 0 or on.open <= 0:
            continue
        expected = 1 - Fraction(value) / Fraction(before.close)
        if expected <= 0:
            return "value_vs_price"
        verdicts[day] = gate(expected, Fraction(before.close), Fraction(on.open))
        if on.close > 0:
            by_close[day] = gate(expected, Fraction(before.close), Fraction(on.close))
    if near:
        verdict = verdicts.get(near[0])
        if verdict is None:
            return "no_bars"
        close = by_close.get(near[0])
        if verdict.reason == "off_factor" and close is not None and close.confirmed:
            # the open can lag a large distribution (SUNDARAM-CLAYTON 2023: open 0.92, close 0.80)
            return near[0], f"NSE ex-date, open gap {fmt4(verdict.gap)}, close {fmt4(close.gap)}"
        if verdict.reason == "off_factor":
            return "gap_disagrees"
        return near[0], f"NSE ex-date, gap {fmt4(verdict.gap)} {verdict.reason or 'confirmed'}"
    if not verdicts:
        return "no_bars"
    confirmed = [d for d, v in verdicts.items() if v.confirmed]
    if len(confirmed) > 1:
        return "ex_date_ambiguous"
    if not confirmed:
        return "ex_date_unconfirmed"
    return confirmed[0], f"bars confirm, gap {fmt4(verdicts[confirmed[0]].gap)}"


def decide(
    p: Proposal,
    reference: Reference,
    bars: Sequence[Bar],
    named: Iterable[date] = (),
    first_bar: date | None = None,
) -> Outcome:
    """A proposal's candidate row on the instrument of the bar before its ex-date, or why not:
    no instrument (skipped: no bars anywhere), no bar, an amount over `MAX_YIELD`, a bonus
    distribution under `BONUS_MIN_SHARE` of the close (skipped: it stays price-neutral) or one
    whose ex-date is not settled (`bonus_ex_date`)."""
    if p.status or p.ex_date is None:
        return Outcome(p, p.status or REVIEW, p.reason or "bad_ex_date")
    if first_bar is not None and p.ex_date <= first_bar:
        return Outcome(p, SKIPPED, "before_bars")  # no instrument has a bar to hold it
    if not reference.ids(p.symbol, p.isin, p.ex_date):
        sme = p.hint in SME_SERIES
        return Outcome(p, SKIPPED if p.table == T_FEED or sme else REVIEW, "no_instrument")
    amount = total(p.parts)
    placed = ""
    if p.table == T_BONUS:
        near = previous_bar(bars, p.ex_date, p.isin)
        if not isinstance(near, str) and not bonus_counts(amount, near.close):
            return _below_share(p, amount, near)
        found = bonus_ex_date(p.ex_date, named, bars, p.isin, amount)
        if isinstance(found, str):
            return Outcome(p, REVIEW, found)
        ex_date, placed = found
        if ex_date != p.ex_date:
            placed += f", research date {p.ex_date} moved"
        p = replace(p, ex_date=ex_date)
    assert p.ex_date is not None
    before = previous_bar(bars, p.ex_date, p.isin)
    if isinstance(before, str):
        return Outcome(p, REVIEW, before)
    ratio = amount / before.close if before.close > 0 else Decimal(0)
    detail = f"Rs {amount.normalize():f} vs prev {before.day} close {before.close} ({ratio:.2%})"
    if placed:
        detail += f"; {placed}"
    if over_yield(amount, before.close):
        return Outcome(p, REVIEW, "amount_vs_price", detail)
    if p.table == T_BONUS and not bonus_counts(amount, before.close):
        return _below_share(p, amount, before)
    record = ActionRecord(
        uuid.UUID(before.instrument_id),
        p.ex_date,
        TYPE_DIVIDEND,
        Decimal(1),
        stored_text(p.parts, [p.text]),
        SOURCE_FEED if p.table == T_FEED else SOURCE_RESEARCH,
        amount,
    )
    evidence = {
        "previous_day": before.day.isoformat(),
        "previous_close": str(before.close),
        "payouts": [{"kind": x.kind, "amount": str(x.amount)} for x in p.parts],
    }
    if placed:
        evidence["ex_date"] = placed
    return Outcome(p, STORED, "", detail, record, evidence=evidence)


def _below_share(p: Proposal, amount: Decimal, before: Bar) -> Outcome:
    ratio = amount / before.close if before.close > 0 else Decimal(0)
    detail = f"Rs {amount.normalize():f} vs prev {before.day} close {before.close} ({ratio:.2%})"
    return Outcome(p, SKIPPED, "below_one_percent", detail)


def stored_text(parts: Sequence[Payout], texts: Sequence[str]) -> str:
    return f"Dividend {describe(parts)}: {' | '.join(texts)}"[:TEXT_MAX]


def merge(outcomes: Sequence[Outcome]) -> None:
    """Candidates of one key (a feed dividend and a bonus distribution on the same instrument and
    ex-date) become one row on the first: the amounts add up and the texts join; the others
    keep no record and follow the first's verdict (`merged_into`)."""
    by_key: dict[tuple, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        if o.record is not None:
            by_key[o.record.key].append(o)
    for group in by_key.values():
        first, rest = group[0], group[1:]
        if not rest or first.record is None:
            continue
        parts = tuple(sorted(x for o in group for x in o.proposal.parts))
        first.record = replace(
            first.record,
            cash_amount=total(parts),
            raw_text=stored_text(parts, [o.proposal.text for o in group]),
            source=SOURCE_FEED
            if any(o.proposal.table == T_FEED for o in group)
            else first.record.source,
        )
        first.evidence["payouts"] = [{"kind": x.kind, "amount": str(x.amount)} for x in parts]
        first.evidence["merged"] = [f"{o.proposal.table}:{o.proposal.rowid}" for o in rest]
        for o in rest:
            o.record, o.merged_into = None, first


def settle(
    outcomes: Sequence[Outcome],
    reference: Reference,
    stored: Mapping[str, list[StoredDividend]],
    held: set[tuple],
) -> None:
    """Each candidate against the dividends stored on its ex-date for the company (its chain):
    the same amount is `already`, a missing amount is added (`amount_added`), another is a
    `conflict`; a key the owner vacated is `skipped`. A merged outcome takes its row's verdict."""
    for o in outcomes:
        r = o.record
        if r is None:
            continue
        p = o.proposal
        ids = reference.ids(p.symbol, p.isin, r.ex_date) | {str(r.instrument_id)}
        same_day = [s for iid in ids for s in stored.get(iid, ()) if s.ex_date == r.ex_date]
        if r.key in held:
            o.status, o.reason, o.record = SKIPPED, "owner_vacated", None
        elif any(s.amount is not None and s.amount == r.cash_amount for s in same_day):
            o.status, o.record = ALREADY, None
        elif any(s.amount is not None for s in same_day):
            amounts = ", ".join(sorted({str(s.amount) for s in same_day if s.amount is not None}))
            o.status, o.reason, o.record = CONFLICT, "stored_amount_differs", None
            o.detail += f"; stored {amounts}"
        elif same_day:
            o.fill, o.reason = same_day[0], REASON_AMOUNT_ADDED
    for o in outcomes:
        if o.merged_into is not None:
            o.status, o.reason = o.merged_into.status, "merged"


def stored_dividends(session: Session) -> dict[str, list[StoredDividend]]:
    out: dict[str, list[StoredDividend]] = defaultdict(list)
    for row in session.scalars(
        select(HatCorporateAction).where(HatCorporateAction.action_type == TYPE_DIVIDEND)
    ):
        amount = None if row.cash_amount is None else Decimal(row.cash_amount)
        out[str(row.instrument_id)].append(
            StoredDividend(str(row.instrument_id), row.ex_date, amount)
        )
    return out


def named_bonus_dates(feed: Iterable[FeedRow]) -> dict[str, set[date]]:
    """NSE's ex-dates of each company's non-equity bonuses, by ISIN and by symbol."""
    out: dict[str, set[date]] = defaultdict(set)
    for row in feed:
        if row.ex_date is not None and _BONUS_INSTRUMENT.search(row.subject.lower()):
            for key in (row.isin, row.symbol):
                if key:
                    out[key].add(row.ex_date)
    return out


def evaluate(
    session: Session, store: DataStore, feed: Sequence[FeedRow], bonus: Sequence[Mapping[str, str]]
) -> list[Outcome]:
    """Every event's outcome; nothing is written."""
    props = feed_proposals(feed) + bonus_proposals(bonus)
    reference = load_reference(session)
    named = named_bonus_dates(feed)
    wanted = {
        k: (p.ex_date, reference.ids(p.symbol, p.isin, p.ex_date))
        for k, p in enumerate(props)
        if not p.status and p.ex_date is not None
    }
    windows = read_windows(store, wanted)
    days = store.days(DATASET_BARS)
    first_bar = days[0] if days else None
    outcomes = [
        decide(
            p,
            reference,
            windows.get(k, ()),
            named.get(p.isin, set()) | named.get(p.symbol, set()),
            first_bar,
        )
        for k, p in enumerate(props)
    ]
    merge(outcomes)
    held = owner_vacated(
        session, {o.record.instrument_id for o in outcomes if o.record is not None}
    )
    settle(outcomes, reference, stored_dividends(session), held)
    return outcomes


# --- writing and the report ---------------------------------------------------------------------


def apply(
    session: Session, outcomes: Sequence[Outcome], *, journal: Mapping[str, Any], now: datetime
) -> tuple[int, int]:
    """Store every new row (`load_actions`), set the amount of each stored dividend that had none,
    and journal each as `corporate_action.imported` in the same transaction; returns (rows
    created, amounts added). Raises when the database disagrees with the plan (a row appeared or
    got an amount meanwhile): nothing is then kept."""
    run_id = str(uuid.uuid4())
    new = [o for o in outcomes if o.status == STORED and o.record is not None and o.fill is None]
    fills = [o for o in outcomes if o.status == STORED and o.fill is not None]
    report = load_actions(session, [o.record for o in new if o.record is not None])
    if report.created != len(new) or report.conflicts or report.held:
        raise CaFileError("ca_import_raced")
    for o in fills:
        assert o.fill is not None
        assert o.record is not None
        row = session.scalar(
            select(HatCorporateAction)
            .where(
                HatCorporateAction.instrument_id == uuid.UUID(o.fill.instrument_id),
                HatCorporateAction.ex_date == o.fill.ex_date,
                HatCorporateAction.action_type == TYPE_DIVIDEND,
            )
            .with_for_update()
        )
        if row is None or row.cash_amount is not None:
            raise CaFileError("ca_import_raced")
        row.cash_amount = o.record.cash_amount
    for o in [*new, *fills]:
        r = o.record
        assert r is not None
        instrument = uuid.UUID(o.fill.instrument_id) if o.fill is not None else r.instrument_id
        events.record(
            session,
            aggregate_type="instrument",
            aggregate_id=instrument,  # type: ignore[arg-type]
            event_type=EVENT_IMPORTED,
            actor="engine",
            payload={
                **journal,
                "run_id": run_id,
                "table": o.proposal.table,
                "row": o.proposal.rowid,
                "symbol": o.proposal.symbol,
                "isin": o.proposal.isin,
                "ex_date": r.ex_date.isoformat(),
                "type": r.action_type,
                "cash_amount": str(r.cash_amount),
                "amount_added": o.fill is not None,
                "text": r.raw_text[:500],
                "check": o.evidence,
            },
            occurred_at=now,
        )
    session.flush()
    return report.created, len(fills)


def report_lines(outcomes: Sequence[Outcome], *, applied: bool, everything: bool) -> list[str]:
    """Counts per input and status, the reasons, then the conflict and review rows (every row
    when `everything`, the stored ones too)."""
    counts: Counter[tuple[str, str]] = Counter((o.proposal.table, o.status) for o in outcomes)
    label = {STORED: "stored" if applied else "to_store"}
    lines = [
        "input               " + " ".join(f"{label.get(s, s):>9}" for s in STATUSES) + "    total"
    ]
    for table in TABLES:
        cells = [counts[(table, s)] for s in STATUSES]
        lines.append(f"{table:<20}" + " ".join(f"{c:>9}" for c in cells) + f" {sum(cells):>8}")
    added = sum(1 for o in outcomes if o.status == STORED and o.fill is not None)
    rows = sum(
        1 for o in outcomes if o.status == STORED and o.record is not None and o.fill is None
    )
    lines.append(
        f"rows {'written' if applied else 'to write'}: dividend {rows}, amounts added {added}"
    )
    for table in TABLES:
        reasons = Counter(
            (o.status, o.reason) for o in outcomes if o.proposal.table == table and o.reason
        )
        lines.append(
            f"reasons {table}: "
            + ", ".join(f"{s}/{r} {n}" for (s, r), n in sorted(reasons.items()))
        )
    shown = STATUSES if everything else (CONFLICT, REVIEW)
    for status in shown:
        for o in outcomes:
            if o.status != status:
                continue
            p = o.proposal
            amount = o.record.cash_amount if o.record is not None else total(p.parts)
            lines.append(
                "\t".join(
                    [
                        label.get(o.status, o.status).upper(),
                        o.reason,
                        p.table,
                        p.symbol,
                        p.isin,
                        p.ex_date.isoformat() if p.ex_date else "-",
                        f"{amount.normalize():f}" if amount else "-",
                        p.text[:160],
                        o.detail,
                        p.hint[:160],
                    ]
                )
            )
    return lines


# --- command line -------------------------------------------------------------------------------


def input_path(name: str, *, directory: bool = False, suffix: str = "") -> Path:
    """A path named on the command line, under the working directory (a directory, or a file with
    `suffix`): anything else is refused before it is opened."""
    base = Path.cwd().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or (not directory and target.suffix.lower() != suffix):
        raise CaFileError("ca_file_outside_cwd")
    if not (target.is_dir() if directory else target.is_file()):
        raise CaFileError("ca_file_missing")
    return target


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_dividends --feed DIR [--bonus F.csv] [--apply] [--all]

    Reads NSE's corporate-actions feed (DIR of `ca_YYYY-MM.json`) and the research file f (both
    under the working directory, read-only) and the raw bars under `HAT_DATA_ROOT`, and prints
    what it would store (a dry run) or, with `--apply`, stores it in one transaction. The
    report goes to stdout; `--all` lists every event. Dividends change no price: no lineage or
    features rebuild is needed for them."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_dividends")
    parser.add_argument("--feed", required=True)
    parser.add_argument("--bonus", help="research file f (.csv)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        feed_dir = input_path(args.feed, directory=True)
        feed, feed_sha = read_feed(feed_dir)
        bonus_path = input_path(args.bonus, suffix=".csv") if args.bonus else None
        bonus = read_bonus_file(bonus_path) if bonus_path else []
    except CaFileError as exc:
        sys.stderr.write(f"refused: {exc.code}\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from ..time import utc_now

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatSessionLocal() as session:
        outcomes = evaluate(session, store, feed, bonus)
        if args.apply:
            journal: dict[str, Any] = {"source": SOURCE_FEED, "feed_sha256": feed_sha}
            if bonus_path is not None:
                journal["research_sha256"] = {
                    "f": hashlib.sha256(bonus_path.read_bytes()).hexdigest()
                }
            created, added = apply(session, outcomes, journal=journal, now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows, {added} amounts added\n")
        else:
            session.rollback()
    lines = report_lines(outcomes, applied=args.apply, everything=args.all)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

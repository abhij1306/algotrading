"""The corporate-action research import (CA2, D241/D245): demerger factors and rights terms
that web research resolved for rows the CA1 file import (`ca_import`) left in review.

Inputs: CA1's NSE file (the base: a research row only overrides an event NSE recorded) and the
research CSVs, all outside the repo and untrusted: read as text, cut to `TEXT_MAX`, every number
parsed by this module; a row that does not parse or match goes to the report, never into the
database. Each research row carries a confidence (high/medium/low), a source URL and a quote.

- `a` and `b`: demergers CA1 could not price (per resulting company: ratio, cost-of-acquisition
  split, listing), plus `b`'s rights rows with no price or non-simple terms.
- `d`: later, better-sourced values for events of `a` and `b`. An event `d` names is taken from
  `d` alone (`merge_events`); each of its legs keeps the leg type and exclusion of the `a`/`b`
  leg of the same company.
- `c`: rights rows CA1 found `ambiguous` (the terms checked against sources).

A research row matches its file row by symbol and ex-date (`match_rows`); an ISIN on both sides
must agree unless the file's is a placeholder.

Demergers (`decide_demerger`, owner rule): the stored factor is the MARKET factor, the ex-date
open over the previous close (NSE's special pre-open discovers it), never a cost-of-acquisition
% (a book-value tax allocation: TMPV 2025-10-14 CoA 68.85% against a market 0.605) nor a ratio
priced at a resulting company's close. Those two are cross-checks (`cross_checks`): the parent's
CoA % / 100 (`coa_factor`) and 1 - sum(r_i * C_i) / P (`ratio_factor`, each resulting company
priced through a known instrument: its first close, or its close at the ex-date when it already
traded). The gap is stored (`market_factor`) when a cross-check agrees with it by CA1's gate (within
20%, and twice as close to it as to 1), or, with no cross-check or none agreeing (a CoA may well
differ from the market: VEDL's five-way 2026 demerger), when a source confirms the date, the drop
is beyond `DEMERGER_MIN_DROP` (opens 10% or more below the previous close are 0.28% of the 6.8
million sessions in the raw bars, actions included, so on a date a source names such a drop is
the event) and the price holds it five sessions on. Anything else goes to review. The stored text
carries the method (`market_gap`) and the cross-check values, each marked `disagrees` when it
does. Amalgamation legs are not
cross-checks for the parent. An event that distributes no shares (not a demerger, preference shares
only, no shares to holders) is reported, never stored; a low-confidence event, or one whose date
the research disputes, goes to review. The resulting company is never linked into the parent's
lineage.

Rights (`rights_override`): the TERP factor of CA1 (`rights_factor`) with the terms the research
confirms, or corrects when it says ours are wrong and gives the right ones. A partly-paid issue
uses the full issue price (the TERP convention). The gate's `ambiguous` still stores, the text
tagged `price_unconfirmed`; `off_factor`, low confidence, warrants or convertibles alongside, and
terms the research flags without correcting go to review.

Rows are stored with source `ca_research` and journalled `corporate_action.imported` with the
digests of every input file and each row's research file, line, confidence and source URL. A
stored key is never rewritten (a conflict is reported; `correct-action` replaces it).

Point in time (CA5, `ca_known`): a research row may carry a `source_date` column, the date its
source was published. A demerger is known on the first route that accepts its market gap: an
agreeing ratio check at the latest of the resulting company's priced close and the legs' source
dates, an agreeing CoA check at the legs' source dates, or the close `SETTLE_SESSIONS` on. A rights
factor resting on terms only research gives (`research_supplied`) is known at its source date (not
known without one); one whose terms NSE's text already gave, at the ex-date open.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..money import exact
from .ca_import import (
    CHILD_DAYS,
    DEMERGER_MIN_DROP,
    KIND_DEMERGER,
    REVIEW,
    SKIPPED,
    STORED,
    T_DEMERGERS,
    T_RIGHTS,
    TEXT_MAX,
    Bar,
    CaFileError,
    Context,
    Instrument,
    Leg,
    Outcome,
    Proposal,
    RightsTerms,
    around,
    decide,
    dedupe,
    equity_share_isin,
    file_digest,
    fmt4,
    gate,
    load_reference,
    near_actions,
    nse_date,
    owner_vacated,
    parse_ratio,
    read_file,
    read_first_closes,
    read_windows,
    rights_terms,
    settle,
    stored_price_actions,
)
from .ca_known import (
    RESEARCH_SOURCE_DATE,
    RESULTING_FIRST_CLOSE,
    SETTLE_SESSIONS,
    Known,
    earliest,
    from_sources,
    settle_known,
)
from .corporate_actions import TYPE_OTHER
from .store import DataStore

SOURCE_RESEARCH = "ca_research"
PRICE_UNCONFIRMED = "price_unconfirmed"
MARKET_GAP = "market_gap"
CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
COA_SUM_TOL = Fraction(1, 2)  # the parent's and the resulting companies' % add up to 100 +- this
RECORD_DAYS = 7  # a record date falls this soon after the ex-date (T+2 then T+1 settlement)
DISPUTE_DAYS = 60  # a record or ex date this close to the ex-date but off it disputes the event
MAX_ROWS = 5000
MAX_BYTES = 5_000_000
SOURCE_DATE = "source_date"  # the optional column: when the row's source was published (CA5)

FILE_A, FILE_B, FILE_C, FILE_D = "a", "b", "c", "d"
REQUIRED: dict[str, tuple[str, ...]] = {
    FILE_A: (
        "parent_symbol",
        "parent_isin",
        "ex_date",
        "is_demerger",
        "resulting_name",
        "resulting_nse_symbol",
        "resulting_isin",
        "ratio_a",
        "ratio_b",
        "coa_parent_pct",
        "coa_resulting_pct",
        "non_equity_consideration",
        "confidence",
        "source_url",
        "quote",
        "notes",
    ),
    FILE_B: (
        "table",
        "symbol",
        "isin",
        "ex_date",
        "resulting_name",
        "resulting_nse_symbol",
        "resulting_isin",
        "ratio_a",
        "ratio_b",
        "leg_type",
        "issue_price",
        "partly_paid",
        "coa_parent_pct",
        "coa_resulting_pct",
        "non_equity_consideration",
        "our_data_wrong",
        "confidence",
        "source_url",
        "quote",
        "notes",
    ),
    FILE_C: (
        "symbol",
        "isin",
        "ex_date",
        "ratio_m",
        "ratio_n",
        "face_value",
        "issue_price",
        "partly_paid",
        "other_instruments",
        "our_data_wrong",
        "confidence",
        "source_url",
        "quote",
        "notes",
    ),
    FILE_D: (
        "source_file",
        "symbol",
        "ex_date",
        "resulting_name",
        "resulting_nse_symbol",
        "ratio_a",
        "ratio_b",
        "coa_parent_pct",
        "coa_resulting_pct",
        "corrected_ex_or_record_date",
        "confidence",
        "source_url",
        "quote",
        "notes",
    ),
}
TABLES = (
    f"{FILE_A}:demergers",
    f"{FILE_B}:demergers",
    f"{FILE_D}:demergers",
    f"{FILE_B}:rights",
    f"{FILE_C}:rights",
    "unmatched",
)

# --- parsing (pure) -----------------------------------------------------------------------------

_DECIMAL = re.compile(r"\d+(?:\.\d+)?")
_AMOUNT = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"  # "1,000" and "1,00,000" too
_LEADING_NUMBER = re.compile(rf"\s*(?:rs\.?\s*)?{_AMOUNT}\b", re.IGNORECASE)
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&-]{0,19}")


def number(raw: str) -> Fraction | None:
    """A plain decimal cell (`68.85`) as a number, else None."""
    cell = raw.strip()
    return Fraction(cell) if _DECIMAL.fullmatch(cell) else None


def leading_number(raw: str) -> Fraction | None:
    """The amount a free-text price cell begins with (`700 per share (FV 10 + premium 690)`)."""
    found = _LEADING_NUMBER.match(raw)
    return Fraction(found.group(1).replace(",", "")) if found else None


def iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def confidence(raw: str) -> int:
    """high 2, medium 1, anything else (low, blank) 0."""
    return CONFIDENCE.get(raw.strip().lower(), 0)


def ratio_of(a: str, b: str) -> tuple[Fraction, Fraction] | None:
    first, second = number(a), number(b)
    return (first, second) if first and second else None


_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATES = re.compile(
    r"(?P<iso>(?P<y1>\d{4})-(?P<m1>\d{2})-(?P<d1>\d{2}))"
    rf"|(?P<dmy>\b(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?[\s-]+(?P<m2>{_MON})[\s,-]+(?P<y2>\d{{4}}))"
    rf"|(?P<mdy>\b(?P<m3>{_MON})\s+(?P<d3>\d{{1,2}}),?\s+(?P<y3>\d{{4}}))",
    re.IGNORECASE,
)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_DATE_KIND = re.compile(r"\b(record|ex)\b(?:[\s-]*date)?", re.IGNORECASE)
_CLAUSE_END = re.compile(r"[.;|]\s")


def _date(found: re.Match[str]) -> date | None:
    try:
        if found.group("iso"):
            return date(int(found["y1"]), int(found["m1"]), int(found["d1"]))
        if found.group("dmy"):
            month = _MONTHS.index(found["m2"][:3].lower()) + 1
            return date(int(found["y2"]), month, int(found["d2"]))
        month = _MONTHS.index(found["m3"][:3].lower()) + 1
        return date(int(found["y3"]), month, int(found["d3"]))
    except ValueError:
        return None


def stated_dates(text: str, *, default: str = "") -> list[tuple[str, date]]:
    """The record and ex dates a text states: each date labelled by the nearest `record` or
    `ex` before it in the same clause (within 40 characters), else `default` (a cell that only
    holds such dates); dates with no label are not returned."""
    out = []
    for found in _DATES.finditer(text):
        day = _date(found)
        if day is None:
            continue
        before = text[max(0, found.start() - 40) : found.start()]
        cut = list(_CLAUSE_END.finditer(before))
        before = before[cut[-1].end() :] if cut else before
        labels = _DATE_KIND.findall(before)
        kind = labels[-1].lower() if labels else default
        if kind:
            out.append((kind, day))
    return out


def date_check(ex_date: date, texts: Iterable[tuple[str, str]]) -> str:
    """`confirmed` when a source states a record date in [ex, ex + RECORD_DAYS] or the ex-date
    itself and none states another within DISPUTE_DAYS; `disputed` when one does; "" when no
    source states a date near it. `texts` are (text, default label) pairs."""
    agree = disagree = False
    for text, default in texts:
        for kind, day in stated_dates(text, default=default):
            if abs((day - ex_date).days) > DISPUTE_DAYS:
                continue  # another event's date
            ok = day == ex_date if kind == "ex" else 0 <= (day - ex_date).days <= RECORD_DAYS
            agree, disagree = agree or ok, disagree or not ok
    return "disputed" if disagree else "confirmed" if agree else ""


def coa_factor(legs: Sequence[ResultingLeg]) -> Fraction | str | None:
    """The parent's cost-of-acquisition share / 100 over the share-distributing legs: None when
    no leg gives it; a reason when the legs disagree on it, it is outside (0, 100), or, every leg
    giving its own %, the parent's and theirs do not add up to 100."""
    parent = {leg.coa_parent for leg in legs if leg.coa_parent is not None}
    if not parent:
        return None
    if len(parent) > 1:
        return "coa_disagrees"
    pct = parent.pop()
    if not 0 < pct < 100:
        return "coa_out_of_range"
    shares = [leg.coa_resulting for leg in legs]
    if all(s is not None for s in shares) and abs(pct + sum(shares) - 100) > COA_SUM_TOL:  # type: ignore[arg-type]
        return "coa_does_not_sum"
    return pct / 100


def ratio_factor(
    priced: Sequence[tuple[tuple[Fraction, Fraction], Fraction]], parent_close: Fraction
) -> Fraction | None:
    """1 - sum(r_i * C_i) / P over the resulting companies (r_i = a/b shares per parent share,
    C_i its price): the share of the parent's value that stays with it. None outside (0, 1)."""
    if not priced or parent_close <= 0 or any(close <= 0 for _, close in priced):
        return None
    factor = 1 - sum(a / b * close for (a, b), close in priced) / parent_close
    return factor if 0 < factor < 1 else None


def agrees(gap: Fraction, check: Fraction) -> bool:
    """Does a cross-check explain the market gap? CA1's gate with the check as the expected
    factor: within `GATE_TOL` (20%) of it, and at least twice as close to it as to 1 in log
    terms, so a plain session move does not count as agreement."""
    return gate(check, Fraction(1), gap).confirmed


def market_factor(
    gap: Fraction, checks: Mapping[str, Fraction], when: str, settled: Fraction | None
) -> tuple[Fraction | None, str]:
    """A demerger's stored factor is the market gap (ex-date open / previous close, which NSE's
    special pre-open discovers), never a cross-check. (gap, which checks agree) when a cross-check
    explains it. Otherwise (no cross-check, or every one disagrees: a CoA is a book-value tax
    split that may well differ) the gap is still the factor when a source confirms the date, the
    drop is beyond `DEMERGER_MIN_DROP` and the price holds it: `settled` (the close
    `SETTLE_SESSIONS` sessions on, over the previous close) agrees with the gap as a cross-check
    would, so a pre-open discovery the market reverses (TATACOMM 2019-09-17: open 0.62, upper
    circuits to 0.83 in five sessions) is not stored; else (None, why it goes to review)."""
    if not 0 < gap.numerator < gap.denominator:  # 0 < gap < 1, exact
        return None, "no_drop"
    agreeing = [k for k, v in checks.items() if agrees(gap, v)]
    if agreeing:
        return gap, "agrees with " + "+".join(agreeing)
    unchecked = "gap_disagrees" if checks else "no_cross_check"
    if when != "confirmed":
        return None, f"{unchecked}_date_unconfirmed"
    if gap > 1 - DEMERGER_MIN_DROP:
        return None, f"{unchecked}_gap_not_material"
    if settled is None or not agrees(settled, gap):
        return None, f"{unchecked}_gap_not_held"
    if checks:
        return gap, "disagrees with " + "+".join(checks) + ", material drop on a confirmed date"
    return gap, "no cross-check, material drop on a confirmed date"


_NAME_SKIP = {"THE", "LTD", "LIMITED", "PVT", "PRIVATE", "CO", "COMPANY", "INDIA", "NEW", "LTDNEW"}


def name_words(name: str) -> tuple[str, ...]:
    """A company name's distinctive words, before any parenthesis: `Jio Financial Services Ltd
    (formerly ...)` is (JIO, FINANCIAL, SERVICES)."""
    words = re.findall(r"[A-Z0-9&]+", name.split("(")[0].upper())
    return tuple(w for w in words if w not in _NAME_SKIP)


def same_company(a: str, b: str) -> bool:
    """Two names of one company: their first two distinctive words agree."""
    x, y = name_words(a)[:2], name_words(b)[:2]
    return bool(x) and x == y


# --- the research rows --------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    file: str
    line: int
    confidence: int
    url: str
    quote: str
    day: date | None = None  # the `source_date` cell: when the source was published


@dataclass(frozen=True)
class ResultingLeg:
    """One resulting company of a demerger, as a research row gives it. `excluded` is why the
    event distributes no shares through this leg (not a demerger, preference shares only), ""
    when it does; `amalgamation` legs are never price factors for the parent."""

    name: str
    symbol: str
    isin: str
    ratio: tuple[Fraction, Fraction] | None
    coa_parent: Fraction | None
    coa_resulting: Fraction | None
    source: Source
    amalgamation: bool = False
    excluded: str = ""
    dates: tuple[tuple[str, str], ...] = ()  # (text, default label) for `date_check`

    @property
    def distributing(self) -> bool:
        return not self.amalgamation and not self.excluded


@dataclass(frozen=True)
class RightsResearch:
    """A rights row's terms as the research gives them."""

    terms: RightsTerms | None
    premium: Fraction | None  # the issue price over the face value at the issue
    partly_paid: bool
    alongside: bool  # warrants, convertibles or debentures in the same issue
    terms_wrong: bool  # the research says our terms are wrong
    isin: str
    source: Source


Key = tuple[str, date]  # symbol, ex-date

_NOT_DISTRIBUTING = re.compile(r"no shares to holders|wholly[- ]owned|100% subsidiary", re.I)
_ALONGSIDE = re.compile(
    r"\bwarrants?\b|\bccps\b|\bccds?\b|\bncds?\b|convertible|debenture|\b(?:two|second|2nd)\s+tranches?\b",
    re.I,
)
_PREMIUM = re.compile(rf"premium\s*(?:of\s*)?(?:rs\.?\s*)?{_AMOUNT}", re.I)
_NO_PREMIUM = re.compile(r"no premium|premium\s*(?:rs\.?\s*)?0\b|at par", re.I)


def premium_in(text: str) -> Fraction | None:
    """The premium a free-text price cell states (`FV 10 + premium 690`; `no premium` is 0)."""
    if _NO_PREMIUM.search(text):
        return Fraction(0)
    found = _PREMIUM.findall(text)
    return Fraction(found[0].replace(",", "")) if len(found) == 1 else None


def _cell(value: object) -> str:
    return ("" if value is None else str(value)).strip()[:TEXT_MAX]


def _symbol(raw: str) -> str:
    cell = raw.strip()
    return cell if _SYMBOL.fullmatch(cell) else ""


def _isin(raw: str) -> str:
    cell = raw.strip()
    return cell if equity_share_isin(cell) else ""


def _source(file: str, line: int, row: Mapping[str, str]) -> Source:
    return Source(
        file,
        line,
        confidence(row["confidence"]),
        row["source_url"],
        row["quote"],
        iso_date(row.get(SOURCE_DATE, "")),
    )


def _excluded_a(row: Mapping[str, str]) -> str:
    kind = row["is_demerger"].lower()
    if kind.startswith("no"):
        return "not_a_demerger"
    if kind.startswith("unknown"):
        return "event_unknown"
    if _NOT_DISTRIBUTING.search(row["is_demerger"]) or _NOT_DISTRIBUTING.search(
        row["resulting_name"]
    ):
        return "no_shares_to_holders"
    if row["non_equity_consideration"].lower().startswith("yes"):
        return "preference_shares_only"
    return ""


def leg_from_a(line: int, row: Mapping[str, str]) -> ResultingLeg:
    return ResultingLeg(
        row["resulting_name"],
        _symbol(row["resulting_nse_symbol"]),
        _isin(row["resulting_isin"]),
        ratio_of(row["ratio_a"], row["ratio_b"]),
        number(row["coa_parent_pct"]),
        number(row["coa_resulting_pct"]),
        _source(FILE_A, line, row),
        excluded=_excluded_a(row),
        dates=((row["notes"], ""), (row["quote"], "")),
    )


def leg_from_b(line: int, row: Mapping[str, str]) -> ResultingLeg:
    excluded = (
        "preference_shares_only"
        if row["non_equity_consideration"].lower().startswith("yes")
        else ""
    )
    return ResultingLeg(
        row["resulting_name"],
        _symbol(row["resulting_nse_symbol"]),
        _isin(row["resulting_isin"]),
        ratio_of(row["ratio_a"], row["ratio_b"]),
        number(row["coa_parent_pct"]),
        number(row["coa_resulting_pct"]),
        _source(FILE_B, line, row),
        amalgamation=not row["leg_type"].lower().startswith("demerger"),
        excluded=excluded,
        dates=((row["notes"], ""), (row["quote"], ""), (row["our_data_wrong"], "")),
    )


def leg_from_d(line: int, row: Mapping[str, str]) -> ResultingLeg:
    return ResultingLeg(
        row["resulting_name"],
        _symbol(row["resulting_nse_symbol"]),
        "",
        ratio_of(row["ratio_a"], row["ratio_b"]),
        number(row["coa_parent_pct"]),
        number(row["coa_resulting_pct"]),
        _source(FILE_D, line, row),
        dates=(
            (row["corrected_ex_or_record_date"], "record"),
            (row["notes"], ""),
            (row["quote"], ""),
        ),
    )


def merge_events(
    earlier: Mapping[Key, Sequence[ResultingLeg]], later: Mapping[Key, Sequence[ResultingLeg]]
) -> dict[Key, tuple[ResultingLeg, ...]]:
    """Each event's legs: `later` (file d) replaces an event's `earlier` (a, b) legs wholesale,
    where it disagrees too (d wins); each later leg keeps the leg type, the exclusion and the
    ISIN of the earlier leg of the same company. Duplicate later rows of one company and ratio
    keep the most confident."""
    out = {key: tuple(legs) for key, legs in earlier.items()}
    for key, legs in later.items():
        merged: dict[tuple, ResultingLeg] = {}
        for leg in legs:
            match = next((e for e in earlier.get(key, ()) if same_company(e.name, leg.name)), None)
            if match is not None:
                leg = replace(
                    leg,
                    amalgamation=match.amalgamation,
                    excluded=match.excluded,
                    isin=leg.isin or match.isin,
                    symbol=leg.symbol or match.symbol,
                )
            ident = (name_words(leg.name), leg.ratio)
            kept = merged.get(ident)
            if kept is None or leg.source.confidence > kept.source.confidence:
                merged[ident] = leg
        out[key] = tuple(merged.values())
    return out


def rights_from_c(line: int, row: Mapping[str, str]) -> RightsResearch:
    ratio = ratio_of(row["ratio_m"], row["ratio_n"])
    price = number(row["issue_price"])
    face = number(row["face_value"])
    wrong = row["our_data_wrong"].lower()
    return RightsResearch(
        RightsTerms(ratio[0], ratio[1], price) if ratio and price else None,
        price - face if price is not None and face is not None and price >= face else None,
        row["partly_paid"].lower().startswith("yes"),
        bool(_ALONGSIDE.search(row["other_instruments"])),
        wrong.startswith("yes") and "isin" not in wrong,  # a wrong ISIN is fixed by matching
        _isin(row["isin"]),
        _source(FILE_C, line, row),
    )


def rights_from_b(line: int, row: Mapping[str, str]) -> RightsResearch:
    ratio = ratio_of(row["ratio_a"], row["ratio_b"])
    price = leading_number(row["issue_price"])
    wrong = row["our_data_wrong"].lower()
    terms_text = " ".join(row[c] for c in ("issue_price", "partly_paid", "leg_type", "notes"))
    return RightsResearch(
        RightsTerms(ratio[0], ratio[1], price) if ratio and price else None,
        premium_in(row["issue_price"]),
        "yes" in row["partly_paid"].lower()[:12],
        bool(_ALONGSIDE.search(terms_text)),
        wrong.startswith("yes") and "isin" not in wrong,
        _isin(row["isin"]),
        _source(FILE_B, line, row),
    )


@dataclass
class Research:
    """The research files, read: demerger events by key and file label, rights by key, and the
    rows that do not parse (file, line, reason)."""

    events: dict[Key, tuple[ResultingLeg, ...]]
    rights: dict[Key, RightsResearch]
    parent_isin: dict[Key, str]
    unparsed: list[tuple[str, int, str, str]]  # file, line, symbol, reason


def read_research(rows: Mapping[str, Sequence[Mapping[str, str]]]) -> Research:
    """The research rows (by file label, each row's cells as text) as events and rights."""
    earlier: dict[Key, list[ResultingLeg]] = defaultdict(list)
    later: dict[Key, list[ResultingLeg]] = defaultdict(list)
    rights: dict[Key, RightsResearch] = {}
    parent_isin: dict[Key, str] = {}
    unparsed: list[tuple[str, int, str, str]] = []

    def add_rights(file: str, line: int, key: Key, found: RightsResearch) -> None:
        if key in rights:
            unparsed.append((file, line, key[0], "duplicate_rights_row"))
        else:
            rights[key] = found

    for file, table in rows.items():
        for line, row in enumerate(table, start=2):  # line 1 is the header
            symbol = _symbol(row.get("parent_symbol") or row.get("symbol", ""))
            ex = iso_date(row["ex_date"])
            if not symbol or ex is None:
                unparsed.append((file, line, symbol or row.get("symbol", ""), "bad_key"))
                continue
            key = (symbol, ex)
            if file == FILE_A:
                earlier[key].append(leg_from_a(line, row))
                if _isin(row["parent_isin"]):
                    parent_isin[key] = row["parent_isin"].strip()
            elif file == FILE_B and row["table"] == T_RIGHTS:
                add_rights(file, line, key, rights_from_b(line, row))
            elif file == FILE_B and row["table"] == T_DEMERGERS:
                earlier[key].append(leg_from_b(line, row))
                if _isin(row["isin"]):
                    parent_isin.setdefault(key, row["isin"].strip())
            elif file == FILE_C:
                add_rights(file, line, key, rights_from_c(line, row))
            elif file == FILE_D and row["source_file"] in (FILE_A, FILE_B):
                later[key].append(leg_from_d(line, row))
            else:
                unparsed.append((file, line, symbol, "unknown_table"))
    return Research(merge_events(earlier, later), rights, parent_isin, unparsed)


def read_csv(path: Path, file: str) -> list[dict[str, str]]:
    """A research CSV's rows as text; refuses (`CaFileError`) a file too large, not UTF-8, or
    missing a required column."""
    if path.stat().st_size > MAX_BYTES:
        raise CaFileError("research_file_too_large")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaFileError("research_file_unreadable") from exc
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not set(REQUIRED[file]) <= set(reader.fieldnames or ()):
            raise CaFileError("research_file_columns")
        out = []
        for row in reader:
            if len(out) >= MAX_ROWS:
                raise CaFileError("research_file_too_large")
            out.append({k: _cell(row.get(k)) for k in (*REQUIRED[file], SOURCE_DATE)})
    except csv.Error as exc:
        raise CaFileError("research_file_unreadable") from exc
    return out


# --- matching -----------------------------------------------------------------------------------


def match_rows(
    file_rows: Sequence[Mapping[str, str]], keys: Iterable[Key]
) -> tuple[dict[Key, Mapping[str, str]], list[Key]]:
    """Each research key's file row (same symbol and ex-date; NSE keeps one row per event in
    these tables), and the keys with none or with several."""
    index: dict[Key, list[Mapping[str, str]]] = defaultdict(list)
    for row in file_rows:
        ex = nse_date(row["exDate"])
        if ex is not None:
            index[(row["symbol"], ex)].append(row)
    matched, unmatched = {}, []
    for key in keys:
        found = index.get(key, [])
        if len(found) == 1:
            matched[key] = found[0]
        else:
            unmatched.append(key)
    return matched, unmatched


def isin_for(file_isin: str, research_isin: str) -> str | None:
    """The ISIN to match instruments by: the file's, or the research's when the file holds a
    placeholder; None when both are real and differ."""
    if not equity_share_isin(file_isin):
        return research_isin
    if research_isin and research_isin != file_isin:
        return None
    return file_isin


# --- deciding -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Priced:
    """A resulting company priced through a known instrument."""

    instrument: Instrument
    day: date
    close: Fraction
    listed: bool  # a fresh listing (first close) rather than a company already trading


def resolve_resulting(
    ctx: Context, leg: ResultingLeg, ex_date: date, parent_ids: set[str]
) -> Instrument | None:
    """The instrument of a resulting company: by its ISIN; else by its NSE symbol (now or in
    the symbol history) when that instrument's name is the company's or it first listed in the
    year after the ex-date; else the one fresh equity listing of that year with the company's
    name. Never the parent's own chain."""
    ref = ctx.reference

    def fresh(iid: str) -> bool:
        first = ctx.first_closes.get(iid)
        return first is not None and ex_date <= first[0] <= ex_date + timedelta(days=CHILD_DAYS)

    if leg.isin and leg.isin in ref.by_isin:
        iid = ref.by_isin[leg.isin]
        return None if iid in parent_ids else ref.instruments.get(iid)
    if leg.symbol:
        ids = set(ref.by_symbol.get(leg.symbol, ())) | {
            iid for iid, _, _ in ref.history.get(leg.symbol, ())
        }
        found = [
            ref.instruments[i]
            for i in sorted(ids - parent_ids)
            if i in ref.instruments
            and equity_share_isin(ref.instruments[i].isin)
            and (same_company(ref.instruments[i].name, leg.name) or fresh(i))
        ]
        if len(found) == 1:
            return found[0]
    named = [
        i
        for iid, i in ref.instruments.items()
        if iid not in parent_ids
        and iid not in ref.successors
        and equity_share_isin(i.isin)
        and fresh(iid)
        and same_company(i.name, leg.name)
    ]
    return named[0] if len(named) == 1 else None


def price_resulting(
    ctx: Context, instrument: Instrument, ex_date: date, bars: Sequence[Bar]
) -> Priced | None:
    """The resulting company's first close when it listed in the year after the ex-date, or its
    first close on or after the ex-date when it already traded (`bars`: its window)."""
    first = ctx.first_closes.get(instrument.id)
    if first is None:
        return None
    if ex_date <= first[0] <= ex_date + timedelta(days=CHILD_DAYS):
        return Priced(instrument, first[0], Fraction(first[1]), True)
    if first[0] < ex_date:
        on = [b for b in bars if b.instrument_id == instrument.id and b.day >= ex_date]
        if on:
            bar = min(on, key=lambda b: b.day)
            return Priced(instrument, bar.day, Fraction(bar.close), False)
    return None


def _event_text(
    legs: Sequence[ResultingLeg], checks: Mapping[str, Fraction], gap: Fraction, note: str
) -> str:
    """The stored text: the event, the method and each cross-check, `disagrees` when the gap
    does not agree with it (`note`: a CoA the research gives that contradicts itself)."""
    parts = [
        f"{leg.ratio[0]}:{leg.ratio[1]} {leg.name[:60]}" if leg.ratio else leg.name[:60]
        for leg in legs
    ]
    marks = [f"{k} {fmt4(v)}{'' if agrees(gap, v) else ' disagrees'}" for k, v in checks.items()]
    tag = "; ".join([MARKET_GAP, *marks, *([note] if note else [])])
    return f"Demerger {'; '.join(parts)} [research {tag}]"[:TEXT_MAX]


def _sources(legs: Sequence[ResultingLeg]) -> list[dict[str, Any]]:
    return [
        {
            "file": leg.source.file,
            "line": leg.source.line,
            "confidence": next(k for k, v in CONFIDENCE.items() if v == leg.source.confidence),
            "source_url": leg.source.url[:500],
            "quote": leg.source.quote[:300],
        }
        for leg in legs
    ]


def cross_checks(
    distributing: Sequence[ResultingLeg],
    priced: Sequence[Priced | None],
    prev_close: Fraction,
    *,
    ratio_basis_known: bool,
) -> tuple[dict[str, Fraction], str]:
    """The cross-checks a demerger's market gap is compared with: `coa` (the parent's
    cost-of-acquisition %, a book-value tax allocation) and `ratio` (1 - sum(r_i*C_i)/P with
    each resulting company's price), each where the research and the bars give it; and a note when
    the CoA figures contradict themselves (then there is no `coa` check)."""
    checks: dict[str, Fraction] = {}
    coa = coa_factor(distributing)
    note = coa if isinstance(coa, str) else ""
    if isinstance(coa, Fraction):
        checks["coa"] = coa
    pairs = [
        (leg.ratio, c.close) for leg, c in zip(distributing, priced, strict=True) if leg.ratio and c
    ]
    if ratio_basis_known and len(pairs) == len(distributing):
        found = ratio_factor(pairs, prev_close)
        if found is not None:
            checks["ratio"] = found
    return checks, note


def decide_demerger(
    p: Proposal,
    legs: Sequence[ResultingLeg],
    ctx: Context,
    bars: Sequence[Bar],
    resulting: Mapping[int, Priced | None],
) -> Outcome:
    """The parent's factor for one researched demerger event: the market gap
    (`market_factor`), with the CoA and priced-ratio cross-checks beside it (`resulting`: each
    leg's price by its index, None when no known instrument prices it)."""
    if p.status or p.ex_date is None:
        return Outcome(p, p.status or REVIEW, p.reason or "bad_ex_date")
    distributing = [leg for leg in legs if leg.distributing]
    if not distributing:
        excluded = next((leg.excluded for leg in legs if leg.excluded), "amalgamation_only")
        status = REVIEW if excluded == "event_unknown" else SKIPPED
        return Outcome(p, status, excluded)
    if min(leg.source.confidence for leg in distributing) < CONFIDENCE["medium"]:
        return Outcome(p, REVIEW, "low_confidence")
    when = date_check(p.ex_date, (t for leg in legs for t in leg.dates))
    if when == "disputed":
        return Outcome(p, REVIEW, "date_disputed")
    ids = ctx.reference.ids(p.symbol, p.isin, p.ex_date)
    if not ids:
        return Outcome(p, SKIPPED if p.sme else REVIEW, "no_instrument")
    pair = around(bars, p.ex_date, p.isin)
    if isinstance(pair, str):
        return Outcome(p, REVIEW, pair)
    before, on = pair
    prev_close, ex_open = Fraction(before.close), Fraction(on.open)
    detail = f"prev {before.day} close {before.close} -> {on.day} open {on.open}"
    if prev_close <= 0 or ex_open <= 0:
        return Outcome(p, REVIEW, "no_bars", detail)
    evidence: dict[str, Any] = {
        "previous_day": before.day.isoformat(),
        "previous_close": str(before.close),
        "ex_day": on.day.isoformat(),
        "ex_open": str(on.open),
        "research": _sources(legs),
    }
    near = near_actions(ctx, ids | {before.instrument_id, on.instrument_id}, p.ex_date)
    same_day = math.prod(
        (
            Fraction(s.factor)
            for s in near
            if before.day < s.ex_date <= on.day and s.kind != KIND_DEMERGER
        ),
        start=Fraction(1),
    )
    priced = [resulting.get(i) for i, leg in enumerate(legs) if leg.distributing]
    detail += "".join(
        f", resulting {c.instrument.symbol} {'first ' if c.listed else ''}close {c.close:.2f}"
        f" on {c.day}"
        for c in priced
        if c
    )
    # with another action between the sessions, the ratio's basis (before or after it) is unknown
    checks, note = cross_checks(distributing, priced, prev_close, ratio_basis_known=same_day == 1)
    if note:
        detail += f", {note}"
    gap = ex_open / prev_close / same_day
    detail += f", market gap {fmt4(gap)}" + "".join(f", {k} {fmt4(v)}" for k, v in checks.items())
    evidence["cross_checks"] = {k: fmt4(v) for k, v in checks.items()}
    after = sorted(
        (b for b in bars if b.instrument_id == on.instrument_id and b.day >= on.day),
        key=lambda b: b.day,
    )
    held = None
    if len(after) >= SETTLE_SESSIONS and after[SETTLE_SESSIONS - 1].close > 0:
        last = after[SETTLE_SESSIONS - 1]
        # the raw close is unadjusted: an action stored after the ex session moves it too
        later = math.prod(
            (
                Fraction(s.factor)
                for s in ctx.stored.get(on.instrument_id, ())
                if on.day < s.ex_date <= last.day
            ),
            start=Fraction(1),
        )
        held = Fraction(last.close) / prev_close / same_day / later
    if held is not None:
        detail += f", close {SETTLE_SESSIONS} sessions on {fmt4(held)}"
    factor, reason = market_factor(gap, checks, when, held)
    if factor is None:
        return Outcome(p, REVIEW, reason, detail)
    # the fifth-session close dates the gap only when it held it; else a cross-check's date does
    settle_days = [b.day for b in after] if held is not None and agrees(held, gap) else []
    known = demerger_known(p.ex_date, gap, checks, distributing, priced, settle_days)
    evidence["derived"] = f"{MARKET_GAP} {reason}"
    evidence["gap"] = fmt4(gap)
    p = replace(p, text=_event_text(legs, checks, gap, note))
    # the market factor passes the gate by construction; the cross-checks were the test
    verdict = gate(factor * same_day, prev_close, ex_open)
    near_kind = [s for s in near if s.kind == KIND_DEMERGER]
    outcome = settle(
        p,
        before,
        [Leg(TYPE_OTHER, factor, True)],
        near_kind,
        verdict,
        detail,
        evidence,
        source=SOURCE_RESEARCH,
        known=known,
    )
    if outcome.status == STORED:
        outcome.reason = f"{MARKET_GAP} ({reason})"
    return outcome


def demerger_known(
    ex_date: date,
    gap: Fraction,
    checks: Mapping[str, Fraction],
    distributing: Sequence[ResultingLeg],
    priced: Sequence[Priced | None],
    days: Sequence[date],
) -> Known:
    """When a demerger's market gap was knowable (CA5): the first route that accepts it, an
    agreeing `ratio` check (at the latest of its priced closes and the legs' source dates), an
    agreeing `coa` check (at the legs' source dates), or the gap held to the close
    `SETTLE_SESSIONS` on (`days`: the ex instrument's sessions, empty when the close did not
    hold the gap)."""
    sources = [leg.source.day for leg in distributing]
    routes = [settle_known(days, ex_date)]
    if "ratio" in checks and agrees(gap, checks["ratio"]):
        closes = [c.day for c in priced if c is not None]
        routes.append(from_sources(ex_date, [*sources, *closes], RESULTING_FIRST_CLOSE))
    if "coa" in checks and agrees(gap, checks["coa"]):
        routes.append(from_sources(ex_date, sources, RESEARCH_SOURCE_DATE))
    return earliest(routes)


def rights_override(
    p: Proposal, row: Mapping[str, str], research: RightsResearch
) -> tuple[Proposal, str]:
    """The proposal with the research's terms, or why it stays in review; the second value is
    the note the stored text carries (corrected terms, partly paid)."""
    if research.source.confidence < CONFIDENCE["medium"]:
        return p.ends(REVIEW, "low_confidence"), ""
    if research.alongside or _ALONGSIDE.search(p.text):
        return p.ends(REVIEW, "warrants_or_convertibles_alongside"), ""
    if research.terms is None:
        return p.ends(REVIEW, "research_no_terms"), ""
    plain = re.sub(r"partly[\s-]*paid(?:[\s-]*up)?", " ", p.text, flags=re.IGNORECASE)
    ours = rights_terms(plain, row["faceVal"])
    ours_ratio = parse_ratio(row["entitlement_ratio_parsed"])
    t = research.terms
    notes = []
    if isinstance(ours, RightsTerms):
        # our file's face value is today's: after a split it is not the one at the issue
        premium = ours.price - Fraction(exact(row["faceVal"], positive=True) or 0)
        same = (ours.new, ours.held) == (t.new, t.held) and (
            ours.price == t.price or premium == research.premium
        )
        if same and ours.price != t.price:
            notes.append("face value at the issue per research")
    else:
        same = ours_ratio == (t.new, t.held)
    corrects_face_value = isinstance(ours, RightsTerms) and ours.price != t.price
    if same and research.terms_wrong and not corrects_face_value:
        return p.ends(REVIEW, "research_flags_row"), ""  # wrong, but not in its terms
    if not same:
        if not research.terms_wrong:
            return p.ends(REVIEW, "research_terms_differ"), ""
        notes.append("terms corrected by research")
    partly = research.partly_paid or "partly" in p.text.lower()
    if partly:
        notes.append("partly paid: full issue price")
    note = f"{t.new}:{t.held} @ {fmt4(t.price)}" + "".join(f"; {n}" for n in notes)
    return replace(p, rights=t, status="", reason=""), note


def _rights_proposal(
    label: str, row: Mapping[str, str], research: RightsResearch
) -> tuple[Proposal, str]:
    p = Proposal(
        label,
        str(research.source.line),
        row["symbol"],
        row["isin"],
        nse_date(row["exDate"]),
        row["subject_raw"],
        hint=f"{row['market']} FV {row['faceVal']}",
        sme="sme" in row["market"],
    )
    isin = isin_for(row["isin"], research.isin)
    if isin is None:
        return p.ends(REVIEW, "isin_disagrees"), ""
    p = replace(p, isin=isin)
    if p.ex_date is None:
        return p.ends(REVIEW, "bad_ex_date"), ""
    return rights_override(p, row, research)


def decide_rights(
    p: Proposal, note: str, research: RightsResearch, ctx: Context, bars: Sequence[Bar]
) -> Outcome:
    """CA1's decision with the research's terms; the gate's `ambiguous` still stores, tagged
    `price_unconfirmed`."""
    full = f"{p.text} [research {research.source.file}: {note}]"
    p = replace(p, text=full[:TEXT_MAX])
    known = None
    if p.ex_date is not None and research_supplied(full):  # before a cut drops the tag's markers
        known = from_sources(p.ex_date, [research.source.day], RESEARCH_SOURCE_DATE)
    outcome = decide(
        p, ctx, bars, source=SOURCE_RESEARCH, accept=frozenset({"ambiguous"}), known=known
    )
    outcome.evidence["research"] = _sources_rights(research)
    if outcome.status == STORED and outcome.reason == "ambiguous":
        tag = f"{p.text[:-1]}; {PRICE_UNCONFIRMED}]"
        outcome.records = [replace(r, raw_text=tag) for r in outcome.records]
        outcome.reason = PRICE_UNCONFIRMED
    return outcome


_SUPPLIED = (
    "terms corrected by research",
    "face value at the issue per research",
)
_PARTLY_PAID = re.compile(r"partly[\s-]*paid(?:[\s-]*up)?", re.IGNORECASE)


def research_supplied(text: str) -> bool:
    """Does a stored rights text (`decide_rights`: NSE's purpose, then `[research ...]`) rest on
    terms only research gives: terms or a face value research corrected, or a purpose whose own
    terms do not price the issue (no issue price, non-simple terms)? Then the factor is known at
    the research's source date, not at the ex-date open (CA5)."""
    head, _, tag = text.partition(" [research ")
    if any(marker in tag for marker in _SUPPLIED) or (tag and not tag.endswith("]")):
        return True  # a stored text cut at `TEXT_MAX` may have lost its markers: never ex_open
    return not isinstance(rights_terms(_PARTLY_PAID.sub(" ", head), "1"), RightsTerms)


def _sources_rights(research: RightsResearch) -> list[dict[str, Any]]:
    s = research.source
    level = next(k for k, v in CONFIDENCE.items() if v == s.confidence)
    return [
        {
            "file": s.file,
            "line": s.line,
            "confidence": level,
            "source_url": s.url[:500],
            "quote": s.quote[:300],
        }
    ]


def _demerger_proposal(label: str, key: Key, row: Mapping[str, str], parent_isin: str) -> Proposal:
    return Proposal(
        label,
        row["rowid"],
        key[0],
        parent_isin,
        key[1],
        f"Demerger {row['ratio']}",
        hint=row["ratio_note"][:300],
        sme=row["market"] == "sme",
    )


def _label(legs: Sequence[ResultingLeg]) -> str:
    files = {leg.source.file for leg in legs}
    return f"{FILE_D if FILE_D in files else min(files)}:demergers"


def evaluate(
    session: Session,
    store: DataStore,
    tables: Mapping[str, Sequence[Mapping[str, str]]],
    research: Research,
) -> list[Outcome]:
    """Every research row's outcome against CA1's file rows (`tables`); nothing is written."""
    reference = load_reference(session)
    ctx = Context(reference, stored_price_actions(session), read_first_closes(store))
    outcomes: list[Outcome] = [
        Outcome(
            Proposal("unmatched", str(line), symbol, "", None, f"{file} line {line}"),
            REVIEW,
            reason,
        )
        for file, line, symbol, reason in research.unparsed
    ]
    dem_rows, dem_missing = match_rows(tables.get(T_DEMERGERS, ()), research.events)
    rig_rows, rig_missing = match_rows(tables.get(T_RIGHTS, ()), research.rights)
    for key in [*dem_missing, *rig_missing]:
        text = f"no single NSE file row for {key[0]} {key[1]}"
        p = Proposal("unmatched", "", key[0], "", key[1], text)
        outcomes.append(Outcome(p, REVIEW, "no_file_row"))

    demergers = []
    for key, row in dem_rows.items():
        legs = research.events[key]
        p = _demerger_proposal(_label(legs), key, row, research.parent_isin.get(key, ""))
        demergers.append((p, legs))
    rights = []
    for key, row in rig_rows.items():
        found = research.rights[key]
        p, note = _rights_proposal(f"{found.source.file}:rights", row, found)
        rights.append((p, note, found))

    wanted: dict[int, tuple[date, set[str]]] = {}
    for k, (p, _) in enumerate(demergers):
        if not p.status and p.ex_date is not None:
            wanted[k] = (p.ex_date, reference.ids(p.symbol, p.isin, p.ex_date))
    offset = len(demergers)
    for k, (p, _, _) in enumerate(rights):
        if not p.status and p.ex_date is not None:
            wanted[offset + k] = (p.ex_date, reference.ids(p.symbol, p.isin, p.ex_date))
    # the resulting companies that already traded are priced at the ex-date
    resolved: dict[tuple[int, int], Instrument] = {}
    child_wanted: dict[int, tuple[date, set[str]]] = {}
    for k, (p, legs) in enumerate(demergers):
        if k not in wanted or p.ex_date is None:
            continue
        for i, leg in enumerate(legs):
            if leg.distributing:
                instrument = resolve_resulting(ctx, leg, p.ex_date, wanted[k][1])
                if instrument is not None:
                    resolved[(k, i)] = instrument
                    child_wanted[len(child_wanted) + 10 * (offset + len(rights))] = (
                        p.ex_date,
                        {instrument.id},
                    )
    windows = read_windows(store, {**wanted, **child_wanted})
    child_bars = [b for k in child_wanted for b in windows.get(k, ())]

    for k, (p, legs) in enumerate(demergers):
        prices: dict[int, Priced | None] = {}
        for i in range(len(legs)):
            instrument = resolved.get((k, i))
            prices[i] = (
                None
                if instrument is None or p.ex_date is None
                else price_resulting(ctx, instrument, p.ex_date, child_bars)
            )
        outcomes.append(decide_demerger(p, legs, ctx, windows.get(k, ()), prices))
    for k, (p, note, found) in enumerate(rights):
        if p.status:
            o = Outcome(p, p.status, p.reason)
            o.evidence["research"] = _sources_rights(found)
            outcomes.append(o)
        else:
            outcomes.append(decide_rights(p, note, found, ctx, windows.get(offset + k, ())))
    dedupe(outcomes)
    held = owner_vacated(session, {r.instrument_id for o in outcomes for r in o.records})
    for o in outcomes:
        if any(r.key in held for r in o.records):
            o.status, o.reason, o.records = SKIPPED, "owner_vacated", []
    return outcomes


# --- command line -------------------------------------------------------------------------------


def input_path(name: str, suffix: str) -> Path:
    """A file named on the command line, under the working directory with `suffix`: anything
    else is refused before it is opened."""
    base = Path.cwd().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or target.suffix.lower() != suffix:
        raise CaFileError("ca_file_outside_cwd")
    if not target.is_file():
        raise CaFileError("ca_file_missing")
    return target


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_research --db FILE [--a A.csv] [--b B.csv] [--c C.csv]
    [--d D.csv] [--apply] [--all]

    Reads CA1's corporate-action file FILE and the research CSVs (all under the working
    directory, opened read-only) and the raw bars under `HAT_DATA_ROOT`, and prints what it
    would store (a dry run) or, with `--apply`, stores it in one transaction. The report goes to
    stdout; `--all` lists every row. After an apply, rebuild the lineage
    (`python -m app.hat.data.lineage build`) and the features (`features --full`)."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_research")
    parser.add_argument("--db", required=True)
    for file in REQUIRED:
        parser.add_argument(f"--{file}", help=f"research file {file} (.csv)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = input_path(args.db, ".db")
        tables = read_file(path)
        files = {f: input_path(getattr(args, f), ".csv") for f in REQUIRED if getattr(args, f)}
        research = read_research({f: read_csv(p, f) for f, p in files.items()})
    except CaFileError as exc:
        sys.stderr.write(f"refused: {exc.code}\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from ..time import utc_now
    from .ca_import import apply, report_lines

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    with HatSessionLocal() as session:
        outcomes = evaluate(session, store, tables, research)
        if args.apply:
            journal = {
                "source": SOURCE_RESEARCH,
                "file_sha256": file_digest(path),
                "research_sha256": {f: digest(p) for f, p in files.items()},
            }
            created = apply(session, outcomes, journal=journal, now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows\n")
        else:
            session.rollback()
    lines = report_lines(outcomes, applied=args.apply, everything=args.all, tables=TABLES)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

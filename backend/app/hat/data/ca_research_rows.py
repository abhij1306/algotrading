"""The research CSVs of the corporate-action research import (`ca_research`, CA2), read: untrusted
input outside the repo, read as text, cut to `TEXT_MAX`, every number parsed here; a row that
does not parse is listed (`Research.unparsed`), never stored. Each row carries a confidence
(high/medium/low), a source URL and a quote, and optionally `source_date` (when its source was
published, CA5).

- `a` and `b`: demergers CA1 could not price (per resulting company: ratio, cost-of-acquisition
  split, listing), plus `b`'s rights rows with no price or non-simple terms.
- `d`: later, better-sourced values for events of `a` and `b`. An event `d` names is taken from
  `d` alone (`merge_events`); each of its legs keeps the leg type and exclusion of the `a`/`b`
  leg of the same company.
- `c`: rights rows CA1 found `ambiguous` (the terms checked against sources).

The dates a source states (`stated_dates`) confirm or dispute an ex-date (`date_check`).
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path

from .ca_apply import RightsTerms
from .ca_gate import TEXT_MAX, CaFileError
from .ca_import import T_DEMERGERS, T_RIGHTS
from .isin import voting_equity

CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
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
ALONGSIDE = re.compile(
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
    return cell if voting_equity(cell) else ""


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
        bool(ALONGSIDE.search(row["other_instruments"])),
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
        bool(ALONGSIDE.search(terms_text)),
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

"""NSE's daily corporate-action file (CA4): the `bc` CSV of the PR archive, every security's
actions of the coming days by symbol and series, with an abbreviated purpose such as "INTDIV - RS
7.50 PER SH" that `bc_subject` writes in the words of NSE's corporate-actions feed. The nightly
dividend import (`ca_nightly`) and the demerger notices (`demerger_notice`) read it; `FeedRow`
is a row of either list."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

from .ca_gate import TEXT_MAX, CaFileError, nse_date

MAX_FILE_BYTES = 20_000_000
SOURCE_BC = "nse_pr_bc"  # NSE's daily corporate-action file, read by the nightly (CA4)
SME_SERIES = frozenset({"SM", "ST"})


@dataclass(frozen=True)
class FeedRow:
    symbol: str
    isin: str
    series: str
    ex_date: date | None
    subject: str


def cell(value: object) -> str:
    return ("" if value is None else str(value)).strip()[:TEXT_MAX]


# NSE's daily corporate-action file (the `bc` CSV of the PR archive) abbreviates its purposes:
# "INTDIV - RS 7.50 PER SH", "DIV-RS 8/SPLDIV-RS 2", "DIV/SPDV RS 5/RS 3", "FVSPLT FRM RS 10 TO
# RS 2". `bc_subject` writes them in the feed's words so `payouts` reads them as it reads the feed.
BC_COLUMNS = ("SERIES", "SYMBOL", "EX_DT", "PURPOSE")
# the series of an equity share's normal market and of the SME platform; the file lists the same
# action again under each window (BL, IQ, T0, TL) and every debt or ETF series
BC_SERIES = frozenset({"EQ", "BE", "BZ", *SME_SERIES})


_BC_WORDS = (
    (re.compile(r"\bint\s*di?v"), " interim dividend "),
    (re.compile(r"\bfi?n(?:a?l)?\s*di?v"), " final dividend "),
    (re.compile(r"\b(?:spl|sp)\s*di?v"), " special dividend "),
    (re.compile(r"\bdiv(?!idend)"), " dividend "),
    (re.compile(r"\bfv\s*splt"), " face value split "),
    (re.compile(r"\br(?:i)?ghts\b"), " rights "),
    (re.compile(r"\bcnsldatn"), " consolidation "),
)


# two kinds before two amounts: "DIV/SPDV RS 5/RS 3", "INTDVSPDVRS 4 & 6", "DIVSPDV - RS 5 & 2"
_BC_PAIR = re.compile(
    r"^(int\s*di?v|div)\s*/?\s*(?:spl|sp)\s*di?v\s*-?\s*(?:rs|re)\s*(\d+(?:\.\d+)?)\s*[/&]\s*"
    r"(?:(?:rs|re)\s*)?(\d+(?:\.\d+)?)$"
)


_BC_RUPEE = re.compile(r"\b(rs|re)\.?\s*(?=\d)")  # "RS5", "RS.8": the amount after a space


def bc_subject(purpose: str) -> str:
    """A `bc` purpose in the feed's words ("INTDIV - RS 7.50 PER SH" is "Interim Dividend - Rs
    7.50 Per Sh"), so `payouts` reads it; a purpose naming no dividend keeps its own words."""
    text = re.sub(r"\s+", " ", purpose.lower()).strip()
    pair = _BC_PAIR.match(text)
    if pair is not None:
        kind = "interim dividend" if pair.group(1).startswith("int") else "dividend"
        return f"{kind} - rs {pair.group(2)} / special dividend - rs {pair.group(3)}"
    text = _BC_RUPEE.sub(r"\1 ", text)
    for pattern, words in _BC_WORDS:
        text = pattern.sub(words, text)
    return re.sub(r"\s+", " ", text).strip()


def read_bc(text: str) -> list[FeedRow]:
    """The equity rows of a `bc` file (columns SERIES, SYMBOL, EX_DT, PURPOSE among others), the
    purpose in the feed's words (`bc_subject`); the file names no ISIN. A file too large or
    missing a column is refused (`CaFileError`); a row of another series is left out."""
    if len(text) > MAX_FILE_BYTES:
        raise CaFileError("bc_too_large")
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        fields = {(f or "").strip().upper() for f in reader.fieldnames or ()}
        if not set(BC_COLUMNS) <= fields:
            raise CaFileError("bc_columns")
        rows = []
        for raw in reader:
            row = {(k or "").strip().upper(): cell(v) for k, v in raw.items()}
            series = row.get("SERIES", "").upper()
            if series not in BC_SERIES:
                continue
            rows.append(
                FeedRow(
                    row.get("SYMBOL", "").upper(),
                    "",
                    series,
                    any_date(row.get("EX_DT", "")),
                    bc_subject(row.get("PURPOSE", "")),
                )
            )
    except csv.Error as exc:
        raise CaFileError("bc_unreadable") from exc
    return rows


def any_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return nse_date(raw)

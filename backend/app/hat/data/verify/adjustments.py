"""D2: the stored corporate actions and the adjusted series against independent references.

1. Factors (`--ca-data`, NSE's corporate-action feed pulled independently of HAT's importers,
   read here by parsers of its own):
   - splits and bonuses (`splits_bonus_*.csv`): a bonus a:b is b/(a+b), a face-value split old ->
     new is new/old, a combined row both; the product of the stored split and bonus factors of
     the instrument and day must match within `FACTOR_TOLERANCE`;
   - rights (`rights_issues_*.csv`): the theoretical ex-rights factor (N*P + M*S) / ((N+M)*P)
     for M new shares per N held at S = face value + premium, P the raw close before the ex-date
     (1 when S >= P). The face value is the reference's (`faceVal`, as of the pull: the day
     `ca_import.pull_date` reads from the folder's note; none fails) taken back through the
     reference's face-value splits after the issue day up to the pull (`issue_face_value`), or
     the subject's `@ Rs X`. The tolerance is on the discount, 1 - factor: the stored discount
     within `RIGHTS_TOLERANCE` of the expected one (relative), so a 1:100 issue stored as 1.0
     fails;
   - demergers (`demergers_*.csv`): a stored demerger on the day (the factor is the market's gap
     by the owner's rule, so its size is not compared);
   - dividends (`dividends/ca_YYYY-MM.json`): the cash per share of the day's dividends; the
     stored cash must equal their total, or one row's amount only when every row of the day
     names the same amount (NSE repeats a revised purpose).
   Of the copies of an action stored on several ISINs of a chain, the one the adjusted series
   applies (`chains`: the copy nearest the chain's current ISIN) is judged.
   A stored action no reference holds fails (its factor or cash is unproven); a reference
   action not stored fails unless the owner deleted it (`hat_corporate_action_version`, status
   `deleted`), which warns, as does a demerger whose ex-date close did not gap (by the owner's
   gap rule none is stored); one whose close gap is beyond `DEMERGER_GAP` fails (the market
   priced it, so its factor, the close gap, belongs in the store). A rights issue
   with no price or close, and a bonus of a class nobody checked that is not stored, fail
   (unproven). A reference action whose ISIN and symbol HAT has no instrument for, and the
   reference rows that could not be read, are counted (warn). No reference fails, as does a
   kind with no reference action. Each kind's reference starts with its first action's ex-date
   (`reference_starts`); stored actions before it are not judged, so the check covers only from
   the latest kind's start (`factor_coverage`) and a window before it is not run. Stocks only:
   funds (`INF` ISINs) and SME demergers are not judged. A row NSE classes preference,
   debentures or warrants is judged only when stored (some are equity bonuses by the market's
   gap); a dividend row with no amount is judged by presence (not stored fails). A reference
   dividend not stored with no raw bar in the `PREV_DAYS` before its ex-date fails, unless the
   instrument has no bar before it at all (it adjusts nothing: warn) or NSE lists the same
   dividend under the issuer's new ISIN, stored there (a stale ISIN: warn). Stored dividends
   from the newest dividend file's month (`reference_pulled`) are not judged. A reference action
   is matched by ISIN first, then by the symbol's holder on the ex-date when the ISIN's
   instruments store nothing that day. A stored dividend's bonus-instrument value
   (`StoredAction.bonus`: NSE's row gives the instruments per share, not their face value) fails
   unless a reviewed exception pins it. Every failure a reviewed exception (`D2_EXCEPTIONS`)
   names, at its pinned value, moves to `excepted` (warn).
2. Moves: the price-basis series (`bars_daily_cap`) of the stocks (`isin.ordinary_equity`)
   scanned for close-to-close moves over `audit.JUMP_PCT` between adjacent sessions
   (`audit.jumps`). A move on an ex-date (the factor changed and the move stays) fails; a move
   shaped like a split or bonus ratio (`audit.action_shaped`) with no factor fails (an action
   missing); a drop matching a stored dividend's cash, a move held at a price band (`circuit`:
   closed at the day's high or low, within a tick of the band; `pinned`, `at_band`), a move
   within `RESULTS_DAYS` of a results announcement and a move the way of a market-wide day
   (`MARKET_PCT`) are explained; the rest warn, listed, each reviewed one with its cause from
   `D2_EXCEPTIONS`.
3. Second source (what `verify.fyers` kept): `SAMPLE_SIZE` NIFTY 500 stocks, seeded, each
   read as its chain's current instrument; each calendar year of `SECOND_SOURCE_YEARS`, the
   price return on `bars_daily_cap` (the price basis: no dividends) against Fyers' daily closes
   (`verify_reference/fyers/`). Fyers adjusts splits and bonuses up to the day it is asked, not
   dividends, rights or demergers, and serves only symbols it lists now, so a delisted stock has
   no series; dividends stay judged against NSE's amounts (1.). A year off by more than
   `SECOND_SOURCE_PP` percentage points warns (a reviewed one is excepted), as does a stock with
   no Fyers symbol or no kept answer and a year Fyers has that the series lacks; under
   `SECOND_SOURCE_MIN_STOCKS` of the sample or `SECOND_SOURCE_MIN_YEARS` stock-years compared
   fails (the second source did not run).
4. The bhavcopy's previous close (`PrvsClsgPric`) on each ex-session over the chain's last raw
   close (`bhavcopy_factor`, from `bars_daily_raw`, no network), for each stored split, bonus,
   rights and demerger (a chain's applied copies of one day, multiplied: `chain_day_factors`).
   This is not NSE's adjusted base price: the bhavcopy keeps the raw close on nearly every
   ex-date, which is no evidence on the factor (`no_evidence`). It does not check split or bonus
   factors; only where the bhavcopy's previous close moved must it agree with the stored factor
   (`bhavcopy_verdict`), else it warns (`bhavcopy_prev_close_differs`, a reviewed one excepted).
   Counted by verdict for 2011-2021 and from 2022.
5. Split and bonus evidence: each stored split and bonus day of a chain against four kinds of
   independent evidence on this machine, each `agrees`, `disagrees` or `no_evidence`
   (`evidence_verdict`, within `EVIDENCE_TOLERANCE`): the ex-session's raw open over the last raw
   close (`open_evidence`), the median raw volume of `EVIDENCE_SESSIONS` sessions before over
   after (`volume_evidence`), the share count (paid-up capital over face value) of the results
   XBRL at the quarter ends around the ex-date (2020 on), and screener's paid-up capital at the
   year ends (a bonus only; 2015 on) (`count_evidence`). Counted per year, every action listed
   (`split_bonus_evidence`, pass). A disagreement warns (`split_bonus_<kind>_disagrees`, a
   reviewed one excepted), except a volume one that another kind's agreement outvotes
   (`OUTVOTED`, counted); two kinds disagreeing with none agreeing fails
   (`split_bonus_factor_contradicted`, as does the open disagreeing with none agreeing: the factor
   is shown wrong).
6. Dated values: every event the series applies has its value's `available_on`
   (`undated_values`): one without fails (`ca_value_undated`), as a backtest would never scan
   its name again after the ex-date.
Every failed finding is dated by its ex-dates.
Ex-date moves at a price band (`CIRCUIT_BANDS`) are the market's and moves from a raw close
under `PENNY` are ticks: both are counted, not listed.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from math import log, prod
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any

from app.paths import D2_COST_SPLITS, D2_EXCEPTIONS

from ...money import exact
from ...time import IST
from ..audit import FAIL, JUMP_PCT, PASS, WARN, action_shaped, check_result, finding, jumps
from ..ca_import import PULL_NOTE, pull_date
from ..ca_versions import select as select_version
from ..isin import ordinary_equity
from ..lineage import issuer
from . import covered, dated, seeded_sample, spanning
from .context import Context
from .exceptions import Exceptions, read_exceptions
from .filings import Announcement, read_announcements

if TYPE_CHECKING:
    import duckdb
    import polars as pl

    from ..point_in_time import Event

FACTOR_TOLERANCE = Fraction(1, 1000)  # relative; HAT's MCP bonus factors are rounded to 4 places
RIGHTS_TOLERANCE = Fraction(2, 100)  # of the discount 1 - factor: P is one close, rounded
RIGHTS_FLOOR = Fraction(1, 10**6)  # an issue at or above the close: the factor is exactly 1
RESULTS_DAYS = 3  # a results announcement up to this many days before a move explains it
SAMPLE_SIZE = 100
SAMPLE_SEED = 20261029
SECOND_SOURCE_PP = 2.0
# The calendar years compared with Fyers: the owner asked for 2016..2025, but Fyers serves daily
# history about five years back only (asked on 2026-10-30: 2021-11-02 answered, 2021-10-25
# refused), so the first whole year with a base (the last close of the December before) is 2022.
SECOND_SOURCE_YEARS = (2022, 2025)
# The second source fails unless it compared this share of the sample and this many stock-years.
SECOND_SOURCE_MIN_STOCKS = 0.70
SECOND_SOURCE_MIN_YEARS = 250
CIRCUIT_BANDS = (5.0, 10.0, 20.0)  # NSE's price bands, percent
CIRCUIT_SLACK = 0.25  # percentage points: a band's move after the tick rounding
TICK = 0.05  # rupees: NSE's largest equity tick; a band's price is rounded to it
MARKET_PCT = 3.0  # percent: a day the median stock moved this far is a market-wide move
MARKET_STOCKS = 100  # stocks: fewer traded that day, no market move is read
# A market day explains a stock's move only up to a size: within `MARKET_SPREAD` points of the
# market's move or at most `MARKET_TIMES` times it. A larger move stays unexplained.
MARKET_SPREAD = 10.0
MARKET_TIMES = 3.0
# The explained move classes, every one listed (severity pass) so each can be read back.
EXPLAINED_LISTED = ("circuit", "results", "market")
PENNY = 5.0  # rupees: below it a tick of 0.05 (0.01 since 2023) is 1% or more
PREV_DAYS = 20  # calendar days: a reference's close before the ex-date is looked for this far back
DEMERGER_GAP = Fraction(5, 100)  # a demerger not stored whose close gap is beyond this fails
BHAVCOPY_SLACK = 0.002  # the bhavcopy's factor agrees with the stored one within this (or a tick)
# Independent evidence on a split or bonus factor, each a log distance from the factor expected
# beyond which it disagrees: the ex-session's open over the last close (within the widest price
# band, 20%, of the adjusted base: log 1/0.8), the median volume of `EVIDENCE_SESSIONS` sessions
# each side (trading swings with interest: a factor of 4), the XBRL share count at the quarter
# ends around the ex-date and screener's paid-up capital at the year ends (other issues between).
EVIDENCE_TOLERANCE = {"open": 0.223, "volume": 1.386, "shares": 0.05, "capital": 0.10}
# Evidence whose disagreement another kind's agreement outweighs: it is listed only alone.
OUTVOTED = frozenset({"volume"})
CAPITAL_ROUNDING = 5_000_000.0  # rupees: screener gives the capital in whole crores
ALLOTMENT_DAYS = 15  # a period end this soon after the ex-date may precede the allotment
EVIDENCE_SESSIONS = 20
# A count at a period end further from the ex-date than this is no evidence: XBRL is quarterly,
# screener's balance sheet yearly.
EVIDENCE_PERIOD_DAYS = {"shares": 100, "capital": 400}
COST_SPLIT_GAP = Fraction(10, 100)  # a stored factor this far from the parent's cost share warns
FUND_PREFIX = "INF"  # mutual-fund units (ETFs): not stocks, not judged
NON_EQUITY = frozenset({"preference", "debentures", "warrants"})
PRICE_TYPES = ("split", "bonus")
_REF_DATE = "%d-%b-%Y"


# --- the reference's terms, parsed here -------------------------------------------------------


def ref_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), _REF_DATE).date()  # noqa: DTZ007 - a calendar date
    except ValueError:
        return None


def bonus_factor(ratio: str) -> Fraction | None:
    """Bonus `a:b` (a new shares for every b held): prices scale by b / (a + b)."""
    m = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", ratio or "")
    if not m or not int(m[1]) or not int(m[2]):
        return None
    return Fraction(int(m[2]), int(m[1]) + int(m[2]))


_FV = re.compile(
    r"(?:rs|re|inr)[.\s]*(\d+(?:\.\d+)?)[\s/-]*(?:per\s+share[\s/-]*)?(?:each\s+)?"
    r"to\s*(?:rs|re|inr)[.\s]*(\d+(?:\.\d+)?)"
)


_FV_FROM = re.compile(
    r"from\s*(?:(?:rs|re|inr)[.\s]*)?(\d+(?:\.\d+)?)[\s/-]*(?:per\b[\s/-]*(?:share\b)?[\s/-]*)?"
    r"(?:each\s+)?to\s*(?:face\s*value\s*)?(?:(?:rs|re|inr)[.\s]*)?(\d+(?:\.\d+)?)"
)


def split_factor(subject: str, fv_change: str = "") -> Fraction | None:
    """A face-value split old -> new: prices scale by new / old (from `10->1`, else the
    subject's `Rs 10/- ... To Rs 2/-`, or `From 10/- To Face Value 2/-` and `From Rs 10/- Per
    To Rs 2/-`)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*->\s*([\d.]+)\s*", fv_change or "")
    found = (m[1], m[2]) if m else None
    for pattern in (_FV, _FV_FROM):
        if found is None:
            m = pattern.search((subject or "").lower())
            found = (m[1], m[2]) if m else None
    if found is None:
        return None
    try:
        old, new = Fraction(found[0].rstrip(".")), Fraction(found[1].rstrip("."))
    except (ValueError, ZeroDivisionError):
        return None
    return new / old if old and new and new != old else None


_AMOUNT = re.compile(
    r"\b(?:rs|re|inr)[.:\s-]*(\d+(?:\s*\.+\d+)?)"  # `Re 0..30`, `Rs 0 .70` and `Rs:- 5.50` too
    # a bare amount after the word: `Special Dividend 243 Per Share`, `Div-7.5/-`, `- 1 Rs Per`
    r"|div[a-z]*[\s:-]*(\d+(?:\.\d+)?)\s*(?:/-|per\b|rs\b|re\b)"
)
_NOT_PAYOUT = re.compile(
    r"\(including[^)]*\)?"  # `(Including Special Dividend Of Rs.2/-)`: already in the total
    r"|(?:equity\s+)?shares?\s+of\s+(?:rs|re)\.?\s*[\d.]+"  # `Per Share Of Rs.10/- Each`
    r"|(?:equity\s+)?shares?\s+(?:rs|re)\.?\s*[\d.]+[/\s-]*each"  # `Per Equity Share Re.1/- Each`
    r"|(?:face\s+value|split|bonus|right).*"  # another action joined with `+`
)
_PURPOSES = re.compile(r"/(?!-)")  # `/` parts purposes; `Rs 3/-` is an amount
# not `Sub-Division`; NSE runs words together too: `Meetingdividend`, `Intdividend`
_DIV = re.compile(r"(?<!sub-)(?<!sub )(?:\bdiv|dividend)")
_PAYOUT = ("final", "interim", "special", "spl")


def names_dividend(subject: str) -> bool:
    return bool(_DIV.search((subject or "").lower()))


def dividend_cash(subject: str) -> Decimal | None:
    """The rupees per share of every dividend a purpose text names (`Dividend - Final Rs 22 +
    Special Rs 10` and `Interim Dividend - Rs 5 / Special Dividend - Rs 2` are 32 and 7); None
    when it names none in rupees."""
    total, found = Decimal(0), False
    for part in _PURPOSES.split((subject or "").lower()):
        text = part.strip()
        at = _DIV.search(text)
        if at is None and not text.startswith(_PAYOUT):
            continue
        text = _NOT_PAYOUT.sub(" ", text[at.start() if at else 0 :])
        for m in _AMOUNT.finditer(text):
            total += exact(re.sub(r"\.+", ".", (m[1] or m[2]).replace(" ", ""))) or Decimal(0)
            found = True
    return total if found else None


_PREMIUM = re.compile(
    r"(?:prem[a-z]*|\bprm)[.\s]*(?:of\s+)?(?:(?:rs|re|inr)[.\s]*)?(\d+(?:\.\d+)?)"
)
_PRICE = re.compile(r"@\s*(?:rs|re|inr)\.?\s*(\d+(?:\.\d+)?)")
_AT_PAR = re.compile(r"(?:\bat|@)\s*par\b")


def issue_face_value(
    face_value: Decimal | None, ex_date: date, splits: Sequence[tuple[date, Fraction]]
) -> Decimal | None:
    """The face value on a rights issue's ex-date from the reference's face value (as of the
    day it was pulled) and its face-value splits after the ex-date up to that day (ex date,
    new/old): the pulled face value divided by each such split's new/old."""
    if face_value is None:
        return None
    value = Fraction(face_value)
    for day, ratio in splits:
        if day > ex_date:
            value /= ratio
    return Decimal(value.numerator) / Decimal(value.denominator)


def issue_price(subject: str, premium: str, face_value: Decimal | None) -> Decimal | None:
    """A rights issue's price per share as the reference states it: the issue day's face value
    plus the premium (the subject's, else the parsed column's), the face value alone for an
    issue `at par`, or the subject's `@ Rs X`; None when it states no price."""
    text = (subject or "").lower()
    m = _PREMIUM.search(text)
    prem = exact(m[1] if m else premium)
    if prem is None and _AT_PAR.search(text):
        prem = Decimal(0)
    if prem is None:
        m = _PRICE.search(text)
        return exact(m[1]) if m else None
    return None if face_value is None else face_value + prem


def rights_factor(new: int, held: int, paid: Decimal, prev_close: Decimal) -> Fraction:
    """The theoretical ex-rights factor of `new` shares per `held` at `paid` each, against the
    close before the ex-date; 1 when the issue is not below that close."""
    p, s = Fraction(prev_close), Fraction(paid)
    if s >= p:
        return Fraction(1)
    return (held * p + new * s) / ((held + new) * p)


def close_enough(stored: Fraction, expected: Fraction, tolerance: Fraction) -> bool:
    return abs(stored / expected - 1) <= tolerance


def discount_close(stored: Fraction, expected: Fraction) -> bool:
    """Whether a stored rights factor's discount (1 - factor) is within `RIGHTS_TOLERANCE` of
    the expected discount, relative to it (an expected factor of 1 must be stored as 1)."""
    want = 1 - expected
    return abs((1 - stored) - want) <= max(RIGHTS_TOLERANCE * abs(want), RIGHTS_FLOOR)


def judge_rights(
    stored: Fraction, new: int, held: int, price: Decimal, prev_close: Decimal
) -> tuple[str, Fraction]:
    """`equal` or `factor_mismatch` at the reference's price, with the expected factor."""
    expected = rights_factor(new, held, price, prev_close)
    return ("equal" if discount_close(stored, expected) else "factor_mismatch"), expected


# --- the reference and the store, joined --------------------------------------------------------


@dataclass(frozen=True)
class RefAction:
    kind: str  # split_bonus, rights, demerger, dividend
    ex_date: date
    keys: frozenset[str]  # ISIN and symbol
    factor: Fraction | None = None  # split_bonus: the product of its legs
    cash: Decimal | None = None  # dividend: the total of its rows
    amounts: tuple[Decimal, ...] = ()  # dividend: each row's (NSE repeats a revised purpose)
    entitlement: tuple[int, int] | None = None  # rights: new shares per held
    price: Decimal | None = None  # rights: `issue_price` on the issue day's face value
    text: str = ""
    equity: bool = True  # False: a class nobody checked (a plain bonus without evidence)
    non_equity: bool = False  # NSE classes it preference, debentures or warrants


@dataclass(frozen=True)
class StoredAction:
    instrument_id: str
    ex_date: date
    action_type: str
    factor: Fraction
    cash: Decimal | None
    demerger: bool
    source: str
    text: str = ""

    @property
    def bonus(self) -> Decimal | None:
        """A bonus instrument's value in a dividend (`bonus_instrument N` in the stored text:
        debentures or preference shares given as a bonus, which NSE's dividend purpose does not
        price)."""
        m = _BONUS_INSTRUMENT.search(self.text)
        return exact(m[1]) if m else None

    @property
    def payout(self) -> Decimal | None:
        """A dividend's cash without a bonus instrument's value (`bonus`)."""
        bonus = self.bonus
        return self.cash if self.cash is None or bonus is None else self.cash - bonus


_BONUS_INSTRUMENT = re.compile(r"bonus_instrument (\d+(?:\.\d+)?)")


def is_fund(keys: Iterable[str]) -> bool:
    """Whether an instrument's keys name only mutual-fund ISINs (its ISINs all `INF`)."""
    isins = [k for k in keys if _ISIN.match(k)]
    return bool(isins) and all(k.startswith(FUND_PREFIX) for k in isins)


def _rows(paths: Iterable[Path]) -> Iterable[dict[str, str]]:
    for path in paths:
        with path.open(encoding="utf-8", newline="") as handle:
            yield from csv.DictReader(handle)


def _keys(row: Mapping[str, str]) -> frozenset[str]:
    return frozenset(
        v.strip().upper() for v in (row.get("isin", ""), row.get("symbol", "")) if v and v.strip()
    )


def _split_bonus(row: Mapping[str, str]) -> RefAction | None:
    day = ref_date(row.get("exDate", ""))
    kind = row.get("action_type", "")
    if day is None:
        return None
    non_equity = row.get("instrument_class") in NON_EQUITY
    factor: Fraction | None = Fraction(1)
    for leg in (
        bonus_factor(row.get("ratio_parsed", "")) if "bonus" in kind else Fraction(1),
        split_factor(row.get("subject_raw", ""), row.get("fv_change", ""))
        if "split" in kind
        else Fraction(1),
    ):
        if leg is None and not non_equity:
            return None
        factor = None if leg is None or factor is None else factor * leg  # unpriced: no ratio
    equity = row.get("instrument_class") == "equity" or kind != "bonus"
    return RefAction(
        "split_bonus",
        day,
        _keys(row),
        factor,
        text=row.get("subject_raw", ""),
        equity=equity,
        non_equity=non_equity,
    )


def _rights(
    row: Mapping[str, str], splits: Mapping[frozenset[str], Sequence[tuple[date, Fraction]]]
) -> RefAction | None:
    day = ref_date(row.get("exDate", ""))
    m = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", row.get("entitlement_ratio_parsed", ""))
    if day is None or not m:
        return None
    keys = _keys(row)
    later = [s for k, items in splits.items() if k & keys for s in items]
    fv = issue_face_value(exact(row.get("faceVal", "")), day, later)
    price = issue_price(row.get("subject_raw", ""), row.get("premium_rs_parsed", ""), fv)
    return RefAction(
        "rights",
        day,
        keys,
        entitlement=(int(m[1]), int(m[2])),
        price=price,
        text=row.get("subject_raw", ""),
    )


def combine_legs(legs: Sequence[RefAction]) -> RefAction:
    """A day's split and bonus rows of one instrument as one action: their factors' product
    (HAT stores each leg, judged together); None when no row has a ratio."""
    priced = [leg.factor for leg in legs if leg.factor is not None]
    factor: Fraction | None = None
    for leg_factor in priced:  # an unpriced non-equity leg (no ratio) adds no factor
        factor = leg_factor if factor is None else factor * leg_factor
    return RefAction(
        "split_bonus",
        legs[0].ex_date,
        legs[0].keys,
        factor,
        text=" + ".join(leg.text[:60] for leg in legs),
        equity=all(leg.equity for leg in legs),
        non_equity=all(leg.non_equity for leg in legs),
    )


def read_reference(
    folder: Path,
) -> tuple[list[RefAction], Counter[str], date | None]:  # pragma: no cover - files
    """Every reference action of `folder`, the rows that could not be read, by kind, and the
    day the pull was collected (`ca_import.pull_date`; None without its note)."""
    out: list[RefAction] = []
    skipped: Counter[str] = Counter()
    legs: dict[tuple[frozenset[str], date], dict[str, RefAction]] = defaultdict(dict)
    for row in _rows(sorted(folder.glob("splits_bonus_*.csv"))):
        action = _split_bonus(row)
        if action is None:
            skipped["split_bonus"] += 1
        else:  # the files overlap at their edges: a leg once per text
            legs[(action.keys, action.ex_date)][action.text] = action
    for row in _rows(sorted(folder.glob("etf_unit_actions_*.csv"))):  # from NSE's circulars
        day, fv = ref_date(row.get("ex_effective_date", "")), row.get("old_face_value_rs", "")
        factor = split_factor("", f"{fv}->{row.get('new_face_value_rs', '')}")
        if day is None or factor is None:
            skipped["etf_unit"] += 1
            continue
        etf = RefAction("split_bonus", day, _keys(row), factor, text=row.get("event_type", ""))
        legs[(etf.keys, day)][etf.text] = etf
    out += [combine_legs(list(same.values())) for same in legs.values()]
    paths = [
        p
        for p in sorted(folder.glob("rights_issues_*.csv"))
        if not {"invit", "sme"} & set(p.stem.split("_"))  # equities only
    ]
    pulled = pull_date(folder)  # `faceVal` is as of the pull: a split after it changed nothing
    splits: dict[frozenset[str], list[tuple[date, Fraction]]] = defaultdict(list)
    for row in _rows(sorted(folder.glob("splits_bonus_*.csv"))):  # face-value changes only
        day = ref_date(row.get("exDate", ""))
        ratio = split_factor(row.get("subject_raw", ""), row.get("fv_change", ""))
        if "split" in row.get("action_type", "") and day is not None and ratio is not None:
            if pulled is not None and day <= pulled:
                splits[_keys(row)].append((day, ratio))
    for row in _rows(paths):
        action = _rights(row, splits)
        if action is None:
            skipped["rights"] += 1
        else:
            out.append(action)
    for row in _rows(sorted(folder.glob("demergers_*.csv"))):
        day = ref_date(row.get("exDate", ""))
        if row.get("market", "").strip().lower() == "sme":
            continue  # stocks of the main board only, as for rights
        if day is None:
            skipped["demerger"] += 1
            continue
        out.append(RefAction("demerger", day, _keys(row), text=row.get("ratio", "")))
    amounts: dict[tuple[frozenset[str], date], list[Decimal | None]] = defaultdict(list)
    texts: dict[tuple[frozenset[str], date], list[str]] = defaultdict(list)
    for path in sorted((folder / "dividends").glob("ca_*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            skipped["dividend_file"] += 1
            continue
        for row in rows:
            subject = str(row.get("subject") or "")
            series = str(row.get("series", "")).upper()
            if not names_dividend(subject) or series not in {"EQ", "BE", "BZ"}:
                continue
            day = ref_date(str(row.get("exDate", "")))
            if day is None:
                skipped["dividend"] += 1
                continue
            amount = dividend_cash(subject)  # None: `Interim Dividend`, judged by presence
            key = (_keys({k: str(v) for k, v in row.items()}), day)
            if subject.strip() in texts[key]:
                continue  # the monthly files repeat a row at their edges
            texts[key].append(subject.strip())
            amounts[key].append(amount)
    out += [
        RefAction(
            "dividend",
            day,
            keys,
            cash=sum(priced, Decimal(0))
            if (priced := [a for a in rows if a is not None])
            else None,
            amounts=tuple(priced),
            text=" | ".join(texts[(keys, day)]),
        )
        for (keys, day), rows in amounts.items()
    ]
    return out, skipped, pulled


WANTED = {
    "split_bonus": PRICE_TYPES,
    "rights": ("other",),
    "demerger": ("other",),
    "dividend": ("dividend",),
}
SEVERITY = {
    "factor_mismatch": FAIL,
    "cash_mismatch": FAIL,
    "not_stored": FAIL,
    "not_stored_class_unknown": FAIL,
    "stored_not_in_reference": FAIL,
    "no_prev_close": FAIL,
    "no_issue_price": FAIL,
    "demerger_not_stored": WARN,
    "demerger_not_stored_gapped": FAIL,
    "not_stored_unpriced": FAIL,
    "no_bar": FAIL,
    "no_bar_stale_isin": WARN,
    "before_first_bar": WARN,
    "bonus_instrument": FAIL,
    "deleted_by_owner": WARN,
    "no_instrument": WARN,
}


def _kind(action: StoredAction) -> str:
    if action.action_type in PRICE_TYPES:
        return "split_bonus"
    if action.action_type == "dividend":
        return "dividend"
    return "demerger" if action.demerger else "rights"


def applied_copies(
    per_instrument: Mapping[str, Sequence[StoredAction]],
    heads: Mapping[str, tuple[str, int]],
) -> dict[str, list[StoredAction]]:
    """The stored actions the adjusted series applies, per chain: of each type, the copy of the
    member nearest the chain's current ISIN (`chains.chain_factors`, `chain_dividends`: depth,
    then instrument id). An instrument outside every link is its own chain."""
    best: dict[tuple[str, str], tuple[int, str, list[StoredAction]]] = {}
    for instrument, acts in per_instrument.items():
        chain, depth = heads.get(instrument, (instrument, 0))
        for kind in {a.action_type for a in acts}:
            held = best.get((chain, kind))
            if held is None or (depth, instrument) < held[:2]:
                copy = [a for a in acts if a.action_type == kind]
                best[(chain, kind)] = (depth, instrument, copy)
    out: dict[str, list[StoredAction]] = defaultdict(list)
    for (chain, _), (_, _, acts) in sorted(best.items()):
        out[chain] += acts
    return dict(out)


def judge_ref(
    ref: RefAction, per_chain: Mapping[str, Sequence[StoredAction]], prev_close: Decimal | None
) -> tuple[str, dict[str, Any]]:
    """One reference action against the stored actions of its kind on its day that the
    adjusted series applies, per chain (`applied_copies`; a split and a bonus stored on two
    ISINs of the chain are judged together, one leg per type). When the reference names more
    than one chain, the chain whose actions agree best is judged: its code and evidence."""
    if ref.kind == "demerger":
        first = next(iter(per_chain.values()))[0]
        return "present", {"factor": str(float(first.factor))}
    if ref.kind == "dividend":
        if ref.cash is None:
            return "present_unpriced", {}
        totals = [
            sum((a.payout or Decimal(0) for a in acts), Decimal(0)) for acts in per_chain.values()
        ]
        best = min(totals, key=lambda t: abs(t - ref.cash))  # type: ignore[operator]
        repeated = len(ref.amounts) > 1 and len(set(ref.amounts)) == 1  # one purpose, revised
        code = (
            "equal"
            if best == ref.cash
            else "equal_one_row"
            if repeated and best == ref.amounts[0]
            else "cash_mismatch"
        )
        return code, {"stored": str(best), "expected": str(ref.cash)}
    groups = list(per_chain.values())
    products = []
    for acts in groups:
        product = Fraction(1)
        for a in acts:
            product *= a.factor
        products.append(product)
    if ref.kind == "split_bonus":
        assert ref.factor is not None
        n = min(range(len(products)), key=lambda k: abs(products[k] / ref.factor - 1))  # type: ignore[operator]
        best = products[n]
        code = "equal" if close_enough(best, ref.factor, FACTOR_TOLERANCE) else "factor_mismatch"
        sources = sorted({a.source for a in groups[n]})
        return code, {
            "stored": str(float(best)),
            "expected": str(float(ref.factor)),
            "source": ",".join(sources),
        }
    assert ref.entitlement is not None
    terms = "{}:{}".format(*ref.entitlement)  # NSE's: new shares per shares held
    if ref.price is None:
        return "no_issue_price", {"stored": str(float(products[0])), "entitlement": terms}
    if prev_close is None:
        return "no_prev_close", {"stored": str(float(products[0])), "entitlement": terms}
    judged = [judge_rights(p, *ref.entitlement, ref.price, prev_close) for p in products]
    n, (code, expected) = min(enumerate(judged), key=lambda j: j[1][0] != "equal")
    return code, {
        "stored": str(float(products[n])),
        "entitlement": terms,
        "expected": str(float(expected)),
        "prev_close": str(prev_close),
        "issue_price": str(ref.price),
    }


KINDS = ("split_bonus", "rights", "demerger", "dividend")


def reference_starts(refs: Iterable[RefAction], start: date) -> dict[str, date]:
    """Where each kind's reference starts: the ex-date of its first action, never before
    `start`; a kind the reference has no action of is absent."""
    firsts: dict[str, date] = {}
    for ref in refs:
        if ref.kind not in firsts or ref.ex_date < firsts[ref.kind]:
            firsts[ref.kind] = ref.ex_date
    return {k: max(start, d) for k, d in firsts.items()}


def factor_coverage(starts: Mapping[str, date], end: date) -> tuple[date, date] | None:
    """The span every kind's reference covers (from the latest start), None when a kind has no
    reference: a stored action before its kind's reference starts is not judged, so a window
    reaching before the span finds D2 not run."""
    if any(k not in starts for k in KINDS):
        return None
    return max(starts.values()), end


_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")


@dataclass(frozen=True)
class Stores:
    """What the store says of each instrument: its chain's current id and its distance from it
    (`chains.chains`), its own ISIN and its first raw bar."""

    heads: Mapping[str, tuple[str, int]] = field(default_factory=dict)
    isin_of: Mapping[str, str] = field(default_factory=dict)
    first_bar: Mapping[str, date] = field(default_factory=dict)
    ex_close: Mapping[tuple[str, date], Decimal] = field(default_factory=dict)  # demergers'


def demerger_gap(prev_close: Decimal | None, ex_close: Decimal | None) -> Fraction | None:
    """A demerger's close gap: the ex-date's raw close over the close before (the factor HAT
    stores for a demerger, `audit.check_demergers`); None without both closes."""
    if not prev_close or not ex_close:
        return None
    return Fraction(ex_close) / Fraction(prev_close)


Spans = Mapping[str, Sequence[tuple[date, date | None, frozenset[str]]]]


def ref_instruments(
    keys: Iterable[str],
    by_key: Mapping[str, set[str]],
    day: date | None = None,
    held: Spans | None = None,
) -> list[str]:
    """The instruments a reference action names: those its ISIN finds (each instrument's keys
    hold its chain's ISINs), and only when no ISIN finds one, those its symbol finds. With the
    symbols' spans `held` (symbol -> (from, to, the holder's chain)), a symbol finds only the
    chain that held it on `day`, else the chain holding it now (NSE's split pulls name today's
    ticker), so a ticker another company used before or since never pulls in an unrelated
    chain."""
    keys = list(keys)
    by_isin = set().union(*(by_key.get(k, set()) for k in keys if _ISIN.match(k)))
    if by_isin:
        return sorted(by_isin)
    found: set[str] = set()
    for key in (k for k in keys if not _ISIN.match(k)):
        named = by_key.get(key, set())
        spans = (held or {}).get(key)
        if day is None or not spans:
            found |= named
            continue
        on_day = [c for a, b, c in spans if a <= day and (b is None or day < b)]
        now = [c for _, b, c in spans if b is None]
        found |= named & set().union(*(on_day or now))
    return sorted(found)


def compare_factors(
    refs: Sequence[RefAction],
    stored: Sequence[StoredAction],
    keys_of: Mapping[str, frozenset[str]],
    prev_close: Mapping[tuple[str, date], Decimal],
    deleted: Iterable[tuple[str, date, str]],
    starts: Mapping[str, date],
    held: Spans | None = None,
    ends: Mapping[str, date] | None = None,
    exceptions: Exceptions | None = None,
    stores: Stores | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The reference actions against the stored ones (each stored action's instrument known by
    `keys_of[instrument_id]`: its ISINs and symbols; a symbol by who held it, `held`), and the
    stored actions from where their kind's reference starts (`reference_starts`) to where it
    was pulled (`ends`, exclusive) that no reference holds, by kind: the counts and the
    findings, less the reviewed `exceptions`. Funds are not judged; `prev_close` is the raw
    close in the `PREV_DAYS` before each ex-date of the instruments that have one. `stores`
    gives the chains the adjusted series applies a copy from, each instrument's own ISIN and
    its first raw bar. Every stored dividend with a bonus instrument's value fails
    (`bonus_instrument`) unless a reviewed exception pins that value: NSE's row gives the
    instruments per share, not their face value."""
    by_key = _by_key(keys_of)
    ends = ends or {}
    exceptions = exceptions or Exceptions()
    stores = stores or Stores()
    index: dict[tuple[str, date], list[StoredAction]] = defaultdict(list)
    for action in stored:
        index[(action.instrument_id, action.ex_date)].append(action)
    gone = set(deleted)
    counts: Counter[str] = Counter()
    years: dict[str, Counter[int]] = defaultdict(Counter)
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    used: set[StoredAction] = set()
    bonus_days: set[tuple[str, date]] = set()  # NSE's bonus debentures or preference shares
    found_dividends: set[tuple[str, date]] = set()  # (issuer, ex-date) of the stored ones
    barless: list[tuple[RefAction, list[str]]] = []

    def note(code: str, ref: RefAction, **extra: object) -> None:
        counts[f"{ref.kind}:{code}"] += 1
        years[f"{ref.kind}:{code}"][ref.ex_date.year] += 1
        items[f"{ref.kind}:{code}"].append(
            {"ex_date": ref.ex_date.isoformat(), "keys": sorted(ref.keys), "text": ref.text[:80]}
            | extra
        )

    def stored_on(ids: Iterable[str], ref: RefAction) -> dict[str, list[StoredAction]]:
        return {
            i: acts
            for i in ids
            if (acts := [a for a in index.get((i, ref.ex_date), ()) if _kind(a) == ref.kind])
        }

    for ref in refs:
        if is_fund(ref.keys):
            counts[f"{ref.kind}:fund_not_judged"] += 1
            continue
        ids = ref_instruments(ref.keys, by_key, ref.ex_date, held)
        if not ids:
            note("no_instrument", ref)
            continue
        if all(is_fund(keys_of.get(i, ())) for i in ids):
            counts[f"{ref.kind}:fund_not_judged"] += 1
            continue
        if ref.non_equity:
            bonus_days.update((i, ref.ex_date) for i in ids)
            if ref.factor is None:  # "Issue Of Bonus Debentures": no ratio, proves the day only
                counts[f"{ref.kind}:non_equity_unpriced"] += 1
                continue
        per_instrument = stored_on(ids, ref)
        if not per_instrument and held is not None:  # a stale ISIN: the day's ticker holder
            symbols = [k for k in ref.keys if not _ISIN.match(k)]
            if by_symbol := stored_on(ref_instruments(symbols, by_key, ref.ex_date, held), ref):
                ids, per_instrument = sorted(by_symbol), by_symbol
        for acts in per_instrument.values():
            used.update(acts)
        close = next(
            (prev_close[(i, ref.ex_date)] for i in ids if (i, ref.ex_date) in prev_close), None
        )
        if per_instrument:
            code, detail = judge_ref(ref, applied_copies(per_instrument, stores.heads), close)
            note(code, ref, **detail)
            if ref.kind == "dividend":
                found_dividends.update((issuer(k), ref.ex_date) for k in ref.keys if issuer(k))
        elif any((i, ref.ex_date, t) in gone for i in ids for t in WANTED[ref.kind]):
            note("deleted_by_owner", ref)
        elif (
            ref.kind == "rights"
            and ref.price is not None
            and close is not None
            and rights_factor(*ref.entitlement, ref.price, close) == 1  # type: ignore[misc]
        ):
            note("not_below_market", ref)
        elif ref.kind == "demerger":
            gap = demerger_gap(
                close,
                next(
                    (
                        stores.ex_close[(i, ref.ex_date)]
                        for i in ids
                        if (i, ref.ex_date) in stores.ex_close
                    ),
                    None,
                ),
            )
            if gap is not None and gap < 1 - DEMERGER_GAP:  # the market priced it: store it
                own = sorted({stores.isin_of[i] for i in ids if i in stores.isin_of})
                note(  # NSE's demerger rows name a symbol only: the item carries the ISIN
                    "demerger_not_stored_gapped",
                    ref,
                    isin=own[0] if len(own) == 1 else "",
                    close_gap=str(round(float(gap), 6)),
                )
            else:
                note("demerger_not_stored", ref)  # no gap: the owner's rule stores none
        elif ref.non_equity:
            counts[f"{ref.kind}:non_equity_not_stored"] += 1  # NSE's class: no price factor
        elif ref.kind == "dividend" and ref.cash is None:
            note("not_stored_unpriced", ref)
        elif ref.kind == "dividend" and close is None:
            barless.append((ref, ids))  # judged once every stored dividend is found
        else:
            note("not_stored" if ref.equity else "not_stored_class_unknown", ref)
    for ref, ids in barless:
        firsts = [stores.first_bar.get(i) for i in ids]
        if all(f is None or f >= ref.ex_date for f in firsts):
            note("before_first_bar", ref)  # it adjusts no stored bar
        elif any((issuer(k), ref.ex_date) in found_dividends for k in ref.keys if issuer(k)):
            note("no_bar_stale_isin", ref)  # NSE lists it under the new ISIN too, stored there
        else:
            note("no_bar", ref, why=f"no raw bar in the {PREV_DAYS} days before the ex-date")
    for a in stored:
        if a.bonus and _kind(a) in starts and a.ex_date >= starts[_kind(a)]:
            code = "dividend:bonus_instrument"
            counts[code] += 1
            years[code][a.ex_date.year] += 1
            items[code].append(
                {
                    "isin": stores.isin_of.get(a.instrument_id, ""),
                    "ex_date": a.ex_date.isoformat(),
                    "bonus": str(a.bonus),
                    "cash": None if a.cash is None else str(a.cash),
                    "nse_bonus_row": (a.instrument_id, a.ex_date) in bonus_days,
                }
            )
        if (
            a in used
            or a.action_type not in {*PRICE_TYPES, "other", "dividend"}
            or _kind(a) not in starts
            or a.ex_date < starts[_kind(a)]
            or (_kind(a) in ends and a.ex_date >= ends[_kind(a)])
        ):
            continue
        if is_fund(keys_of.get(a.instrument_id, ())):
            counts[f"{_kind(a)}:fund_not_judged"] += 1
            continue
        if a.payout == 0 and (a.instrument_id, a.ex_date) in bonus_days:
            counts["dividend:bonus_instrument_day"] += 1  # NSE's non-equity bonus row that day
            continue
        code = f"{_kind(a)}:stored_not_in_reference"
        counts[code] += 1
        years[code][a.ex_date.year] += 1
        items[code].append(
            {
                "instrument_id": a.instrument_id,
                "isin": stores.isin_of.get(a.instrument_id, ""),
                "keys": sorted(keys_of.get(a.instrument_id, ())),
                "ex_date": a.ex_date.isoformat(),
                "type": a.action_type,
                "source": a.source,
                "factor": str(float(a.factor)),
                "cash": None if a.cash is None else str(a.cash),
            }
        )
    findings = []
    for code in sorted(items):
        level = SEVERITY.get(code.split(":", 1)[1])
        if level is not None:
            name = code.replace(":", "_").replace("demerger_demerger", "demerger")
            kept = exceptions.split(name, items[code])
            if not kept:
                continue
            by_year = dict(sorted(Counter(int(i["ex_date"][:4]) for i in kept).items()))
            found = {**finding(name, level, kept), "by_year": by_year}
            if level == FAIL:
                found = dated(found, [i["ex_date"] for i in kept])
            findings.append(found)
    return {k: counts[k] for k in sorted(counts)}, findings


# --- demerger factors against the cost-of-acquisition split ---------------------------------------


@dataclass(frozen=True)
class CostSplit:
    """A demerger's published apportionment of the parent's cost of acquisition: `parent` is the
    share (0 to 1) that stays with the parent's shares."""

    isin: str
    ex_date: date
    parent: Fraction
    source: str


def read_cost_splits(
    rows: Iterable[Mapping[str, str]],
) -> tuple[dict[tuple[str, date], CostSplit], int]:
    """The reference's splits by (parent ISIN, ex-date) and the rows that could not be read: a
    row needs an ISIN, an ISO ex-date, a `parent_pct` strictly between 0 and 100 and a
    `source_url`; a second row for the same ISIN and day is not read."""
    out: dict[tuple[str, date], CostSplit] = {}
    unread = 0
    for row in rows:
        isin = (row.get("isin") or "").strip().upper()
        try:
            day = date.fromisoformat((row.get("ex_date") or "").strip())
            pct = Fraction((row.get("parent_pct") or "").strip())
        except ValueError:
            unread += 1
            continue
        source = (row.get("source_url") or "").strip()
        if not (_ISIN.match(isin) and 0 < pct < 100 and source) or (isin, day) in out:
            unread += 1
            continue
        out[(isin, day)] = CostSplit(isin, day, pct / 100, source)
    return out, unread


def compare_cost_splits(
    demergers: Iterable[tuple[str, str, date, Fraction]],
    splits: Mapping[tuple[str, date], CostSplit],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Each stored demerger (ISIN, symbol, ex-date, factor) against its company's published
    cost-of-acquisition split. The split is a tax apportionment by book value (Income-tax Act
    s.49(2C)), the factor the market's gap, so the two differ by design and a far one is a
    lead, not a defect: beyond `COST_SPLIT_GAP` it warns (`demerger_cost_split_far`, listed),
    as does a demerger with no published split (`demerger_cost_split_missing`)."""
    far, missing, gaps = [], [], []
    for isin, symbol, day, factor in demergers:
        split = splits.get((isin, day))
        item: dict[str, Any] = {"isin": isin, "symbol": symbol, "ex_date": day.isoformat()}
        if split is None:
            missing.append(item)
            continue
        gap = factor - split.parent
        gaps.append(abs(gap))
        if abs(gap) > COST_SPLIT_GAP:
            far.append(
                item
                | {
                    "stored": str(round(float(factor), 6)),
                    "cost_split": str(round(float(split.parent), 6)),
                    "gap": str(round(float(gap), 4)),
                    "source": split.source,
                }
            )
    gaps.sort()
    summary = {
        "compared": len(gaps),
        "within": sum(g <= COST_SPLIT_GAP for g in gaps),
        "far": len(far),
        "no_split": len(missing),
        "median_gap": str(round(float(gaps[len(gaps) // 2]), 4)) if gaps else None,
        "gap_limit": str(float(COST_SPLIT_GAP)),
    }
    far.sort(key=lambda r: -abs(float(r["gap"])))
    findings = []
    if far:
        findings.append(finding("demerger_cost_split_far", WARN, far))
    if missing:
        findings.append(finding("demerger_cost_split_missing", WARN, missing))
    return summary, findings


# --- the move scan -----------------------------------------------------------------------------


def explained_by_results(
    day: date, announcements: Sequence[Announcement], within: int = RESULTS_DAYS
) -> bool:
    """Whether a results announcement fell in the `within` days before `day` (or on it)."""
    first = day - timedelta(days=within)
    return any(first <= a.announced.date() <= day for a in announcements)


def pinned(move_pct: float, close: float, high: float, low: float) -> bool:
    """Whether the day closed where a band would hold it: at the day's high after a rise, at its
    low after a fall. A stock that traded beyond its close was not held at a band (a stock in
    F&O has none: RPOWER 2019-05-03 closed 20.6% up at 6.45 after a high of 6.65)."""
    return close == (high if move_pct > 0 else low)


def at_band(move_pct: float, prev_raw_close: float = 0.0) -> bool:
    """Whether a move is a price band's (`CIRCUIT_BANDS`): within `CIRCUIT_SLACK`, or, given the
    raw close before it, within one `TICK` of the band's price (NSE rounds the band's price to
    the tick: 20% on 6.60 is 7.92, traded at 7.95, a move of 20.45%)."""
    slack = CIRCUIT_SLACK
    if prev_raw_close > 0:
        slack = max(slack, 100 * TICK / prev_raw_close)
    return any(abs(abs(move_pct) - band) <= slack for band in CIRCUIT_BANDS)


def market_move(moves: Sequence[float]) -> float | None:
    """The market's move on a day: the median of every stock's move that day (percent); None
    with fewer than `MARKET_STOCKS` stocks."""
    if len(moves) < MARKET_STOCKS:
        return None
    ordered = sorted(moves)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def dividends_by_chain(
    stored: Iterable[StoredAction], heads: Mapping[str, tuple[str, int]]
) -> dict[tuple[str, date], float]:
    """Each chain's dividend cash per ex-date, keyed by the chain's current instrument (as the
    adjusted series is: a dividend stored on an older ISIN of the chain counts against its
    moves): an instrument's dividends of one day are summed, then the copy nearest the chain's
    current ISIN wins (`chains.chain_dividends`)."""
    from ..chains import chain_dividends  # noqa: PLC0415

    per_day: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for a in stored:
        if a.action_type == "dividend" and a.cash:
            per_day[(a.instrument_id, a.ex_date)] += a.cash
    rows = [(i, d, cash) for (i, d), cash in per_day.items()]
    paid = chain_dividends(rows, heads)
    return {(str(c.instrument_id), c.ex_date): float(c.amount) for c in paid}


def classify_move(
    move: Mapping[str, Any],
    explained: bool,
    prev_raw_close: float,
    cash: float = 0.0,
    market: float | None = None,
) -> str:
    """One adjusted move beyond the threshold (`audit.jumps`): `ex_date_jump` (`ex_date_circuit`
    when the move is a price band's: the market's, on an ex-date), `dividend` (a raw drop
    that is within `audit.JUMP_PCT` once the day's stored dividend `cash` is added back), `penny`
    (from a raw close under `PENNY`, where a tick is a large move), `action_shaped`, `circuit`
    (a price band's move: closed at the day's extreme, `move["pinned"]`, at the band),
    `results`, `market` (the day's `market` move, the median stock's, at least `MARKET_PCT`
    the same way, and the move within `explained_by_market`) or `unexplained`. A move not
    pinned is never a band's."""
    if move["ex_date"]:
        held = move.get("pinned", False) and at_band(move["move_pct"])
        return "ex_date_circuit" if held else "ex_date_jump"
    if cash > 0 and prev_raw_close > 0:
        paid = 100 * cash / prev_raw_close  # the move with the cash added back stays in range
        if move["raw_move_pct"] < 0 and abs(move["raw_move_pct"] + paid) <= JUMP_PCT:
            return "dividend"
    if prev_raw_close < PENNY:
        return "penny"
    if action_shaped(move["raw_move_pct"]):
        return "action_shaped"
    if move.get("pinned", False) and (
        at_band(move["move_pct"]) or at_band(move["raw_move_pct"], prev_raw_close)
    ):
        return "circuit"
    if explained:
        return "results"
    if market is not None and explained_by_market(move["move_pct"], market):
        return "market"
    return "unexplained"


def explained_by_market(move_pct: float, market: float) -> bool:
    """Whether the day's market move (`market`, percent) explains a stock's `move_pct`: the market
    moved at least `MARKET_PCT` the same way, and the move is within `MARKET_SPREAD` points of
    it or at most `MARKET_TIMES` times it."""
    if abs(market) < MARKET_PCT or (market > 0) != (move_pct > 0):
        return False
    return abs(move_pct - market) <= MARKET_SPREAD or abs(move_pct) <= MARKET_TIMES * abs(market)


_YEAR_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date, isin, symbol,
       CAST(close AS DOUBLE) AS close, CAST(raw_close AS DOUBLE) AS raw_close,
       CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low
FROM bars_daily_cap WHERE date BETWEEN ? AND ?
"""


def _moves(
    ctx: Context,
    con: duckdb.DuckDBPyConnection,
    announcements: tuple[Mapping, Mapping],
    members: Mapping[str, Sequence[tuple[date, date | None]]],
    dividends: Mapping[tuple[str, date], float],
    exceptions: Exceptions,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - real data
    import polars as pl  # noqa: PLC0415

    position = {d: n for n, d in enumerate(ctx.store.days("bars_daily_raw"))}
    by_isin, by_symbol = announcements
    counts: Counter[str] = Counter()
    years: dict[str, Counter[str]] = defaultdict(Counter)
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for year in range(ctx.start.year, ctx.end.year + 1):
        first, last = max(ctx.start, date(year, 1, 1)), min(ctx.end, date(year, 12, 31))
        if first > last:
            continue
        frame = pl.from_arrow(con.execute(_YEAR_SQL, [first - timedelta(days=14), last]).arrow())
        assert isinstance(frame, pl.DataFrame)
        frame = frame.filter(
            pl.col("isin").map_elements(ordinary_equity, return_dtype=pl.Boolean)
        ).sort("instrument_id", "date")
        market = _market_moves(frame)
        for (instrument,), part in frame.partition_by("instrument_id", as_dict=True).items():
            days = part["date"].to_list()
            found = jumps(
                days,
                part["close"].fill_null(0.0).to_list(),
                part["raw_close"].fill_null(0.0).to_list(),
                position,
            )
            if not found:
                continue
            isins, symbols = part["isin"].to_list(), part["symbol"].to_list()
            raws = part["raw_close"].to_list()
            closes, highs, lows = (part[c].to_list() for c in ("close", "high", "low"))
            for move in found:
                day = date.fromisoformat(move["date"])
                if not first <= day <= last:
                    continue
                at = days.index(day)
                move["pinned"] = pinned(move["move_pct"], closes[at], highs[at], lows[at])
                said = [*by_isin.get(isins[at], ()), *by_symbol.get(symbols[at], ())]
                code = classify_move(
                    move,
                    explained_by_results(day, said),
                    raws[at - 1] or 0.0,
                    dividends.get((str(instrument), day), 0.0),
                    market.get(day),
                )
                member = any(
                    a <= day and (b is None or day < b) for a, b in members.get(str(instrument), ())
                )
                counts[code] += 1
                years[str(year)][code] += 1
                if member:
                    counts[f"{code}_nifty500"] += 1
                market_pct = market.get(day)
                items[code].append(
                    {**move, "symbol": symbols[at], "isin": isins[at], "nifty500": member}
                    | ({"market_pct": round(market_pct, 2)} if market_pct is not None else {})
                )
    for code in items:  # members first, then the largest moves
        items[code].sort(key=lambda m: (not m["nifty500"], -abs(m["move_pct"])))
    findings = []
    for code, level in (("ex_date_jump", FAIL), ("action_shaped", FAIL), ("unexplained", WARN)):
        kept = exceptions.split(f"move_{code}", items[code])
        if kept:
            found = {
                **finding(f"move_{code}", level, kept),
                "count": len(kept),
                "nifty500": sum(m["nifty500"] for m in kept),
            }
            if level == FAIL:
                found = dated(found, [m["date"] for m in kept])
            findings.append(found)
    for code in EXPLAINED_LISTED:  # every explained move, listed in full for reading back
        if items[code]:
            listed = sorted(items[code], key=lambda m: (m["date"], m["symbol"]))
            findings.append(
                {**finding(f"move_{code}", PASS, ()), "count": len(listed), "items": listed}
            )
    return {
        "stocks_only": "ordinary equity ISINs: funds, rights entitlements and debt not scanned",
        "moves_over_pct": dict(counts),
        "by_year": {k: dict(v) for k, v in sorted(years.items())},
    }, findings


def _market_moves(frame: pl.DataFrame) -> dict[date, float]:  # pragma: no cover - real data
    """Each day's `market_move` over the stocks of `frame` (sorted by instrument and day) with a
    raw close of at least `PENNY` the day before."""
    import polars as pl  # noqa: PLC0415

    prev = pl.col("close").shift(1).over("instrument_id")
    moves = (
        frame.with_columns(
            (100 * (pl.col("close") / prev - 1)).alias("move"),
            pl.col("raw_close").shift(1).over("instrument_id").alias("prev_raw"),
        )
        .filter(pl.col("move").is_not_null() & (pl.col("prev_raw") >= PENNY))
        .group_by("date")
        .agg(pl.col("move"))
    )
    return {
        day: value
        for day, values in moves.iter_rows()
        if (value := market_move(values)) is not None
    }


# --- the second source --------------------------------------------------------------------------


def fyers_request(symbol: str, year: int, base: bool = False) -> tuple[str, dict[str, str]]:
    """The kept file's name and the Fyers history request of `symbol`'s daily candles in one
    calendar year (a daily request spans at most 366 days), or for the `base` year only its
    December: its last close is the first year's base."""
    request = {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": f"{year}-12-01" if base else f"{year}-01-01",
        "range_to": f"{year}-12-31",
        "cont_flag": "1",
    }
    return f"{symbol.removeprefix('NSE:')}_{year}.json", request


FYERS_INVALID_SYMBOL = -300  # Fyers' "Invalid symbol provided": an answer about the symbol


def fyers_answered(answer: Mapping[str, Any]) -> bool:
    """A history answer to keep: candles (`ok`), `no_data` for a year without any, or Fyers'
    "invalid symbol" (no series; read as no closes, so the stock counts as missing)."""
    return answer.get("s") in ("ok", "no_data") or answer.get("code") == FYERS_INVALID_SYMBOL


def fyers_closes(data: bytes) -> list[tuple[date, float]]:
    """(IST date, close) of each daily candle in a kept Fyers answer."""
    out = []
    for row in json.loads(data).get("candles") or []:
        if isinstance(row, list) and len(row) >= 5:
            out.append((datetime.fromtimestamp(int(row[0]), IST).date(), float(row[4])))
    return out


def year_returns(points: Iterable[tuple[date, float]]) -> dict[int, float]:
    """Each calendar year's return from (date, total-return close), last close over last."""
    last: dict[int, float] = {}
    for day, close in sorted(points):
        last[day.year] = close
    return {y: 100 * (last[y] / last[y - 1] - 1) for y in sorted(last) if y - 1 in last}


def compare_returns(
    ours: Mapping[int, float], theirs: Mapping[int, float], first_year: int, last_year: int
) -> list[dict[str, Any]]:
    """The years both have (whole years inside the window) with their returns and gap."""
    return [
        {
            "year": y,
            "hat_pct": round(ours[y], 2),
            "fyers_pct": round(theirs[y], 2),
            "gap_pp": round(ours[y] - theirs[y], 2),
        }
        for y in sorted(set(ours) & set(theirs))
        if first_year <= y <= last_year
    ]


_PRICE_SQL = """
SELECT date, CAST(close AS DOUBLE) FROM bars_daily_cap
WHERE instrument_id = ? AND date BETWEEN ? AND ? ORDER BY date
"""


def second_source_sample(
    symbols: Mapping[str, str],
    members: Iterable[str],
    heads: Mapping[str, tuple[str, int]],
) -> list[str]:
    """The seeded `SAMPLE_SIZE` of the NIFTY 500's past and present members (instrument ids)
    that have a symbol, one per chain: the members in their seeded order, a member whose chain
    was drawn already skipped for the next (an old ISIN and its successor are one company)."""
    pool = sorted(i for i in members if i in symbols)
    out: list[str] = []
    drawn: set[str] = set()
    for instrument in seeded_sample(pool, len(pool), SAMPLE_SEED):
        chain = heads.get(instrument, (instrument, 0))[0]
        if chain not in drawn:
            drawn.add(chain)
            out.append(instrument)
            if len(out) == SAMPLE_SIZE:
                break
    return out


def fyers_sample(
    sample: Iterable[str],
    symbols: Mapping[str, str],
    heads: Mapping[str, tuple[str, int]],
    broker: Mapping[str, str],
) -> list[tuple[str, str, str | None]]:
    """Each sampled instrument as (its chain's current instrument, that one's symbol, its Fyers
    symbol or None): the price basis carries the chain's current id, and Fyers names a stock by
    the symbol it trades under now."""
    out = []
    for instrument in sample:
        current = heads.get(instrument, (instrument, 0))[0]
        out.append((current, symbols.get(current, symbols[instrument]), broker.get(current)))
    return out


def nifty500_members(ctx: Context) -> dict[str, list[tuple[date, date | None]]]:
    members: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for i, a, b in ctx.rows(
        "SELECT CAST(instrument_id AS TEXT), effective_from, effective_to FROM hat_index_member "
        "WHERE index_name = 'NIFTY 500'"
    ):
        members[i].append((a, b))
    return members


def judge_second_source(
    sampled: int, compared_stocks: int, stock_years: int
) -> list[dict[str, Any]]:
    """`second_source_not_run` (fail) when the second source compared under
    `SECOND_SOURCE_MIN_STOCKS` of the sample's stocks or under `SECOND_SOURCE_MIN_YEARS`
    stock-years, or with no sample at all: too little of it to vouch for the series."""
    if (
        not sampled
        or compared_stocks < SECOND_SOURCE_MIN_STOCKS * sampled
        or stock_years < SECOND_SOURCE_MIN_YEARS
    ):
        detail = f"{compared_stocks} of {sampled} stocks, {stock_years} stock-years compared"
        return [finding("second_source_not_run", FAIL, [detail])]
    return []


def years_only_theirs(
    ours: Mapping[int, float], theirs: Mapping[int, float], first_year: int, last_year: int
) -> list[int]:
    """The whole years in the window the second source has a return for and the series lacks."""
    return [y for y in sorted(theirs) if first_year <= y <= last_year and y not in ours]


def _second_source(
    ctx: Context,
    con: duckdb.DuckDBPyConnection,
    members: Mapping[str, Sequence[tuple[date, date | None]]],
    heads: Mapping[str, tuple[str, int]],
    exceptions: Exceptions,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - real data
    symbols = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
    isins = dict(ctx.rows("SELECT CAST(id AS TEXT), isin FROM hat_instrument"))
    broker = dict(
        ctx.rows(
            "SELECT CAST(instrument_id AS TEXT), broker_symbol FROM hat_broker_symbol "
            "WHERE broker = 'fyers'"
        )
    )
    sample = fyers_sample(second_source_sample(symbols, members, heads), symbols, heads, broker)
    first_year = max(SECOND_SOURCE_YEARS[0], ctx.start.year + 1)  # whole years only
    last_year = min(SECOND_SOURCE_YEARS[1], ctx.end.year - 1)
    folder = ctx.reference_dir("fyers")
    compared, stocks, off, missing, unlisted, lacking = 0, 0, [], [], [], []
    gaps: list[float] = []
    for instrument, symbol, fyers in sample:
        if fyers is None:
            unlisted.append(symbol)
            continue
        theirs_points: list[tuple[date, float]] = []
        for year in range(first_year - 1, last_year + 1):
            path = folder / fyers_request(fyers, year, base=year < first_year)[0]
            if path.is_file():
                theirs_points += fyers_closes(path.read_bytes())
        theirs = year_returns(theirs_points)
        if not theirs:
            missing.append(symbol)
            continue
        points = con.execute(
            _PRICE_SQL, [instrument, date(first_year - 1, 1, 1), date(last_year, 12, 31)]
        ).fetchall()
        ours = year_returns(points)
        rows = compare_returns(ours, theirs, first_year, last_year)
        stocks += bool(rows)
        compared += len(rows)
        gaps += [abs(r["gap_pp"]) for r in rows]
        isin = isins.get(instrument, "")
        off += [
            {"symbol": symbol, "isin": isin, "date": f"{r['year']}-12-31", **r}
            for r in rows
            if abs(r["gap_pp"]) > SECOND_SOURCE_PP
        ]
        lacking += [
            {"symbol": symbol, "isin": isin, "year": y}
            for y in years_only_theirs(ours, theirs, first_year, last_year)
        ]
    off.sort(key=lambda r: -abs(r["gap_pp"]))
    gaps.sort()
    summary = {
        "source": (
            "Fyers daily history (split- and bonus-adjusted to the day fetched; no dividends, "
            "rights or demergers) against the price basis bars_daily_cap; dividends are judged "
            "against NSE's amounts instead"
        ),
        "sample": len(sample),
        "years": f"{first_year}..{last_year}",
        "stocks_compared": stocks,
        "stock_years_compared": compared,
        "median_gap_pp": round(gaps[len(gaps) // 2], 3) if gaps else None,
        "p90_gap_pp": round(gaps[int(len(gaps) * 0.9)], 3) if gaps else None,
        "within_pp": SECOND_SOURCE_PP,
        "years_off": len(off),
        "symbols_not_at_fyers": len(unlisted),
        "symbols_without_answer": len(missing),
        "years_only_fyers": len(lacking),
    }
    findings = []
    off = exceptions.split("second_source_year_off", off)
    if off:
        findings.append(finding("second_source_year_off", WARN, off))
    if lacking:
        findings.append(finding("second_source_year_not_ours", WARN, lacking))
    if unlisted:
        findings.append(finding("second_source_not_at_fyers", WARN, unlisted))
    if missing:
        findings.append(finding("second_source_missing", WARN, missing))
    findings += judge_second_source(len(sample), stocks, compared)
    return summary, findings


# --- the stored factors against the bhavcopy and independent evidence --------------------------

# A raw bar around an ex-date: day, open, the bhavcopy's previous close, close, volume.
RawBar = tuple[date, float, float, float, int]


def bhavcopy_factor(bars: Sequence[RawBar], ex_date: date) -> tuple[float, float, float] | None:
    """The bhavcopy's factor for an action from one chain's raw bars around its ex-date: the
    ex-session's previous close (`PrvsClsgPric`) over the chain's last close before it, with
    both prices; None without a bar on each side. The bhavcopy's previous close is the raw
    close on nearly every ex-date, so this is no evidence on a factor unless it moved."""
    before = [b for b in bars if b[0] < ex_date]
    after = [b for b in bars if b[0] >= ex_date]
    if not before or not after or before[-1][3] <= 0:
        return None
    base, last = after[0][2], before[-1][3]
    return base / last, base, last


def bhavcopy_verdict(stored: float, bhavcopy: float, last_close: float) -> str:
    """How the bhavcopy's factor (`bhavcopy_factor`) stands to the stored factor: `no_evidence`
    when it is 1 (the previous close is the raw close, as on nearly every ex-date), else `agrees`
    within `BHAVCOPY_SLACK` or one tick of the last close, or `differs`."""
    slack = max(BHAVCOPY_SLACK, TICK / last_close) if last_close > 0 else BHAVCOPY_SLACK
    if abs(bhavcopy - 1) <= slack and abs(stored - 1) > slack:
        return "no_evidence"
    return "agrees" if abs(stored - bhavcopy) <= slack else "differs"


def open_evidence(bars: Sequence[RawBar], ex_date: date) -> float | None:
    """The ex-session's raw open over the chain's last raw close before it: the market's first
    price on the new share, which a split or bonus scales by its factor."""
    before = [b for b in bars if b[0] < ex_date]
    after = [b for b in bars if b[0] >= ex_date]
    if not before or not after or before[-1][3] <= 0 or after[0][1] <= 0:
        return None
    return after[0][1] / before[-1][3]


def volume_evidence(
    bars: Sequence[RawBar], ex_date: date, sessions: int = EVIDENCE_SESSIONS
) -> float | None:
    """The median raw volume of the `sessions` sessions before the ex-date over that of the
    `sessions` from it: a split or bonus multiplies the shares, and so the shares traded, by
    1/factor. None with under half the sessions traded on either side."""
    before = [b[4] for b in bars if b[0] < ex_date and b[4] > 0][-sessions:]
    after = [b[4] for b in bars if b[0] >= ex_date and b[4] > 0][:sessions]
    if min(len(before), len(after)) < sessions // 2:
        return None
    return median(before) / median(after)


def count_evidence(
    rows: Sequence[tuple[date, float, float]],
    ex_date: date,
    days: int,
    factors: Mapping[date, float],
    *,
    split: bool,
    rounding: float = 0.0,
) -> tuple[float, float, float] | None:
    """The share count around an ex-date, from (period end, paid-up capital, face value) rows:
    the count (capital over face value) at the last period end before the ex-date over that at the
    first `ALLOTMENT_DAYS` or more after it (the new shares are allotted after the record date),
    each within `days`; what the stored actions say it should be (the product of `factors`, by
    ex-date, between the two period ends: a split or bonus multiplies the shares by 1/factor); and
    the log slack the capital's `rounding` (absolute, either way) allows on each side. None without
    a row on each side, or for a split (`split`) whose filings give the same face value on both
    sides: the filer did not restate it, so the count shows nothing."""
    span = timedelta(days=days)
    first = ex_date + timedelta(days=ALLOTMENT_DAYS)
    before = [r for r in sorted(rows) if ex_date - span <= r[0] < ex_date and r[1] > 0 < r[2]]
    after = [r for r in sorted(rows) if first <= r[0] <= ex_date + span and r[1] > 0 < r[2]]
    if not before or not after or (split and before[-1][2] == after[0][2]):
        return None
    (start, capital_before, face_before), (end, capital_after, face_after) = before[-1], after[0]
    expected = prod(f for day, f in factors.items() if start < day <= end)
    observed = (capital_before / face_before) / (capital_after / face_after)
    return observed, expected, rounding / capital_before + rounding / capital_after


def evidence_verdict(expected: float, observed: float | None, tolerance: float) -> str:
    """`no_evidence` without an observation, or when both the expected and the observed factor
    are within `tolerance` (log) of 1 (the evidence cannot tell the action from no action); else
    `disagrees` when the observed factor is more than `tolerance` from the expected one (so a
    factor stored near 1 that the evidence puts far from it disagrees), else `agrees`."""
    if observed is None or observed <= 0 or expected <= 0:
        return "no_evidence"
    if abs(log(expected)) <= tolerance and abs(log(observed)) <= tolerance:
        return "no_evidence"
    return "disagrees" if abs(log(observed / expected)) > tolerance else "agrees"


def factor_contradicted(verdicts: Mapping[str, str]) -> bool:
    """A stored factor is shown wrong when no kind of evidence agrees with it and the open (the
    market's first price on the new share, the most direct) or at least two kinds disagree."""
    seen = list(verdicts.values())
    if "agrees" in seen:
        return False
    return verdicts.get("open") == "disagrees" or seen.count("disagrees") >= 2


def chain_day_factors(
    stored: Iterable[StoredAction], heads: Mapping[str, tuple[str, int]]
) -> tuple[dict[tuple[str, date], Fraction], ...]:
    """Each (chain, ex-date)'s factor of every price action (splits, bonuses, rights, demergers),
    of its splits and bonuses, and of its bonuses alone: the product of the copies the adjusted
    series applies (`applied_copies`), so an action stored on two ISINs of a chain counts once."""
    by_day: dict[date, dict[str, list[StoredAction]]] = defaultdict(lambda: defaultdict(list))
    for a in stored:
        if a.action_type in PRICE_TYPES or a.action_type == "other" or a.demerger:
            by_day[a.ex_date][a.instrument_id].append(a)
    every: dict[tuple[str, date], Fraction] = {}
    split_bonus: dict[tuple[str, date], Fraction] = {}
    bonus: dict[tuple[str, date], Fraction] = {}
    for day, per_instrument in by_day.items():
        for chain, acts in applied_copies(per_instrument, heads).items():
            key = (chain, day)
            every[key] = prod(a.factor for a in acts)
            if any(a.action_type in PRICE_TYPES for a in acts):
                split_bonus[key] = prod(a.factor for a in acts if a.action_type in PRICE_TYPES)
            if any(a.action_type == "bonus" for a in acts):
                bonus[key] = prod(a.factor for a in acts if a.action_type == "bonus")
    return every, split_bonus, bonus


_RAW_BARS_SQL = """
SELECT isin, date, CAST(open AS DOUBLE), CAST(prev_close AS DOUBLE), CAST(close AS DOUBLE),
       volume
FROM bars_daily_raw_latest WHERE date BETWEEN ? AND ? AND isin IN (SELECT unnest(?))
"""
RAW_BARS_DAYS = (45, 45)  # calendar days of bars read before and after each ex-date
# Each instrument's share count (paid-up capital over face value, both of one XBRL context) at
# each period end its NSE results XBRL gives, the latest version's; and its paid-up capital at
# each year end screener's balance sheet gives (no face value by period there, so a count only
# for a bonus).
_SHARES_SQL = """
WITH pairs AS (
    SELECT f.instrument_id, f.period_end, f.version, a.context,
           max(a.value) FILTER (WHERE a.item = 'paid_up_capital') AS paid_up,
           max(a.value) FILTER (WHERE a.item = 'face_value') AS face
    FROM hat_fundamental_fact a JOIN hat_fundamental_filing f ON f.id = a.filing_id
    WHERE f.source = 'nse' AND a.item IN ('paid_up_capital', 'face_value')
      AND a.period_end = f.period_end
    GROUP BY f.id, f.instrument_id, f.period_end, f.version, a.context
)
SELECT DISTINCT ON (instrument_id, period_end)
       CAST(instrument_id AS TEXT), period_end, paid_up, face
FROM pairs WHERE paid_up IS NOT NULL AND face IS NOT NULL
ORDER BY instrument_id, period_end, version DESC, context
"""
_CAPITAL_SQL = """
SELECT DISTINCT ON (f.instrument_id, a.period_end)
       CAST(f.instrument_id AS TEXT), a.period_end, a.value
FROM hat_fundamental_fact a JOIN hat_fundamental_filing f ON f.id = a.filing_id
WHERE f.source = 'screener' AND a.item = 'share_capital' AND a.period_kind = 'instant'
ORDER BY f.instrument_id, a.period_end, f.fetched_at DESC
"""


def _raw_bars(
    con: duckdb.DuckDBPyConnection, keys: Sequence[tuple[date, set[str]]]
) -> list[list[RawBar]]:  # pragma: no cover - real data
    """Each (ex-date, ISINs) key's raw bars within `RAW_BARS_DAYS`, one per day (the most traded
    of a chain's ISINs that day), in date order. Read a year at a time: the store is read by
    date."""
    before, after = (timedelta(days=d) for d in RAW_BARS_DAYS)
    wanted: dict[int, list[int]] = defaultdict(list)
    for n, (ex, _) in enumerate(keys):
        wanted[ex.year].append(n)
    out: list[list[RawBar]] = [[] for _ in keys]
    for year, asked in sorted(wanted.items()):
        rows = con.execute(
            _RAW_BARS_SQL,
            [
                date(year, 1, 1) - before,
                date(year, 12, 31) + after,
                sorted(set().union(*(keys[n][1] for n in asked))),
            ],
        ).fetchall()
        by_isin: dict[str, list[RawBar]] = defaultdict(list)
        for isin, day, op, prev, close, volume in rows:
            by_isin[isin].append((day, op or 0.0, prev or 0.0, close or 0.0, volume or 0))
        for n in asked:
            ex, isins = keys[n]
            days: dict[date, RawBar] = {}
            for b in (b for i in isins for b in by_isin[i] if ex - before <= b[0] <= ex + after):
                if b[0] not in days or b[4] > days[b[0]][4]:
                    days[b[0]] = b
            out[n] = [days[d] for d in sorted(days)]
    return out


def _period(ex: date) -> str:
    return "2011_2021" if ex.year <= 2021 else "2022_on"


def _stored_against_market(
    ctx: Context,
    con: duckdb.DuckDBPyConnection,
    stored: Sequence[StoredAction],
    heads: Mapping[str, tuple[str, int]],
    exceptions: Exceptions,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - real data
    """The bhavcopy's previous close against every stored price action (`bhavcopy_verdict`), and
    each stored split and bonus against the evidence of the open, the volume, the XBRL share count
    and screener's paid-up capital (`evidence_verdict`)."""
    isin_of = dict(ctx.rows("SELECT CAST(id AS TEXT), isin FROM hat_instrument"))
    symbol_of = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
    members: dict[str, set[str]] = defaultdict(set)
    for instrument, (head, _) in heads.items():
        members[head].add(instrument)
    factors, split_bonus, bonus = chain_day_factors(stored, heads)
    factors = {k: f for k, f in factors.items() if ordinary_equity(isin_of.get(k[0], ""))}
    keys = sorted(factors)
    chain = {head: members[head] | {head} for head, _ in keys}
    bars = _raw_bars(
        con, [(ex, {isin_of[m] for m in chain[head] if m in isin_of}) for head, ex in keys]
    )

    counts: Counter[str] = Counter()
    differs = []
    for n, (head, ex) in enumerate(keys):
        found = bhavcopy_factor(bars[n], ex)
        if found is None:
            counts["no_bars"] += 1
            continue
        observed, base, last = found
        stored_factor = float(factors[(head, ex)])
        verdict = bhavcopy_verdict(stored_factor, observed, last)
        counts[f"{verdict}_{_period(ex)}"] += 1
        if verdict == "differs":
            differs.append(
                {
                    "symbol": symbol_of.get(head, ""),
                    "isin": isin_of.get(head, ""),
                    "ex_date": ex.isoformat(),
                    "stored": round(stored_factor, 6),
                    "bhavcopy": round(observed, 6),
                    "bhavcopy_prev_close": base,
                    "last_close": last,
                }
            )
    findings = []
    differs = exceptions.split("bhavcopy_prev_close_differs", differs)
    if differs:
        findings.append(finding("bhavcopy_prev_close_differs", WARN, differs))
    bhavcopy = {
        "source": "the bhavcopy's previous close on the ex-session (raw on nearly every ex-date: "
        "no evidence on a split or bonus factor unless it moved)",
        "actions": len(keys),
        **dict(sorted(counts.items())),
    }

    counts: dict[str, dict[str, list[tuple[date, float, float]]]] = {
        "shares": defaultdict(list),
        "capital": defaultdict(list),
    }
    for instrument, day, paid_up, face in ctx.rows(_SHARES_SQL):
        if paid_up and face:
            counts["shares"][instrument].append((day, float(paid_up), float(face)))
    for instrument, day, value in ctx.rows(_CAPITAL_SQL):
        if value:  # no face value by period: the capital alone, which a split leaves
            counts["capital"][instrument].append((day, float(value), 1.0))
    by_chain: dict[str, dict[str, dict[date, float]]] = {
        "shares": defaultdict(dict),
        "capital": defaultdict(dict),
    }
    # a count's span reaches past the window: its expectation takes the actions around it too
    around = timedelta(days=max(EVIDENCE_PERIOD_DAYS.values()))
    _, near_split_bonus, near_bonus = chain_day_factors(
        _stored(ctx, ctx.start - around, ctx.end + around), heads
    )
    for (head, ex), f in near_split_bonus.items():
        by_chain["shares"][head][ex] = float(f)
    for (head, ex), f in near_bonus.items():
        by_chain["capital"][head][ex] = float(f)
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    listed, disagree, contradicted = [], defaultdict(list), []
    outvoted: Counter[str] = Counter()
    for n, (head, ex) in enumerate(keys):
        if (head, ex) not in split_bonus:
            continue
        stored_factor = float(factors[(head, ex)])
        split = split_bonus[(head, ex)] != bonus.get((head, ex), 1)
        observed: dict[str, float | None] = {
            "open": open_evidence(bars[n], ex),
            "volume": volume_evidence(bars[n], ex),
        }
        # the open takes the whole day's factor (a demerger or rights issue moves the price too);
        # the volume only the splits and bonuses, which alone change the number of shares
        expected = {"open": stored_factor, "volume": float(split_bonus[(head, ex)])}
        slack: dict[str, float] = {}
        for kind, table in counts.items():
            if kind == "capital" and (head, ex) not in bonus:
                continue  # a split leaves the paid-up capital
            found = count_evidence(
                [r for m in chain[head] for r in table[m]],
                ex,
                EVIDENCE_PERIOD_DAYS[kind],
                by_chain[kind][head],
                split=split and kind == "shares",
                rounding=CAPITAL_ROUNDING if kind == "capital" else 0.0,
            )
            if found is not None:
                observed[kind], expected[kind], slack[kind] = found
        verdicts = {
            kind: evidence_verdict(
                expected.get(kind, stored_factor), observed.get(kind), tol + slack.get(kind, 0.0)
            )
            for kind, tol in EVIDENCE_TOLERANCE.items()
        }
        item = {
            "symbol": symbol_of.get(head, ""),
            "isin": isin_of.get(head, ""),
            "ex_date": ex.isoformat(),
            "stored": round(stored_factor, 6),
            **verdicts,
            **{f"{k}_factor": round(v, 4) for k, v in observed.items() if v is not None},
            **{
                f"{k}_expected": round(v, 6)
                for k, v in expected.items()
                if abs(v - stored_factor) > 1e-9
            },
        }
        listed.append(item)
        for kind, verdict in verdicts.items():
            by_year[str(ex.year)][f"{kind}_{verdict}"] += 1
            if verdict != "disagrees":
                continue
            if kind in OUTVOTED and "agrees" in verdicts.values():
                outvoted[kind] += 1
            else:
                disagree[kind].append(item)
        if factor_contradicted(verdicts):
            contradicted.append(item)
    evidence = {
        "actions": len(listed),
        "tolerance_log": EVIDENCE_TOLERANCE,
        "disagrees_outvoted": dict(outvoted),
        "by_year": {y: dict(sorted(c.items())) for y, c in sorted(by_year.items())},
    }
    findings.append(finding("split_bonus_evidence", PASS, listed))
    for kind in EVIDENCE_TOLERANCE:
        left = exceptions.split(f"split_bonus_{kind}_disagrees", disagree[kind])
        if left:
            findings.append(finding(f"split_bonus_{kind}_disagrees", WARN, left))
    contradicted = exceptions.split("split_bonus_factor_contradicted", contradicted)
    if contradicted:
        findings.append(finding("split_bonus_factor_contradicted", FAIL, contradicted))
    return bhavcopy, evidence, findings


def undated_values(events: Iterable[Event], symbol_of: Mapping[str, str]) -> dict[str, Any]:
    """`ca_value_undated`, FAIL: an event the series applies (`point_in_time.series`) whose
    value, its accepted version of highest precedence, has no `available_on`. A backtest holds
    its name factor-pending from the ex-date on (`ca_versions.pending_days`), so it is never
    scanned again: the finding spans the earliest such ex-date on. The owner's correction dates
    it (`correct-actions` with `known_on`); not exceptable."""
    undated = sorted(
        (e.ex_date, f"{symbol_of.get(e.chain_id, e.chain_id)} {e.ex_date} {e.action_type}")
        for e in events
        if (top := select_version(e.versions, None)) is not None and top.available_on is None
    )
    if not undated:
        return finding("ca_value_undated", PASS, [])
    found = finding("ca_value_undated", FAIL, [item for _, item in undated])
    return spanning(found, undated[0][0], date.max)


def _stored(ctx: Context, start: date, end: date) -> list[StoredAction]:  # pragma: no cover
    """The stored corporate actions with an ex-date from `start` to `end`."""
    return [
        StoredAction(str(i), d, t, Fraction(f), exact(c, floats=True), dm, s, x or "")
        for i, d, t, f, c, dm, s, x in ctx.rows(
            "SELECT CAST(instrument_id AS TEXT), ex_date, action_type, adjustment_factor, "
            "cash_amount, demerger, source, raw_text "
            "FROM hat_corporate_action WHERE ex_date BETWEEN :a AND :b",
            a=start,
            b=end,
        )
    ]


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    from ..chains import chains, stored_adjustment, stored_links  # noqa: PLC0415
    from ..point_in_time import series  # noqa: PLC0415

    findings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    members = nifty500_members(ctx)
    announcements: tuple[Mapping, Mapping] = ({}, {})
    coverage = None
    exceptions = Exceptions(read_exceptions(D2_EXCEPTIONS, "D2"))
    stored = _stored(ctx, ctx.start, ctx.end)
    if ctx.ca_data is not None and ctx.ca_data.is_dir():
        refs, skipped, rights_pulled = read_reference(ctx.ca_data)
        refs = [r for r in refs if ctx.start <= r.ex_date <= ctx.end]
        keys_of, held, isin_of = _identities(ctx)
        by_key = _by_key(keys_of)
        deleted = [
            (str(i), d, t)
            for i, d, t in ctx.rows(
                "SELECT instrument_id, ex_date, action_type FROM hat_corporate_action_version "
                "WHERE status = 'deleted'"
            )
        ]
        wanted = [
            (i, r.ex_date)
            for r in refs
            if r.kind in ("rights", "dividend", "demerger")
            for keys in (r.keys, [k for k in r.keys if not _ISIN.match(k)])  # and the ticker's
            for i in ref_instruments(keys, by_key, r.ex_date, held)
        ]
        prev = _prev_closes(ctx, wanted)
        stores = Stores(
            chains(stored_links(ctx.session)),
            isin_of,
            _first_bars(ctx),
            _prev_closes(ctx, wanted, on_day=True),
        )
        starts = reference_starts(refs, ctx.start)
        ends = reference_pulled(p.name for p in (ctx.ca_data / "dividends").glob("ca_*.json"))
        if rights_pulled is None:  # rights would be priced at an unknown face value
            findings.append(finding("reference_pull_date_missing", FAIL, [PULL_NOTE]))
        else:  # a rights action after the pull is not the files' to hold
            ends = {**ends, "rights": rights_pulled}
        factor_summary, factor_findings = compare_factors(
            refs, stored, keys_of, prev, deleted, starts, held, ends, exceptions, stores
        )
        summary["reference_pulled"] = {k: d.isoformat() for k, d in sorted(ends.items())}
        coverage = factor_coverage(starts, ctx.end)
        summary["factors"] = factor_summary
        summary["reference_starts"] = {k: d.isoformat() for k, d in sorted(starts.items())}
        for kind in KINDS:
            if kind not in starts:
                findings.append(finding(f"{kind}_reference_missing", FAIL, [kind]))
        splits, unread = read_cost_splits(_rows([D2_COST_SPLITS]))
        if unread:
            skipped["cost_split"] += unread
        symbol_of = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
        summary["cost_splits"], cost_findings = compare_cost_splits(
            sorted(
                (isin_of[a.instrument_id], symbol_of.get(a.instrument_id, ""), a.ex_date, a.factor)
                for a in stored
                if a.demerger and a.instrument_id in isin_of
            ),
            splits,
        )
        findings += cost_findings
        summary["reference_rows_unread"] = dict(skipped)
        findings += factor_findings
        for kind, n in sorted(skipped.items()):
            findings.append({**finding(f"{kind}_reference_rows_unread", WARN, []), "count": n})
        by_isin, by_symbol, _ = read_announcements(
            sorted(ctx.ca_data.glob("results_announcements_*.csv"))
        )
        announcements = (by_isin, by_symbol)
    else:
        ctx.note("D2: no --ca-data folder; factors unchecked and moves not explained by results")
        findings.append(finding("reference_missing", FAIL, ["ca-data"]))
    with ctx.store.connect(stored_adjustment(ctx.session)) as con:
        heads = chains(stored_links(ctx.session))
        paid = dividends_by_chain(stored, heads)
        summary["moves"], move_findings = _moves(ctx, con, announcements, members, paid, exceptions)
        findings += move_findings
        summary["second_source"], second = _second_source(ctx, con, members, heads, exceptions)
        findings += second
        summary["bhavcopy_prev_close"], summary["split_bonus_evidence"], market = (
            _stored_against_market(ctx, con, stored, heads, exceptions)
        )
        findings += market
    symbols = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
    findings.append(undated_values(series(ctx.session), symbols))
    summary["excepted"] = exceptions.summary()
    result = check_result("D2", summary, [*findings, *exceptions.findings()])
    return result if coverage is None else covered(result, *coverage)


def reference_pulled(names: Iterable[str]) -> dict[str, date]:
    """Where the dividend files' cover ends, from the months they are named for
    (`ca_YYYY-MM.json`, NSE's list by ex-date for the month): the first day of the newest month,
    whose file may be a partial pull (its rows carry no broadcast date to tell); every month
    before it is whole. A dividend stored on or after that day is not judged."""
    months = [m for name in names if (m := re.fullmatch(r"ca_(\d{4})-(\d{2})\.json", name))]
    if not months:
        return {}
    year, month = max((int(m[1]), int(m[2])) for m in months)
    return {"dividend": date(year, month, 1)}


def _identities(
    ctx: Context,
) -> tuple[dict[str, frozenset[str]], Spans, dict[str, str]]:  # pragma: no cover
    """Each instrument's ISINs and symbols (its own, its symbol history's, and its linked
    chain's: an action on an old ISIN is stored on the chain's current one), each symbol's
    holders over time (`symbol_spans`) and each instrument's own ISIN."""
    keys: dict[str, set[str]] = defaultdict(set)
    isin_of: dict[str, str] = {}
    for i, isin, symbol in ctx.rows("SELECT CAST(id AS TEXT), isin, symbol FROM hat_instrument"):
        keys[i] |= {isin.upper(), symbol.upper()}
        isin_of[i] = isin.upper()
    history = [
        (str(i), s.upper(), a, b)
        for i, s, a, b in ctx.rows(
            "SELECT CAST(instrument_id AS TEXT), symbol, valid_from, valid_to "
            "FROM hat_instrument_symbol_history"
        )
    ]
    for i, symbol, _, _ in history:
        keys[i].add(symbol)
    links = ctx.rows(
        "SELECT CAST(old_instrument_id AS TEXT), CAST(new_instrument_id AS TEXT) "
        "FROM hat_isin_lineage WHERE status = 'linked'"
    )
    chain = chains_of(keys, links)
    for members in chain.values():
        union = set().union(*(keys[m] for m in members))
        for m in members:
            keys[m] = union
    return {k: frozenset(v) for k, v in keys.items()}, symbol_spans(history, chain), isin_of


def chains_of(
    instruments: Iterable[str], links: Iterable[tuple[str, str]]
) -> dict[str, frozenset[str]]:
    """Each instrument's linked chain (itself and every instrument a run of links reaches)."""
    parent = {i: i for i in instruments}

    def root(i: str) -> str:
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for old, new in links:
        parent[root(old)] = root(new)
    groups: dict[str, set[str]] = defaultdict(set)
    for i in list(parent):
        groups[root(i)].add(i)
    return {i: frozenset(groups[root(i)]) for i in parent}


def symbol_spans(
    history: Iterable[tuple[str, str, date, date | None]], chain: Mapping[str, frozenset[str]]
) -> dict[str, list[tuple[date, date | None, frozenset[str]]]]:
    """Each symbol's holders: (from, to, the holder's chain) from the symbol history rows
    (instrument, symbol, valid from, valid to; to None while it holds it)."""
    spans: dict[str, list[tuple[date, date | None, frozenset[str]]]] = defaultdict(list)
    for i, symbol, a, b in history:
        spans[symbol].append((a, b, chain.get(i, frozenset({i}))))
    return dict(spans)


def _by_key(keys_of: Mapping[str, frozenset[str]]) -> dict[str, set[str]]:
    by_key: dict[str, set[str]] = defaultdict(set)
    for instrument, keys in keys_of.items():
        for key in keys:
            by_key[key].add(instrument)
    return by_key


_PREV_SQL = """
SELECT CAST(r.instrument_id AS VARCHAR), w.ex_date, arg_max(r.close, r.date)
FROM bars_daily_raw r JOIN wanted w ON CAST(r.instrument_id AS VARCHAR) = w.instrument_id
WHERE r.date < w.ex_date AND r.date >= w.ex_date - CAST(? AS INTEGER) * INTERVAL 1 DAY
GROUP BY ALL
"""


def _first_bars(ctx: Context) -> dict[str, date]:  # pragma: no cover - real data
    with ctx.store.connect() as con:
        return dict(
            con.execute(
                "SELECT CAST(instrument_id AS VARCHAR), min(date) FROM bars_daily_raw GROUP BY ALL"
            ).fetchall()
        )


_DAY_SQL = """
SELECT CAST(r.instrument_id AS VARCHAR), w.ex_date, any_value(r.close)
FROM bars_daily_raw r JOIN wanted w ON CAST(r.instrument_id AS VARCHAR) = w.instrument_id
WHERE r.date = w.ex_date
GROUP BY ALL
"""


def _prev_closes(
    ctx: Context, wanted: Sequence[tuple[str, date]], *, on_day: bool = False
) -> dict[tuple[str, date], Decimal]:  # pragma: no cover - real data
    """The raw close before each (instrument, ex-date) in `PREV_DAYS`, or on it (`on_day`)."""
    import pyarrow as pa  # noqa: PLC0415

    if not wanted:
        return {}
    with ctx.store.connect() as con:
        con.register(
            "wanted",
            pa.table({"instrument_id": [w[0] for w in wanted], "ex_date": [w[1] for w in wanted]}),
        )
        rows = (con.execute(_DAY_SQL) if on_day else con.execute(_PREV_SQL, [PREV_DAYS])).fetchall()
        return {(i, d): close for i, d, c in rows if (close := exact(c, floats=True)) is not None}

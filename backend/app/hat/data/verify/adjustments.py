"""D2: the stored corporate actions and the adjusted series against independent references.

1. Factors (`--ca-data`, NSE's corporate-action feed pulled independently of HAT's importers,
   read here by parsers of its own):
   - splits and bonuses (`splits_bonus_*.csv`): a bonus a:b is b/(a+b), a face-value split old ->
     new is new/old, a combined row both; the product of the stored split and bonus factors of
     the instrument and day must match within `FACTOR_TOLERANCE`;
   - rights (`rights_issues_*.csv`): the theoretical ex-rights factor (N*P + M*S) / ((N+M)*P)
     for M new shares per N held at S = face value + premium, P the raw close before the ex-date
     (1 when S >= P), within `RIGHTS_TOLERANCE` of the stored one;
   - demergers (`demergers_*.csv`): a stored demerger on the day (the factor is the market's gap
     by the owner's rule, so its size is not compared);
   - dividends (`dividends/ca_YYYY-MM.json`): the cash per share of the day's dividends.
   A stored action no reference holds warns; a reference action not stored fails unless the
   owner deleted it (`hat_corporate_action_version`, status `deleted`), which warns. Non-equity
   bonuses (preference, debentures, warrants) are not price actions and are skipped.
2. Moves: the price-basis series (`bars_daily_cap`) scanned for close-to-close moves over
   `audit.JUMP_PCT` between adjacent sessions (`audit.jumps`). A move on an ex-date (the factor
   changed and the move stays) fails; a move shaped like a split or bonus ratio
   (`audit.action_shaped`) with no factor fails (an action missing); a move within
   `RESULTS_DAYS` of a results announcement is explained; the rest warn, listed.
3. Second source (with `--fetch`, else what is kept): `SAMPLE_SIZE` NIFTY 500 stocks, seeded,
   each calendar year's total return on `bars_daily` (adjusted) against Yahoo Finance's
   adjusted close (monthly bars, split- and dividend-adjusted; no login), kept under
   `raw/reference/yahoo/`. Yahoo's known limits: its own dividend method and occasional missed
   or misdated actions, no demerger adjustment, history under today's symbol only, and it
   rate-limits (HTTP 429) a client that asks often. A year off by more than
   `SECOND_SOURCE_PP` percentage points warns.
Ex-date moves at a price band (`CIRCUIT_BANDS`) are the market's and moves from a raw close
under `PENNY` are ticks: both are counted, not listed.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...money import exact
from ..audit import FAIL, WARN, action_shaped, check_result, finding, jumps
from . import seeded_sample
from .context import Context
from .filings import Announcement, read_announcements
from .references import reference_file

if TYPE_CHECKING:
    import duckdb

FACTOR_TOLERANCE = Fraction(1, 1000)  # relative; HAT's MCP bonus factors are rounded to 4 places
RIGHTS_TOLERANCE = Fraction(5, 1000)  # P is one close; NSE's and HAT's may round it differently
RESULTS_DAYS = 3  # a results announcement up to this many days before a move explains it
SAMPLE_SIZE = 100
SAMPLE_SEED = 20261029
SECOND_SOURCE_PP = 2.0
CIRCUIT_BANDS = (5.0, 10.0, 20.0)  # NSE's price bands, percent
CIRCUIT_SLACK = 0.25  # percentage points: a band's move after the tick rounding
PENNY = 5.0  # rupees: below it a tick of 0.05 (0.01 since 2023) is 1% or more
NON_EQUITY = frozenset({"preference", "debentures", "warrants"})
PRICE_TYPES = ("split", "bonus")
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
    "?period1={start}&period2={end}&interval=1mo&events=div%2Csplit"
)
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


def split_factor(subject: str, fv_change: str = "") -> Fraction | None:
    """A face-value split old -> new: prices scale by new / old (from `10->1`, else the
    subject's `Rs 10/- ... To Rs 2/-`)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*->\s*([\d.]+)\s*", fv_change or "")
    found = (m[1], m[2]) if m else None
    if found is None:
        m = _FV.search((subject or "").lower())
        found = (m[1], m[2]) if m else None
    if found is None:
        return None
    try:
        old, new = Fraction(found[0].rstrip(".")), Fraction(found[1].rstrip("."))
    except (ValueError, ZeroDivisionError):
        return None
    return new / old if old and new and new != old else None


_AMOUNT = re.compile(
    r"(?:rs|re|inr)\.?\s*-?\s*(\d+(?:\s*\.+\d+)?)"  # NSE's `Re 0..30` and `Rs 0 .70` too
    r"|div[a-z]*\s*-?\s*(\d+(?:\.\d+)?)\s*per"  # `Special Dividend 243 Per Share`
)
_NOT_PAYOUT = re.compile(
    r"\(including[^)]*\)?"  # `(Including Special Dividend Of Rs.2/-)`: already in the total
    r"|(?:equity\s+)?shares?\s+of\s+(?:rs|re)\.?\s*[\d.]+"  # `Per Share Of Rs.10/- Each`
    r"|(?:face\s+value|split|bonus|right).*"  # another action joined with `+`
)
_PURPOSES = re.compile(r"/(?!-)")  # `/` parts purposes; `Rs 3/-` is an amount
_DIV = re.compile(r"(?<!sub-)(?<!sub )\bdiv")  # not `Sub-Division`
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


_PREMIUM = re.compile(r"prem[a-z]*[.\s]*(?:of\s+)?(?:(?:rs|re|inr)[.\s]*)?(\d+(?:\.\d+)?)")
_PRICE = re.compile(r"@\s*(?:rs|re|inr)\.?\s*(\d+(?:\.\d+)?)")
FACE_VALUES = (Decimal(1), Decimal(2), Decimal(5), Decimal(10))


def issue_prices(subject: str, premium: str, face_value: str) -> list[Decimal]:
    """A rights issue's price per share, the reference's reading first: face value plus the
    premium (the subject's, else the parsed column's), or the subject's `@ Rs X`; after it the
    premium on the other usual face values, as the reference's face value is today's, not the
    issue day's. Empty when the reference states no price."""
    text = (subject or "").lower()
    m = _PREMIUM.search(text)
    prem = exact(m[1] if m else premium)
    fv = exact(face_value)
    if prem is None:
        m = _PRICE.search(text)
        price = exact(m[1]) if m else None
        return [] if price is None else [price]
    first = [fv + prem] if fv is not None else []
    return first + [f + prem for f in FACE_VALUES if f != fv]


def rights_factor(new: int, held: int, paid: Decimal, prev_close: Decimal) -> Fraction:
    """The theoretical ex-rights factor of `new` shares per `held` at `paid` each, against the
    close before the ex-date; 1 when the issue is not below that close."""
    p, s = Fraction(prev_close), Fraction(paid)
    if s >= p:
        return Fraction(1)
    return (held * p + new * s) / ((held + new) * p)


def close_enough(stored: Fraction, expected: Fraction, tolerance: Fraction) -> bool:
    return abs(stored / expected - 1) <= tolerance


def judge_rights(
    stored: Fraction, new: int, held: int, prices: Sequence[Decimal], prev_close: Decimal
) -> tuple[str, Fraction]:
    """`equal` (at the reference's own price), `equal_other_face_value` (at the premium on
    another face value), `not_below_market` (no price below the close: no factor due) or
    `factor_mismatch`, with the expected factor at the reference's own price."""
    expected = rights_factor(new, held, prices[0], prev_close)
    if close_enough(stored, expected, RIGHTS_TOLERANCE):
        return "equal", expected
    if any(
        close_enough(stored, rights_factor(new, held, p, prev_close), RIGHTS_TOLERANCE)
        for p in prices[1:]
    ):
        return "equal_other_face_value", expected
    return "factor_mismatch", expected


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
    prices: tuple[Decimal, ...] = ()  # rights: `issue_prices`
    text: str = ""
    equity: bool = True  # False: a class nobody checked (a plain bonus without evidence)


@dataclass(frozen=True)
class StoredAction:
    instrument_id: str
    ex_date: date
    action_type: str
    factor: Fraction
    cash: Decimal | None
    demerger: bool
    source: str


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
    if day is None or row.get("instrument_class") in NON_EQUITY:
        return None
    factor = Fraction(1)
    for leg in (
        bonus_factor(row.get("ratio_parsed", "")) if "bonus" in kind else Fraction(1),
        split_factor(row.get("subject_raw", ""), row.get("fv_change", ""))
        if "split" in kind
        else Fraction(1),
    ):
        if leg is None:
            return None
        factor *= leg
    equity = row.get("instrument_class") == "equity" or kind != "bonus"
    return RefAction(
        "split_bonus", day, _keys(row), factor, text=row.get("subject_raw", ""), equity=equity
    )


def _rights(row: Mapping[str, str]) -> RefAction | None:
    day = ref_date(row.get("exDate", ""))
    m = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", row.get("entitlement_ratio_parsed", ""))
    if day is None or not m:
        return None
    prices = issue_prices(
        row.get("subject_raw", ""), row.get("premium_rs_parsed", ""), row.get("faceVal", "")
    )
    return RefAction(
        "rights",
        day,
        _keys(row),
        entitlement=(int(m[1]), int(m[2])),
        prices=tuple(prices),
        text=row.get("subject_raw", ""),
    )


def combine_legs(legs: Sequence[RefAction]) -> RefAction:
    """A day's split and bonus rows of one instrument as one action: their factors' product
    (HAT stores each leg, judged together)."""
    factor = Fraction(1)
    for leg in legs:
        factor *= leg.factor or 1
    return RefAction(
        "split_bonus",
        legs[0].ex_date,
        legs[0].keys,
        factor,
        text=" + ".join(leg.text[:60] for leg in legs),
        equity=all(leg.equity for leg in legs),
    )


def read_reference(
    folder: Path,
) -> tuple[list[RefAction], Counter[str]]:  # pragma: no cover - files
    """Every reference action of `folder` and the rows that could not be read, by kind."""
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
    for row in _rows(paths):
        action = _rights(row)
        if action is None:
            skipped["rights"] += 1
        else:
            out.append(action)
    for row in _rows(sorted(folder.glob("demergers_*.csv"))):
        day = ref_date(row.get("exDate", ""))
        if day is None:
            skipped["demerger"] += 1
            continue
        out.append(RefAction("demerger", day, _keys(row), text=row.get("ratio", "")))
    amounts: dict[tuple[frozenset[str], date], list[Decimal]] = defaultdict(list)
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
            amount = dividend_cash(subject)
            if day is None or amount is None:
                skipped["dividend"] += 1
                continue
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
            cash=sum(rows, Decimal(0)),
            amounts=tuple(rows),
            text=" | ".join(texts[(keys, day)]),
        )
        for (keys, day), rows in amounts.items()
    ]
    return out, skipped


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
    "not_stored_class_unknown": WARN,
    "demerger_not_stored": WARN,
    "deleted_by_owner": WARN,
    "stored_not_in_reference": WARN,
    "no_prev_close": WARN,
    "no_issue_price": WARN,
}


def _kind(action: StoredAction) -> str:
    if action.action_type in PRICE_TYPES:
        return "split_bonus"
    if action.action_type == "dividend":
        return "dividend"
    return "demerger" if action.demerger else "rights"


def judge_ref(
    ref: RefAction, per_instrument: Mapping[str, Sequence[StoredAction]], prev_close: Decimal | None
) -> tuple[str, dict[str, Any]]:
    """One reference action against the stored actions of its kind on its day, per instrument
    of its chain (an action stored on both an old and a new ISIN is applied once per chain, so
    the instrument whose actions agree best is judged): its code and evidence."""
    if ref.kind == "demerger":
        first = next(iter(per_instrument.values()))[0]
        return "present", {"factor": str(float(first.factor))}
    if ref.kind == "dividend":
        assert ref.cash is not None
        totals = [
            sum((a.cash or Decimal(0) for a in acts), Decimal(0))
            for acts in per_instrument.values()
        ]
        best = min(totals, key=lambda t: abs(t - ref.cash))  # type: ignore[operator]
        code = (
            "equal"
            if best == ref.cash
            else "equal_one_row"
            if len(ref.amounts) > 1 and best in ref.amounts
            else "cash_mismatch"
        )
        return code, {"stored": str(best), "expected": str(ref.cash)}
    products = []
    for acts in per_instrument.values():
        product = Fraction(1)
        for a in acts:
            product *= a.factor
        products.append(product)
    if ref.kind == "split_bonus":
        assert ref.factor is not None
        n = min(range(len(products)), key=lambda k: abs(products[k] / ref.factor - 1))  # type: ignore[operator]
        best = products[n]
        code = "equal" if close_enough(best, ref.factor, FACTOR_TOLERANCE) else "factor_mismatch"
        sources = sorted({a.source for a in list(per_instrument.values())[n]})
        return code, {
            "stored": str(float(best)),
            "expected": str(float(ref.factor)),
            "source": ",".join(sources),
        }
    assert ref.entitlement is not None
    if not ref.prices:
        return "no_issue_price", {"stored": str(float(products[0]))}
    if prev_close is None:
        return "no_prev_close", {"stored": str(float(products[0]))}
    judged = [judge_rights(p, *ref.entitlement, ref.prices, prev_close) for p in products]
    order = ("equal", "equal_other_face_value", "factor_mismatch")
    n, (code, expected) = min(enumerate(judged), key=lambda j: order.index(j[1][0]))
    return code, {
        "stored": str(float(products[n])),
        "expected": str(float(expected)),
        "prev_close": str(prev_close),
        "issue_price": str(ref.prices[0]),
    }


def compare_factors(
    refs: Sequence[RefAction],
    stored: Sequence[StoredAction],
    keys_of: Mapping[str, frozenset[str]],
    prev_close: Mapping[tuple[str, date], Decimal],
    deleted: Iterable[tuple[str, date, str]],
    reference_from: date,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The reference actions against the stored ones (each stored action's instrument known by
    `keys_of[instrument_id]`: its ISINs and symbols), and the stored actions from
    `reference_from` that no reference holds, by kind: the counts and the findings."""
    by_key: dict[str, set[str]] = defaultdict(set)
    for instrument, keys in keys_of.items():
        for key in keys:
            by_key[key].add(instrument)
    index: dict[tuple[str, date], list[StoredAction]] = defaultdict(list)
    for action in stored:
        index[(action.instrument_id, action.ex_date)].append(action)
    gone = set(deleted)
    counts: Counter[str] = Counter()
    years: dict[str, Counter[int]] = defaultdict(Counter)
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    used: set[StoredAction] = set()

    def note(code: str, ref: RefAction, **extra: object) -> None:
        counts[f"{ref.kind}:{code}"] += 1
        years[f"{ref.kind}:{code}"][ref.ex_date.year] += 1
        items[f"{ref.kind}:{code}"].append(
            {"ex_date": ref.ex_date.isoformat(), "keys": sorted(ref.keys), "text": ref.text[:80]}
            | extra
        )

    for ref in refs:
        ids = sorted(set().union(*(by_key.get(k, set()) for k in ref.keys)))
        if not ids:
            note("no_instrument", ref)
            continue
        per_instrument = {
            i: acts
            for i in ids
            if (acts := [a for a in index.get((i, ref.ex_date), ()) if _kind(a) == ref.kind])
        }
        for acts in per_instrument.values():
            used.update(acts)
        close = next(
            (prev_close[(i, ref.ex_date)] for i in ids if (i, ref.ex_date) in prev_close), None
        )
        if per_instrument:
            code, detail = judge_ref(ref, per_instrument, close)
            note(code, ref, **detail)
        elif any((i, ref.ex_date, t) in gone for i in ids for t in WANTED[ref.kind]):
            note("deleted_by_owner", ref)
        elif (
            ref.kind == "rights"
            and ref.prices
            and close is not None
            and all(
                rights_factor(*ref.entitlement, p, close) == 1
                for p in ref.prices  # type: ignore[misc]
            )
        ):
            note("not_below_market", ref)
        elif ref.kind == "demerger":
            note("demerger_not_stored", ref)  # the owner's rule stores only a demerger that gaps
        else:
            note("not_stored" if ref.equity else "not_stored_class_unknown", ref)
    for a in stored:
        if (
            a.ex_date < reference_from
            or a in used
            or a.action_type not in {*PRICE_TYPES, "other", "dividend"}
        ):
            continue
        code = f"{_kind(a)}:stored_not_in_reference"
        counts[code] += 1
        years[code][a.ex_date.year] += 1
        items[code].append(
            {
                "instrument_id": a.instrument_id,
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
            by_year = dict(sorted(years[code].items()))
            findings.append(
                {**finding(name, level, items[code]), "count": counts[code], "by_year": by_year}
            )
    return {k: counts[k] for k in sorted(counts)}, findings


# --- the move scan -----------------------------------------------------------------------------


def explained_by_results(
    day: date, announcements: Sequence[Announcement], within: int = RESULTS_DAYS
) -> bool:
    """Whether a results announcement fell in the `within` days before `day` (or on it)."""
    first = day - timedelta(days=within)
    return any(first <= a.announced.date() <= day for a in announcements)


def classify_move(move: Mapping[str, Any], explained: bool, prev_raw_close: float) -> str:
    """One adjusted move beyond the threshold (`audit.jumps`): `ex_date_jump` (`ex_date_circuit`
    when the move is a price band's: the market's, on an ex-date), `penny` (from a
    raw close under `PENNY`, where a tick is a large move), `action_shaped`, `results` or
    `unexplained`."""
    if move["ex_date"]:
        circuit = any(abs(abs(move["move_pct"]) - band) <= CIRCUIT_SLACK for band in CIRCUIT_BANDS)
        return "ex_date_circuit" if circuit else "ex_date_jump"
    if prev_raw_close < PENNY:
        return "penny"
    if action_shaped(move["raw_move_pct"]):
        return "action_shaped"
    return "results" if explained else "unexplained"


_YEAR_SQL = """
SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date, isin, symbol,
       CAST(close AS DOUBLE) AS close, CAST(raw_close AS DOUBLE) AS raw_close
FROM bars_daily_cap WHERE date BETWEEN ? AND ?
"""


def _moves(
    ctx: Context,
    con: duckdb.DuckDBPyConnection,
    announcements: tuple[Mapping, Mapping],
    members: Mapping[str, Sequence[tuple[date, date | None]]],
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
        frame = frame.sort("instrument_id", "date")
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
            for move in found:
                day = date.fromisoformat(move["date"])
                if not first <= day <= last:
                    continue
                at = days.index(day)
                said = [*by_isin.get(isins[at], ()), *by_symbol.get(symbols[at], ())]
                code = classify_move(move, explained_by_results(day, said), raws[at - 1] or 0.0)
                member = any(
                    a <= day and (b is None or day < b) for a, b in members.get(str(instrument), ())
                )
                counts[code] += 1
                years[str(year)][code] += 1
                if member:
                    counts[f"{code}_nifty500"] += 1
                items[code].append({**move, "symbol": symbols[at], "nifty500": member})
    for code in items:  # members first, then the largest moves
        items[code].sort(key=lambda m: (not m["nifty500"], -abs(m["move_pct"])))
    findings = []
    for code, level in (("ex_date_jump", FAIL), ("action_shaped", FAIL), ("unexplained", WARN)):
        if counts[code]:
            findings.append(
                {
                    **finding(f"move_{code}", level, items[code]),
                    "count": counts[code],
                    "nifty500": counts[f"{code}_nifty500"],
                }
            )
    return {
        "moves_over_pct": dict(counts),
        "by_year": {k: dict(v) for k, v in sorted(years.items())},
    }, findings


# --- the second source --------------------------------------------------------------------------


def yahoo_year_returns(data: bytes) -> dict[int, float]:
    """Each calendar year's return from a Yahoo chart answer's monthly adjusted closes (the
    last month's over the year before's last month)."""
    doc = json.loads(data)
    result = ((doc.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return {}
    stamps = result.get("timestamp") or []
    offset = int((result.get("meta") or {}).get("gmtoffset") or 0)  # bars start at local midnight
    adj = (((result.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
    last: dict[int, float] = {}
    for stamp, value in zip(stamps, adj, strict=False):
        if value is not None:
            last[datetime.fromtimestamp(stamp + offset, UTC).year] = float(
                value
            )  # months are in order
    return {y: 100 * (last[y] / last[y - 1] - 1) for y in sorted(last) if y - 1 in last}


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
            "yahoo_pct": round(theirs[y], 2),
            "gap_pp": round(ours[y] - theirs[y], 2),
        }
        for y in sorted(set(ours) & set(theirs))
        if first_year <= y <= last_year
    ]


_TR_SQL = """
SELECT date, CAST(close AS DOUBLE) FROM bars_daily
WHERE instrument_id = ? AND date BETWEEN ? AND ? ORDER BY date
"""


def _second_source(
    ctx: Context,
    con: duckdb.DuckDBPyConnection,
    members: Mapping[str, Sequence[tuple[date, date | None]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - the network  # noqa: ANN401
    symbols = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
    pool = sorted(i for i in members if i in symbols)
    sample = seeded_sample(pool, SAMPLE_SIZE, SAMPLE_SEED)
    first_year, last_year = ctx.start.year + 1, ctx.end.year - 1  # whole years only
    start = int(datetime(ctx.start.year - 1, 12, 1, tzinfo=UTC).timestamp())
    end = int(datetime(ctx.end.year, 1, 31, tzinfo=UTC).timestamp())
    compared, off, missing = 0, [], []
    gaps: list[float] = []
    for instrument in sample:
        symbol = symbols[instrument]
        data = reference_file(
            ctx,
            "yahoo",
            f"{symbol}_{ctx.start.year}_{ctx.end.year}.json",
            YAHOO_URL.format(symbol=symbol.replace("&", "%26"), start=start, end=end),
            valid=lambda d: b'"result":[' in d,
        )
        theirs = yahoo_year_returns(data) if data else {}
        if not theirs:
            missing.append(symbol)
            continue
        points = con.execute(
            _TR_SQL, [instrument, date(ctx.start.year - 1, 1, 1), ctx.end]
        ).fetchall()
        rows = compare_returns(year_returns(points), theirs, first_year, last_year)
        compared += len(rows)
        gaps += [abs(r["gap_pp"]) for r in rows]
        off += [{"symbol": symbol, **r} for r in rows if abs(r["gap_pp"]) > SECOND_SOURCE_PP]
    off.sort(key=lambda r: -abs(r["gap_pp"]))
    gaps.sort()
    summary = {
        "source": "Yahoo Finance chart API, monthly adjusted close (no login)",
        "sample": len(sample),
        "stock_years_compared": compared,
        "median_gap_pp": round(gaps[len(gaps) // 2], 3) if gaps else None,
        "p90_gap_pp": round(gaps[int(len(gaps) * 0.9)], 3) if gaps else None,
        "within_pp": SECOND_SOURCE_PP,
        "years_off": len(off),
        "symbols_without_answer": len(missing),
    }
    findings = []
    if off:
        findings.append(finding("second_source_year_off", WARN, off))
    if missing:
        findings.append(finding("second_source_missing", WARN, missing))
    return summary, findings


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    from ..chains import stored_adjustment  # noqa: PLC0415

    findings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    members: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for i, a, b in ctx.rows(
        "SELECT CAST(instrument_id AS TEXT), effective_from, effective_to FROM hat_index_member "
        "WHERE index_name = 'NIFTY 500'"
    ):
        members[i].append((a, b))
    announcements: tuple[Mapping, Mapping] = ({}, {})
    if ctx.ca_data is not None and ctx.ca_data.is_dir():
        refs, skipped = read_reference(ctx.ca_data)
        refs = [r for r in refs if ctx.start <= r.ex_date <= ctx.end]
        stored = [
            StoredAction(str(i), d, t, Fraction(f), exact(c, floats=True), dm, s)
            for i, d, t, f, c, dm, s in ctx.rows(
                "SELECT CAST(instrument_id AS TEXT), ex_date, action_type, adjustment_factor, "
                "cash_amount, demerger, source "
                "FROM hat_corporate_action WHERE ex_date BETWEEN :a AND :b",
                a=ctx.start,
                b=ctx.end,
            )
        ]
        keys_of = _identities(ctx)
        deleted = [
            (str(i), d, t)
            for i, d, t in ctx.rows(
                "SELECT instrument_id, ex_date, action_type FROM hat_corporate_action_version "
                "WHERE status = 'deleted'"
            )
        ]
        prev = _prev_closes(
            ctx, [(i, r.ex_date) for r in refs if r.kind == "rights" for i in _ids(keys_of, r)]
        )
        reference_from = min((r.ex_date for r in refs if r.kind != "dividend"), default=ctx.start)
        factor_summary, factor_findings = compare_factors(
            refs, stored, keys_of, prev, deleted, reference_from
        )
        summary["factors"] = factor_summary
        summary["reference_rows_unread"] = dict(skipped)
        findings += factor_findings
        by_isin, by_symbol, _ = read_announcements(
            sorted(ctx.ca_data.glob("results_announcements_*.csv"))
        )
        announcements = (by_isin, by_symbol)
    else:
        ctx.note("D2: no --ca-data folder; factors unchecked and moves not explained by results")
        findings.append(finding("reference_missing", WARN, ["ca-data"]))
    with ctx.store.connect(stored_adjustment(ctx.session)) as con:
        summary["moves"], move_findings = _moves(ctx, con, announcements, members)
        findings += move_findings
        summary["second_source"], second = _second_source(ctx, con, members)
        findings += second
    return check_result("D2", summary, findings)


def _identities(ctx: Context) -> dict[str, frozenset[str]]:  # pragma: no cover - the database
    """Each instrument's ISINs and symbols: its own, its symbol history's, and its linked
    chain's (an action on an old ISIN is stored on the chain's current one)."""
    keys: dict[str, set[str]] = defaultdict(set)
    for i, isin, symbol in ctx.rows("SELECT CAST(id AS TEXT), isin, symbol FROM hat_instrument"):
        keys[i] |= {isin.upper(), symbol.upper()}
    for i, symbol in ctx.rows(
        "SELECT CAST(instrument_id AS TEXT), symbol FROM hat_instrument_symbol_history"
    ):
        keys[i].add(symbol.upper())
    links = ctx.rows(
        "SELECT CAST(old_instrument_id AS TEXT), CAST(new_instrument_id AS TEXT) "
        "FROM hat_isin_lineage WHERE status = 'linked'"
    )
    for _ in range(3):  # chains are short; three passes spread the keys along every chain
        for old, new in links:
            union = keys[old] | keys[new]
            keys[old], keys[new] = set(union), set(union)
    return {k: frozenset(v) for k, v in keys.items()}


def _ids(keys_of: Mapping[str, frozenset[str]], ref: RefAction) -> list[str]:
    return [i for i, keys in keys_of.items() if keys & ref.keys]


_PREV_SQL = """
SELECT CAST(r.instrument_id AS VARCHAR), w.ex_date, arg_max(r.close, r.date)
FROM bars_daily_raw r JOIN wanted w ON CAST(r.instrument_id AS VARCHAR) = w.instrument_id
WHERE r.date < w.ex_date AND r.date >= w.ex_date - INTERVAL 20 DAY
GROUP BY ALL
"""


def _prev_closes(
    ctx: Context, wanted: Sequence[tuple[str, date]]
) -> dict[tuple[str, date], Decimal]:  # pragma: no cover - real data
    import pyarrow as pa  # noqa: PLC0415

    if not wanted:
        return {}
    with ctx.store.connect() as con:
        con.register(
            "wanted",
            pa.table({"instrument_id": [w[0] for w in wanted], "ex_date": [w[1] for w in wanted]}),
        )
        return {
            (i, d): close
            for i, d, c in con.execute(_PREV_SQL).fetchall()
            if (close := exact(c, floats=True)) is not None
        }

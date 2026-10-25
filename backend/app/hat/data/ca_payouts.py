"""Dividends from NSE's corporate-action lists (CA3, CA4): the purpose-text parser, the checks
against the raw bars, the verdicts and the journalled write of the nightly `bc` import
(`ca_nightly`) and the one-off feed import (`ca_dividends`). A dividend is a price-neutral
`dividend` row (factor 1) with the cash per share in `cash_amount`.

A purpose text names one or more dividends; each is a `Payout` of a kind (interim, final,
special) and rupees per share (`payouts`). A percentage of the face value, a preference
dividend and a text with no single amount are never guessed: review or skipped. The revision
rows of one (ISIN, ex-date) are combined (`combine`): identical ones once, different kinds
added, a disagreement on one kind to review unless the revised rows settle it.

A row whose symbol and ISIN name different companies goes to review (`symbol_isin_disagree`),
never stored by its ISIN alone. The last close before the ex-date (within `ca_gate.BAR_DAYS`)
must exist and the cash may be at most `MAX_YIELD` of it, else review; the row goes on the
instrument that holds that bar, one row per (instrument, ex-date, `dividend`). Against what is
stored for the company (any ISIN of its chain): the same amount is `already`, a stored dividend
without one gets it (`stored`, `amount_added`), another amount is a `conflict`, never
rewritten, and a key the owner vacated is never re-created. `apply` journals each row as
`corporate_action.imported`.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatCorporateAction
from ..money import require_exact
from .ca_apply import journal_imported, store_new
from .ca_bc import SME_SERIES, SOURCE_BC, FeedRow
from .ca_gate import (
    ALREADY,
    CONFLICT,
    REVIEW,
    SKIPPED,
    STORED,
    TEXT_MAX,
    Bar,
    CaFileError,
    Reference,
    Verdict,
    fmt4,
    gate,
    load_reference,
    read_windows,
)
from .ca_known import RESEARCH_SOURCE_DATE, UNKNOWN, at_ex_open, from_sources, latest
from .ca_versions import Ledger
from .corporate_actions import TYPE_DIVIDEND, ActionRecord, owner_vacated
from .isin import ordinary_equity
from .store import DATASET_BARS, DataStore

SOURCE_FEED = "nse_ca_feed"
SOURCE_RESEARCH = "ca_research"
T_FEED = "feed"
T_BONUS = "bonus_research"
T_NIGHTLY = "nightly"
TABLES = (T_FEED, T_BONUS, T_NIGHTLY)
SOURCES = {T_FEED: SOURCE_FEED, T_BONUS: SOURCE_RESEARCH, T_NIGHTLY: SOURCE_BC}
FROM_DATE = date(2011, 1, 1)
# A cash amount above half the previous close is not a dividend the bars can carry (the largest
# real ones, HINDZINC's and VEDL's specials, are under a fifth): it is a misparse or a wrong row.
MAX_YIELD = Decimal("0.5")
# A bonus of debentures or preference shares is cash to a holder only from this share of the
# previous close on (the owner's rule, CA3): below it the distribution stays price-neutral, as D1
# keeps it, because its ex-date gap is lost in a day's noise and the bars cannot place it.
BONUS_MIN_SHARE = Decimal("0.01")
# InvIT and REIT units and Indian depository receipts: their distributions are not dividends
NON_EQUITY_SERIES = frozenset({"IV", "RR", "DR"})
KIND_INTERIM = "interim"
KIND_FINAL = "final"
KIND_SPECIAL = "special"
KIND_BONUS = "bonus_instrument"
REASON_AMOUNT_ADDED = "amount_added"
# the row's symbol and ISIN name different companies (`Reference.disagrees`): to review
REASON_SYMBOL_ISIN = "symbol_isin_disagree"


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
_KIND = re.compile(r"\b(final|special|interim|interm)\b")
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


# NSE's typos of "interim" in the words before "Dividend": "Interm", "Int", "Interest Dividend"
_INTERIM = re.compile(r"interim|\binterm\b|\bint\b|\binterest\s*$")


def _kind(words: str) -> str:
    if "special" in words:
        return KIND_SPECIAL
    if _INTERIM.search(words.strip()):
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


# a figure in thousands groups, western (1,000,000) or Indian (10,00,000): its commas go
_GROUPED = re.compile(r"(?<![\d.,])\d{1,3}(?:(?:,\d{3})+|(?:,\d{2})+,\d{3})(?![\d,])")
_DIGIT_COMMA = re.compile(r"\d,\d")


def _prefix(lowered: str, found: Sequence[re.Match[str]], n: int) -> str:
    """The words of the n-th "dividend" mention's clause before it."""
    before = lowered[found[n - 1].end() if n else 0 : found[n].start()]
    seps = list(_SEP.finditer(before))
    return before[seps[-1].end() :] if seps else before


def _lowered(text: str) -> str | None:
    """The text in lower case with single spaces and grouped figures without their commas
    ("Rs 1,000" is 1000); None when a comma still sits between digits ("Rs 2,50"): a figure
    that cannot be read is never read as its first digits."""
    lowered = _GROUPED.sub(lambda m: m.group().replace(",", ""), re.sub(r"\s+", " ", text.lower()))
    return None if _DIGIT_COMMA.search(lowered) else lowered


def named_kinds(text: str) -> set[str]:
    """The kinds of the dividends a purpose text mentions, with an amount or not."""
    lowered = re.sub(r"\s+", " ", text.lower())
    found = list(_DIVIDEND.finditer(lowered))
    return {_kind(_prefix(lowered, found, n)) for n in range(len(found))}


def payouts(text: str) -> tuple[Payout, ...] | str:
    """The dividends a purpose text names, or why it gives none it can trust: `no_dividend`,
    `preference_dividend` (only dividends on preference shares), `percent_of_face_value`,
    `several_amounts` (one clause with amounts not each named by its kind), `bad_figure` (a
    comma inside a figure that is no thousands grouping) or `no_amount`. One clause the text
    cannot be read for is enough: a partial sum is never returned."""
    lowered = _lowered(text)
    if lowered is None:
        return "bad_figure"
    found = list(_DIVIDEND.finditer(lowered))
    if not found:
        return "no_dividend"
    out: list[Payout] = []
    inside: list[Payout] = []  # named in brackets: a breakdown of the amount before them
    preference = False
    for n, match in enumerate(found):
        stop = found[n + 1].start() if n + 1 < len(found) else len(lowered)
        prefix = _prefix(lowered, found, n)
        parts = _clause(prefix, lowered[match.end() : stop])
        if parts == [] and n == 0:
            # "Rs 50/- Per Share (Special Dividend)": the label of the one amount before it
            amounts = _MONEY.findall(lowered[: match.start()])
            if len(amounts) != 1:
                return "several_amounts" if amounts else "no_amount"
            amount = require_exact(amounts[0], "amount")
            if amount <= 0:
                return "no_amount"
            parts = [Payout(_kind(prefix), amount)]
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
    """An equity share's ISIN as the feed gives it (`isin.ordinary_equity`: not an ETF unit, a
    rights entitlement or a debt security), or none (matched by symbol)."""
    return not isin or ordinary_equity(isin)


def total(parts: Iterable[Payout]) -> Decimal:
    return sum((p.amount for p in parts), Decimal(0))


def combine(rows: Sequence[FeedRow]) -> tuple[Payout, ...] | str:
    """The payouts of one (ISIN, ex-date)'s feed rows: identical rows count once, rows naming
    different kinds add up, and rows that give one kind different amounts are settled by the
    revised rows when those agree ("(Purpose Revised)"), else `rows_disagree`. A row that cannot
    be read makes the event unreadable (its reason), unless another row of it reads and the
    unreadable one names no amount at all."""
    read: dict[str, tuple[Payout, ...]] = {}
    failed: list[str] = []
    unpriced: set[str] = set()  # the kinds the rows without an amount name
    for row in rows:
        found = payouts(row.subject)
        if isinstance(found, str):
            failed.append(found)
            if found == "no_amount":
                unpriced |= named_kinds(row.subject)
        else:
            read[row.subject] = tuple(sorted(found))
    if unpriced - {p.kind for parts in read.values() for p in parts} and read:
        return "no_amount"  # a kind no row gives an amount for: never a partial sum
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
    source_day: date | None = None  # a research row's `source_date`
    symbols: tuple[str, ...] = ()  # every symbol a feed group's rows give (else `symbol`)

    def ends(self, status: str, reason: str) -> Proposal:
        return replace(self, status=status, reason=reason)


def feed_proposals(rows: Iterable[FeedRow], table: str = T_FEED) -> list[Proposal]:
    """One proposal per (ISIN or symbol, ex-date) of the feed's (or the nightly file's) dividend
    rows, with every symbol its rows give (`decide` checks each against the ISIN)."""
    groups: dict[tuple[str, date | None], list[FeedRow]] = defaultdict(list)
    for row in rows:
        if _DIVIDEND.search(row.subject.lower()):
            groups[(row.isin or row.symbol, row.ex_date)].append(row)
    out = []
    for (key, ex_date), members in sorted(groups.items(), key=lambda g: (str(g[0][1]), g[0][0])):
        first = members[0]
        subjects = list(dict.fromkeys(m.subject for m in members))
        symbols = tuple(dict.fromkeys(m.symbol for m in members))
        text = " | ".join(subjects)
        p = Proposal(
            table, key, first.symbol, first.isin, ex_date, text, hint=first.series, symbols=symbols
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
PREVIOUS_SLACK = 7  # days back from the earliest candidate to the session before it


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
    a symbol and ISIN naming different companies (`symbol_isin_disagree`, review), no instrument
    (skipped: no bars anywhere), no bar, an amount over `MAX_YIELD`, a bonus
    distribution under `BONUS_MIN_SHARE` of the close (skipped: it stays price-neutral) or one
    whose ex-date is not settled (`bonus_ex_date`)."""
    if p.ex_date is None:
        return Outcome(p, p.status or REVIEW, p.reason or "bad_ex_date")
    # never stored by the ISIN alone, and named over another review reason (the owner's first fix)
    disagree = any(reference.disagrees(s, p.isin, p.ex_date) for s in p.symbols or (p.symbol,))
    if p.status:
        reason = REASON_SYMBOL_ISIN if disagree and p.status == REVIEW else p.reason
        return Outcome(p, p.status, reason)
    if first_bar is not None and p.ex_date <= first_bar:
        return Outcome(p, SKIPPED, "before_bars")  # no instrument has a bar to hold it
    if disagree:
        return Outcome(p, REVIEW, REASON_SYMBOL_ISIN)
    if not reference.ids(p.symbol, p.isin, p.ex_date):
        sme = p.hint in SME_SERIES
        return Outcome(p, SKIPPED if p.table != T_BONUS or sme else REVIEW, "no_instrument")
    amount = total(p.parts)
    placed = ""
    if p.table == T_BONUS:
        # below the share on every session the ex-date can be on: price-neutral, not placed
        lowest = _lowest_candidate_bar(bars, p.ex_date, p.isin)
        if lowest is not None and not bonus_counts(amount, lowest.close):
            return _below_share(p, amount, lowest)
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
        SOURCES[p.table],
        amount,
        # NSE's feed and bc file announce the amount before the ex-date; research when its
        # source says
        at_ex_open(p.ex_date)
        if p.table != T_BONUS
        else from_sources(p.ex_date, [p.source_day], RESEARCH_SOURCE_DATE),
    )
    evidence = {
        "previous_day": before.day.isoformat(),
        "previous_close": str(before.close),
        "payouts": [{"kind": x.kind, "amount": str(x.amount)} for x in p.parts],
    }
    if placed:
        evidence["ex_date"] = placed
    return Outcome(p, STORED, "", detail, record, evidence=evidence)


def _lowest_candidate_bar(bars: Sequence[Bar], research_date: date, isin: str) -> Bar | None:
    """The lowest-closing bar that can be the previous session of a bonus's ex-date: any from
    `NAMED_BEFORE` + `PREVIOUS_SLACK` days before the research date to `NAMED_AFTER` after it
    (`bonus_ex_date`'s candidates). A value under `BONUS_MIN_SHARE` of its close is under it on
    every candidate."""
    first = research_date - timedelta(days=NAMED_BEFORE + PREVIOUS_SLACK)
    last = research_date + timedelta(days=NAMED_AFTER)
    window = [b for b in bars if first <= b.day < last and b.close > 0]
    if len({b.isin for b in window}) > 1:
        window = [b for b in window if b.isin == isin]
    return min(window, key=lambda b: b.close, default=None)


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
            source=next(
                (SOURCES[o.proposal.table] for o in group if o.proposal.table != T_BONUS),
                first.record.source,
            ),
            known=latest(o.record.known or UNKNOWN for o in group if o.record is not None),
        )
        first.evidence["payouts"] = [{"kind": x.kind, "amount": str(x.amount)} for x in parts]
        first.evidence["merged"] = [f"{o.proposal.table}:{o.proposal.rowid}" for o in rest]
        for o in rest:
            o.record, o.merged_into = None, first
        # the sum is held to the limit each part was
        assert first.record is not None
        assert first.record.cash_amount is not None
        close = require_exact(first.evidence["previous_close"], "previous_close")
        if over_yield(first.record.cash_amount, close):
            first.status, first.reason, first.record = REVIEW, "amount_vs_price", None
            first.detail += f"; merged {describe(parts)} over half the close"


def settle(
    outcomes: Sequence[Outcome],
    reference: Reference,
    stored: Mapping[str, list[StoredDividend]],
    held: set[tuple],
) -> None:
    """Each candidate against the dividends stored on its ex-date for the company (its chain):
    the same amount on every copy that has one is `already`, a missing amount is added
    (`amount_added`), another amount on any copy is a `conflict`; a key the owner vacated is
    `skipped`. A merged outcome takes its row's verdict."""
    for o in outcomes:
        r = o.record
        if r is None:
            continue
        p = o.proposal
        ids = reference.ids(p.symbol, p.isin, r.ex_date) | {str(r.instrument_id)}
        same_day = [s for iid in ids for s in stored.get(iid, ()) if s.ex_date == r.ex_date]
        amounts = {s.amount for s in same_day if s.amount is not None}
        if r.key in held:
            o.status, o.reason, o.record = SKIPPED, "owner_vacated", None
        elif amounts and amounts != {r.cash_amount}:
            # any stored copy in the chain with another amount (the series reads the nearest)
            o.status, o.reason, o.record = CONFLICT, "stored_amount_differs", None
            o.detail += f"; stored {', '.join(sorted(str(a) for a in amounts))}"
        elif amounts:
            o.status, o.record = ALREADY, None
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
    session: Session,
    store: DataStore,
    feed: Sequence[FeedRow],
    extra: Sequence[Proposal] = (),
    table: str = T_FEED,
) -> list[Outcome]:
    """Every event's outcome, the feed's (or the nightly file's) and the `extra` proposals' (the
    research file f's bonus distributions); nothing is written."""
    props = feed_proposals(feed, table) + list(extra)
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


def apply(
    session: Session, outcomes: Sequence[Outcome], *, journal: Mapping[str, Any], now: datetime
) -> tuple[int, int]:
    """Store every new row (`load_actions`), set the amount of each stored dividend that had none,
    and journal each as `corporate_action.imported` in the same transaction; returns (rows
    created, amounts added). Raises when the database disagrees with the plan (a row appeared or
    got an amount meanwhile): nothing is then kept."""
    new = [o for o in outcomes if o.status == STORED and o.record is not None and o.fill is None]
    fills = [o for o in outcomes if o.status == STORED and o.fill is not None]
    # under the reference-data lock, taken before the stored rows it fills are read
    created = store_new(session, [o.record for o in new if o.record is not None])
    ledger = Ledger.open(session, {o.fill.instrument_id for o in fills if o.fill is not None})
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
        known = o.record.known or UNKNOWN
        values = {"cash_amount": o.record.cash_amount, "known_on": known.on}
        values["known_basis"] = known.basis
        ledger.accept(row, values, source=o.record.source, text=o.record.raw_text)
    journal = {**journal, "run_id": str(uuid.uuid4())}
    for o in [*new, *fills]:
        r = o.record
        assert r is not None
        instrument = uuid.UUID(o.fill.instrument_id) if o.fill is not None else r.instrument_id
        imported = {
            "ex_date": r.ex_date.isoformat(),
            "type": r.action_type,
            "cash_amount": str(r.cash_amount),
            "amount_added": o.fill is not None,
            "text": r.raw_text,
        }
        journal_imported(
            session, instrument, o.proposal, imported, journal=journal, evidence=o.evidence, now=now
        )
    session.flush()
    return created, len(fills)

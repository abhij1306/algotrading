"""The adjusted series' input: which instruments are linked into one chain, and what the
adjusted views of `bars_daily` apply over each chain (PR 74, CA3).

A chain is the instruments `hat_isin_lineage` links (`status = linked`, `stored_links`), read
under its current id. `stored_adjustment` is the one definition of what the adjusted series
applies: every stored price-changing action and every dividend with an amount, once per chain
(`chain_factors`, `chain_dividends`: the copy nearest the chain's current ISIN wins), and the
linked lineage. Every reader of `bars_daily` takes it from here; how a succession comes to be
linked is `lineage`'s.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatCorporateAction, HatIsinLineage
from .corporate_actions import PRICE_NEUTRAL_TYPES, TYPE_DIVIDEND
from .store import ActionFactor, Adjustment, DividendCash

LINKED, REVIEW = "linked", "review"  # the statuses of a `hat_isin_lineage` row


class LineageError(HatError):
    """A lineage rule was broken; `code` is stable and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Action:
    """A stored corporate action, as the rules read it."""

    instrument_id: str
    ex_date: date
    action_type: str
    factor: Decimal
    text: str = ""
    demerger: bool = False

    @property
    def changes_price(self) -> bool:
        return self.action_type not in PRICE_NEUTRAL_TYPES


# --- chains -------------------------------------------------------------------------------------


def chain_members(links: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    """Each instrument of a linked pair -> every instrument of its chain (itself included)."""
    members: dict[str, frozenset[str]] = {}
    for old, new in links:
        group = members.get(old, frozenset({old})) | members.get(new, frozenset({new}))
        for iid in group:
            members[iid] = group
    return members


def chain_neighbours(links: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    """Each instrument of a linked pair -> its linked predecessor and successor."""
    found: dict[str, set[str]] = defaultdict(set)
    for old, new in links:
        found[old].add(new)
        found[new].add(old)
    return found


def chains(links: Iterable[tuple[str, str]]) -> dict[str, tuple[str, int]]:
    """Each linked instrument -> (its chain's current id, its distance from it); the current
    one maps to itself at 0. Raises on a fork or a cycle."""
    successor: dict[str, str] = {}
    predecessor: dict[str, str] = {}
    for old, new in links:
        if old in successor or new in predecessor:
            raise LineageError("lineage_fork")
        successor[old], predecessor[new] = new, old
    out: dict[str, tuple[str, int]] = {}
    for node in {*successor, *predecessor}:
        head, depth = node, 0
        while head in successor:
            head, depth = successor[head], depth + 1
            if depth > len(successor):
                raise LineageError("lineage_cycle")
        out[node] = (head, depth)
    return out


# --- the adjustment -----------------------------------------------------------------------------


def chain_factors(
    actions: Iterable[Action], heads: Mapping[str, tuple[str, int]]
) -> tuple[list[ActionFactor], list[tuple[str, date, str]]]:
    """The price-changing actions keyed by chain, one per (chain, ex-date, type): the copy of the
    member nearest the chain's current ISIN wins. The keys whose copies disagree come second."""
    best: dict[tuple[str, date, str], tuple[int, str, Action]] = {}
    conflicts: set[tuple[str, date, str]] = set()
    for action in actions:
        if not action.changes_price:
            continue
        chain, depth = heads.get(action.instrument_id, (action.instrument_id, 0))
        key = (chain, action.ex_date, action.action_type)
        rank = (depth, action.instrument_id, action)
        held = best.get(key)
        if held is not None and held[2].factor != action.factor:
            conflicts.add(key)
        if held is None or rank[:2] < held[:2]:
            best[key] = rank
    factors = [
        ActionFactor(key[0], key[1], held[2].factor)
        for key, held in sorted(best.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))
    ]
    return factors, sorted(conflicts)


def chain_dividends(
    dividends: Iterable[tuple[str, date, Decimal]], heads: Mapping[str, tuple[str, int]]
) -> list[DividendCash]:
    """The dividends with a cash amount keyed by chain, one per (chain, ex-date): the copy of the
    member nearest the chain's current ISIN wins, as for the factors."""
    best: dict[tuple[str, date], tuple[int, str, Decimal]] = {}
    for instrument_id, ex_date, amount in dividends:
        chain, depth = heads.get(instrument_id, (instrument_id, 0))
        rank = (depth, instrument_id, amount)
        held = best.get((chain, ex_date))
        if held is None or rank[:2] < held[:2]:
            best[(chain, ex_date)] = rank
    return [DividendCash(key[0], key[1], held[2]) for key, held in sorted(best.items())]


def adjustment(
    actions: Iterable[Action],
    links: Iterable[tuple[str, str]],
    dividends: Iterable[tuple[str, date, Decimal]] = (),
) -> Adjustment:
    """What the adjusted views apply, from the stored actions, the linked pairs and the
    dividends with a cash amount (instrument, ex-date, rupees per share)."""
    heads = chains(links)
    factors, _ = chain_factors(actions, heads)
    lineage = {member: head for member, (head, depth) in heads.items() if depth}
    return Adjustment(tuple(factors), lineage, tuple(chain_dividends(dividends, heads)))


# --- the database -------------------------------------------------------------------------------


def stored_links(session: Session) -> list[tuple[str, str]]:
    rows = session.execute(
        select(HatIsinLineage.old_instrument_id, HatIsinLineage.new_instrument_id).where(
            HatIsinLineage.status == LINKED
        )
    )
    return [(str(old), str(new)) for old, new in rows]


def stored_actions(session: Session) -> list[Action]:
    return [
        Action(
            str(r.instrument_id),
            r.ex_date,
            r.action_type,
            Decimal(r.adjustment_factor),
            r.raw_text,
            r.demerger,
        )
        for r in session.scalars(select(HatCorporateAction))
    ]


def stored_dividends(session: Session) -> list[tuple[str, date, Decimal]]:
    """(instrument, ex-date, cash per share) of every stored dividend with an amount (CA3)."""
    rows = session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatCorporateAction.ex_date,
            HatCorporateAction.cash_amount,
        ).where(
            HatCorporateAction.action_type == TYPE_DIVIDEND,
            HatCorporateAction.cash_amount.is_not(None),
        )
    )
    return [(str(i), ex, Decimal(amount)) for i, ex, amount in rows]


def stored_adjustment(session: Session) -> Adjustment:
    """The adjusted views' input: every stored price-changing action and every dividend with an
    amount, once per chain, and the linked lineage. Every reader of `bars_daily` takes it from
    here."""
    return adjustment(stored_actions(session), stored_links(session), stored_dividends(session))

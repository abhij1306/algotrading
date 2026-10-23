"""The order in which a day's triggers take the open slots (09 §2, HAT-BT-026): pure.

Live takes candidates in the order they trigger, so on a capped day (more triggers than open
slots) the first breaks get the slots. Daily bars cannot show that order, so the backtest
estimates it (`time`, the default): a gap over (an entry at the open) triggers at the open,
before any break during the day, and the others break in ascending order of the distance from the
open to the level, (level - open) / open. Ties keep the watchlist's score order. `score` (the
PR 12 rule, the highest score first) and `random:K` (a permutation of the score order fixed by
K and the day) are variants for sensitivity runs. The rule is recorded in the run's metrics and
`config_digest`.

These functions only order the candidates. Whether a candidate triggers and whether its entry
is taken is the loop's and `evaluate_signal`'s decision.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import TypeVar

from .fills import TOUCH

__all__ = [
    "SLOT_ORDER_RANDOM",
    "SLOT_ORDER_SCORE",
    "SLOT_ORDER_TIME",
    "is_slot_order",
    "order_slots",
    "trigger_rank",
]

SLOT_ORDER_TIME = "time"  # the default: the estimated trigger time
SLOT_ORDER_SCORE = "score"  # the PR 12 rule: the watchlist score, highest first
SLOT_ORDER_RANDOM = "random"  # `random:K`: the score order permuted by the seed K and the day
# one spelling per seed, so digests agree; ASCII digits only
_RANDOM = re.compile(r"random:(0|[1-9]\d{0,8})", re.ASCII)

_GAP = 0  # an open above the level: triggers at the open
_BREAK = 1  # a break during the day
_NONE = 2  # no level or no usable open: cannot trigger

T = TypeVar("T")


def is_slot_order(rule: str) -> bool:
    """Whether `rule` is `time`, `score` or `random:K` with K a whole number written without
    leading zeros (below 10^9)."""
    return rule in (SLOT_ORDER_TIME, SLOT_ORDER_SCORE) or _RANDOM.fullmatch(rule) is not None


def trigger_rank(
    open_: Decimal | None, level: Decimal | None, entry_fill_rule: str
) -> tuple[int, Decimal]:
    """The estimated trigger time of a break of `level` on a day that opened at `open_`, as a
    sort key: a gap over first (an entry at the open: open > level, or open = level under
    `touch`, HAT-BT-020), then (level - open) / open ascending; a row with no level or no
    positive open last (it cannot trigger)."""
    if open_ is None or level is None or open_ <= 0:
        return _NONE, Decimal(0)
    if open_ > level or (open_ == level and entry_fill_rule == TOUCH):
        return _GAP, Decimal(0)
    return _BREAK, (level - open_) / open_


def order_slots(
    candidates: Sequence[T],
    rule: str,
    day: date,
    rank: Callable[[T], tuple[int, Decimal]],
) -> list[T]:
    """`candidates` (in the watchlist's score order) in the order they take the slots under
    `rule` on `day`: `time` sorts them stably by `rank` (ties keep the score order), `score`
    keeps them, and `random:K` orders them by the SHA-256 of K, the day and each one's place in
    the score order, so a run is reproducible and one day's order does not depend on the days
    before it. An unknown rule raises ValueError."""
    if rule == SLOT_ORDER_TIME:
        return sorted(candidates, key=rank)
    if rule == SLOT_ORDER_SCORE:
        return list(candidates)
    found = _RANDOM.fullmatch(rule)
    if found is None:
        raise ValueError(f"unknown slot order {rule!r}")
    seed = f"{found.group(1)}:{day.isoformat()}"

    def draw(place: int) -> bytes:
        return hashlib.sha256(f"{seed}:{place}".encode()).digest()

    return [candidates[n] for n in sorted(range(len(candidates)), key=draw)]

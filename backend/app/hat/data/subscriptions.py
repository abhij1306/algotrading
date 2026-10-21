"""HAT's live subscriptions: one `SubscriptionRegistry` owner, `"hat"` (HAT-DATA-112).

The owner's symbol set is always `watchlist | open positions | EP candidates`, in DB symbol
format. Open positions are part of the set by construction, so no change to the watchlist or the
candidates can drop one. The registry is a refcount over owners, so releasing HAT's symbols never
unsubscribes a symbol a websocket client or the HTTP API still holds: the provider is told only
about symbols no owner holds any more.

The provider calls are injected as plain synchronous callables. This code runs on worker threads
(job bodies) as well as on the loop, and a callable bound to the live service hands the call to
the event loop itself (see `live.threadsafe_provider`).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.subscription_registry import SubscriptionRegistry

from ..models import HatInstrument, HatPosition, HatWatchlistEntry

__all__ = [
    "HAT_OWNER",
    "SYMBOL_LIMIT",
    "LiveSubscriptions",
    "SubscriptionInputs",
    "load_inputs",
]

logger = logging.getLogger(__name__)

HAT_OWNER = "hat"
# DS "Live data": under 200 symbols. A set at or above the limit raises a health alert.
SYMBOL_LIMIT = 200

SymbolCall = Callable[[list[str]], None]
AlertCall = Callable[[str], None]


@dataclass(frozen=True)
class SubscriptionInputs:
    watchlist: frozenset[str] = frozenset()
    positions: frozenset[str] = frozenset()
    candidates: frozenset[str] = frozenset()


def load_inputs(
    session: Session, trade_date: date, candidates: Iterable[str] = ()
) -> SubscriptionInputs:
    """Today's watchlist symbols and every open position's symbol (virtual ones too: shadow
    trades need prices as well), from the database; `candidates` come from the gap scan."""
    watch = session.scalars(
        select(HatInstrument.symbol)
        .join(HatWatchlistEntry, HatWatchlistEntry.instrument_id == HatInstrument.id)
        .where(HatWatchlistEntry.trade_date == trade_date)
    )
    held = session.scalars(
        select(HatInstrument.symbol)
        .join(HatPosition, HatPosition.instrument_id == HatInstrument.id)
        .where(HatPosition.closed_at.is_(None))
    )
    return SubscriptionInputs(frozenset(watch), frozenset(held), frozenset(candidates))


class LiveSubscriptions:
    """Keeps the `"hat"` owner's registry entry equal to the union of the three inputs."""

    def __init__(
        self,
        registry: SubscriptionRegistry,
        subscribe: SymbolCall,
        unsubscribe: SymbolCall,
        alert: AlertCall | None = None,
        *,
        limit: int = SYMBOL_LIMIT,
    ) -> None:
        self._registry = registry
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._alert = alert
        self._limit = limit
        self._lock = threading.Lock()
        self._update = threading.RLock()  # one refresh or candidate add at a time
        self._inputs = SubscriptionInputs()

    @property
    def inputs(self) -> SubscriptionInputs:
        return self._inputs

    def desired(self) -> frozenset[str]:
        inputs = self._inputs
        return inputs.watchlist | inputs.positions | inputs.candidates

    def sync(self, inputs: SubscriptionInputs) -> None:
        """Make the owner's set `watchlist | positions | candidates` and tell the provider about
        the symbols no owner held before (subscribe) or holds now (unsubscribe)."""
        with self._update:
            with self._lock:
                self._inputs = inputs
                wanted = self._registry.canonical_symbols(self.desired())
                held = set(self._registry.symbols_of(HAT_OWNER))
                added = self._registry.add(HAT_OWNER, wanted - held)
                released = self._registry.remove(HAT_OWNER, held - wanted)
            if added:
                self._call(self._subscribe, added)
            if released:
                self._call(self._unsubscribe, released)
            if len(wanted) >= self._limit and self._alert is not None:
                self._raise("subscription_limit")

    def add_candidates(self, symbols: Collection[str]) -> None:
        """Add EP candidates to the current inputs (the 09:17 gap-scan subscribe)."""
        with self._update:  # read and write under one lock so a refresh is never overwritten
            inputs = self._inputs
            self.sync(
                SubscriptionInputs(
                    inputs.watchlist, inputs.positions, inputs.candidates | frozenset(symbols)
                )
            )

    def release_all(self) -> None:
        """HAT stops: drop the owner; the provider unsubscribes only unheld symbols."""
        with self._lock:
            self._inputs = SubscriptionInputs()
            released = self._registry.drop_owner(HAT_OWNER)
        if released:
            self._call(self._unsubscribe, released)

    @staticmethod
    def _call(call: SymbolCall, symbols: set[str]) -> None:
        try:
            call(sorted(symbols))
        except Exception:  # the registry is already right; the session replays it on connect
            logger.exception("live provider call failed")

    def _raise(self, code: str) -> None:
        try:
            if self._alert is not None:
                self._alert(code)
        except Exception:
            logger.exception("live health alert failed")

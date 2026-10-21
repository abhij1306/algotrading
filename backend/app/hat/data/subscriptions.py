"""HAT's live subscriptions: who wants which live symbols (`SubscriptionRegistry`, the single
source of truth for provider subscriptions, owned by HAT since K3) and the one owner HAT itself
holds, `"hat"` (HAT-DATA-112).

Each owner (HAT, a legacy websocket client id, or the legacy REST API owner) holds a set of
symbols. A symbol is needed at the provider while at least one owner holds it. Every mutation
returns the provider delta, so callers never diff before/after snapshots. The registry is pure,
synchronous and thread-safe: no I/O, no asyncio, one lock around all state.

The owner's symbol set is always `watchlist | open positions | EP candidates`, in DB symbol
format. Open positions are part of the set by construction, so no change to the watchlist or the
candidates can drop one. The registry is a refcount over owners, so releasing HAT's symbols never
unsubscribes a symbol a websocket client or the HTTP API still holds: the provider is told only
about symbols no owner holds any more.

The provider calls are injected as plain synchronous callables. This code runs on worker threads
(job bodies) as well as on the loop, and a callable bound to the live service hands the call to
the event loop itself (see `live.threadsafe_provider`; `LiveFeed` in `feed.py` is the provider).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection, Hashable, Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatInstrument, HatPosition, HatWatchlistEntry

__all__ = [
    "API_OWNER",
    "HAT_OWNER",
    "SYMBOL_LIMIT",
    "LiveSubscriptions",
    "SubscriptionRegistry",
    "SubscriptionInputs",
    "load_inputs",
]

logger = logging.getLogger(__name__)

HAT_OWNER = "hat"
# Owner of symbols subscribed through the legacy POST /api/websocket/subscribe (no client drops
# them); deleted with that route (K5b).
API_OWNER = "api"
Owner = Hashable
# DS "Live data": under 200 symbols. A set at or above the limit raises a health alert.
SYMBOL_LIMIT = 200

SymbolCall = Callable[[list[str]], None]
AlertCall = Callable[[str], None]


class SubscriptionRegistry:
    """Refcount of symbols per owner; symbols are canonicalised to DB format by the injected
    `canonicalize` (HAT's `symbol_master.to_db`), so the registry knows no symbol master."""

    def __init__(self, canonicalize: Callable[[str], str]) -> None:
        self._canonicalize = canonicalize
        self._lock = threading.Lock()
        self._by_owner: dict[Owner, set[str]] = {}
        # symbol -> number of owners holding it; a symbol is present only while > 0.
        self._refcount: dict[str, int] = {}

    def _canonical(self, symbols: Iterable[str]) -> set[str]:
        canonical = {self._canonicalize(s) for s in symbols if s}
        canonical.discard("")
        return canonical

    def canonical_symbols(self, symbols: Iterable[str]) -> set[str]:
        """`symbols` in the registry's canonical (DB) format, without blanks."""
        return self._canonical(symbols)

    def add(self, owner: Owner, symbols: Iterable[str]) -> set[str]:
        """Give `owner` the symbols; return those no owner held before (provider subscribe)."""
        wanted = self._canonical(symbols)
        newly_needed: set[str] = set()
        with self._lock:
            held = self._by_owner.setdefault(owner, set())
            for symbol in wanted - held:
                held.add(symbol)
                count = self._refcount.get(symbol, 0)
                if count == 0:
                    newly_needed.add(symbol)
                self._refcount[symbol] = count + 1
        return newly_needed

    def remove(self, owner: Owner, symbols: Iterable[str]) -> set[str]:
        """Take the symbols from `owner`; return those no owner holds now (provider unsubscribe)."""
        unwanted = self._canonical(symbols)
        with self._lock:
            held = self._by_owner.get(owner)
            if not held:
                return set()
            released = self._release(held & unwanted)
            held -= unwanted
            if not held:
                del self._by_owner[owner]
        return released

    def drop_owner(self, owner: Owner) -> set[str]:
        """Forget `owner`; return the symbols no remaining owner holds (provider unsubscribe)."""
        with self._lock:
            held = self._by_owner.pop(owner, set())
            return self._release(held)

    def _release(self, symbols: set[str]) -> set[str]:
        """Drop one reference to each symbol (lock held); return the symbols now unowned."""
        no_longer_needed: set[str] = set()
        for symbol in symbols:
            count = self._refcount[symbol] - 1
            if count:
                self._refcount[symbol] = count
            else:
                del self._refcount[symbol]
                no_longer_needed.add(symbol)
        return no_longer_needed

    def all_symbols(self) -> set[str]:
        """Every symbol some owner holds: what the provider should be subscribed to."""
        with self._lock:
            return set(self._refcount)

    def owners_of(self, symbol: str) -> set[Owner]:
        """The owners holding `symbol` (any format)."""
        canonical = self._canonicalize(symbol) if symbol else ""
        with self._lock:
            if canonical not in self._refcount:
                return set()
            return {owner for owner, held in self._by_owner.items() if canonical in held}

    def is_held(self, symbol: str) -> bool:
        """True while some owner holds `symbol` (any format)."""
        canonical = self._canonicalize(symbol) if symbol else ""
        with self._lock:
            return canonical in self._refcount

    def symbols_of(self, owner: Owner) -> frozenset[str]:
        """The symbols `owner` holds (empty for an unknown owner)."""
        with self._lock:
            return frozenset(self._by_owner.get(owner, ()))


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

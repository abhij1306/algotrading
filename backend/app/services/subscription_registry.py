"""Who wants which live symbols: the single source of truth for provider subscriptions.

Each owner (a websocket client id, or the REST API owner) holds a set of symbols. A symbol
is needed at the provider while at least one owner holds it. Every mutation returns the
provider delta, so callers never diff before/after snapshots.

Pure, synchronous and thread-safe: no I/O, no asyncio, one lock around all state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable, Iterable

# Owner of symbols subscribed through POST /api/websocket/subscribe (no client drops them).
API_OWNER = "api"

Owner = Hashable


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

"""A small thread-safe TTL + LRU cache for screener payloads."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """At most `maxsize` entries, each served for `ttl_seconds` after it was stored."""

    def __init__(
        self,
        ttl_seconds: float,
        maxsize: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()

    def get(self, key: Hashable) -> V | None:
        now = self._clock()
        with self._lock:
            expired = [k for k, (at, _) in self._entries.items() if now - at >= self.ttl_seconds]
            for k in expired:
                del self._entries[k]
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def set(self, key: Hashable, value: V) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

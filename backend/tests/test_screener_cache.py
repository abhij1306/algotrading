"""The screener payload cache serves an entry until its TTL passes and keeps at most
`maxsize` entries, evicting the least recently used."""

from app.services.screener import TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_an_entry_is_served_until_its_ttl_passes() -> None:
    clock = FakeClock()
    cache: TTLCache[str] = TTLCache(ttl_seconds=5.0, maxsize=4, clock=clock)
    cache.set("k", "payload")

    clock.now = 4.9
    assert cache.get("k") == "payload"
    clock.now = 5.0
    assert cache.get("k") is None


def test_the_least_recently_used_entry_is_evicted_first() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=60.0, maxsize=2, clock=FakeClock())
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1

    cache.set("c", 3)

    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (1, None, 3)


def test_clear_drops_every_entry() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=60.0, maxsize=2, clock=FakeClock())
    cache.set("a", 1)

    cache.clear()

    assert cache.get("a") is None

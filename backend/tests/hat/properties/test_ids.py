"""PBT ids.uuid7_ordered (HAT-SCH-011): uuid v7 values are well formed and strictly increasing."""

import threading
import uuid

from hypothesis import given
from hypothesis import strategies as st

from app.hat.ids import Uuid7Generator, uuid7

# A clock reading in Unix milliseconds; stays far below the 48-bit field limit.
clock_ms = st.integers(min_value=0, max_value=2**47)


def assert_well_formed(value: uuid.UUID) -> None:
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


@given(readings=st.lists(clock_ms, min_size=1, max_size=300))
def test_uuid7_ordered(readings: list[int]) -> None:
    """For every clock behaviour, including steps backwards, ids rise strictly in byte order."""
    feed = iter(readings)
    generator = Uuid7Generator(clock_ms=lambda: next(feed))
    ids = [generator.next() for _ in readings]
    for value in ids:
        assert_well_formed(value)
    for earlier, later in zip(ids, ids[1:], strict=False):
        assert earlier.bytes < later.bytes


@given(start=clock_ms, burst=st.integers(min_value=1, max_value=9000))
def test_uuid7_ordered_within_one_millisecond(start: int, burst: int) -> None:
    """A frozen clock forces the 12-bit counter to overflow into the next millisecond."""
    generator = Uuid7Generator(clock_ms=lambda: start)
    ids = [generator.next() for _ in range(burst)]
    for value in ids:
        assert_well_formed(value)
    for earlier, later in zip(ids, ids[1:], strict=False):
        assert earlier.bytes < later.bytes


@given(threads=st.integers(min_value=2, max_value=6), per_thread=st.integers(1, 200))
def test_uuid7_unique_across_threads(threads: int, per_thread: int) -> None:
    """The process-wide `uuid7()` never repeats a value, whatever the thread interleaving."""
    seen: list[list[uuid.UUID]] = [[] for _ in range(threads)]

    def work(slot: int) -> None:
        seen[slot] = [uuid7() for _ in range(per_thread)]

    workers = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    flat = [value for chunk in seen for value in chunk]
    assert len(set(flat)) == len(flat)
    for chunk in seen:
        for value in chunk:
            assert_well_formed(value)
        for earlier, later in zip(chunk, chunk[1:], strict=False):
            assert earlier.bytes < later.bytes

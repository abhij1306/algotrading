from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from time import perf_counter


@dataclass
class TimingStats:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0

    def to_dict(self) -> dict[str, int | float]:
        avg_ms = (self.total_ms / self.count) if self.count else 0.0
        return {
            "count": self.count,
            "avg_ms": round(avg_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "last_ms": round(self.last_ms, 3),
            "total_ms": round(self.total_ms, 3),
        }


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._timings: dict[str, TimingStats] = {}

    def increment(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe_ms(self, name: str, value_ms: float) -> None:
        with self._lock:
            stats = self._timings.setdefault(name, TimingStats())
            stats.count += 1
            stats.total_ms += value_ms
            stats.max_ms = max(stats.max_ms, value_ms)
            stats.last_ms = value_ms

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        started = perf_counter()
        try:
            yield
        finally:
            self.observe_ms(name, (perf_counter() - started) * 1000.0)

    def snapshot(
        self, prefix: str | None = None, *, strip_prefix: bool = False
    ) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            timings = {name: stats.to_dict() for name, stats in self._timings.items()}

        if prefix is None:
            return {
                "counters": counters,
                "gauges": gauges,
                "timings": timings,
            }

        def _filter_map(source: dict[str, object]) -> dict[str, object]:
            filtered: dict[str, object] = {}
            for key, value in source.items():
                if not key.startswith(prefix):
                    continue
                target_key = key[len(prefix) :] if strip_prefix else key
                filtered[target_key] = value
            return filtered

        return {
            "counters": _filter_map(counters),
            "gauges": _filter_map(gauges),
            "timings": _filter_map(timings),
        }


runtime_metrics = RuntimeMetrics()

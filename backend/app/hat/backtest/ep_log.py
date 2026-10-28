"""The episodic-pivot log of a run (plan Part B logging, C3): one record per pre-open candidate
of a session, how far it got through the EP funnel and what it read, so a run of a config with
the `episodic_pivot` playbook reports its signals, trades and near misses on the same columns.
Nothing here decides a trade: the loop decides and fills in the record as it goes.

The funnel (`ep_gates`, the C3 order): `gap_scan` (a member that passed the playbook's universe
stage and opened at least the scan floor over its previous close), `gap_up`, the hard setup
criteria in order (`neglect`), then the trigger criteria in the fixed order `earnings_catalyst`,
`early_volume`, `price_breaks_level` (the opening-range break) and any other enabled trigger
type, the entry filters in their order (`max_chase` always runs: HAT-CRIT-122) and `filled`. A
gate a playbook does not enable is left out. On a 5-minute session a candidate reaches the
deepest prefix of its trigger gates that passed together on one bar (`trigger_depth`); on the
daily estimate the trigger gates are judged once.

Logged per candidate, all read before the gap day or at the trigger: the gap, the 63-session
return and the 126-session range (max high / min low - 1) to the previous session, the pre-gap
ADR(20) (the repo's formula: the mean of 100 x (high / low - 1), `universe.adr_min`), the trigger
time, the day volume at the trigger against ADV(20), and the newest results filing public in
(the previous session's open, the trigger instant] with its growth as known then
(`live.results_filing`). `catalyst_type` is `earnings` with such a filing, `other_announcement`
when only `catalyst_attached` matched (no announcements are stored for the past, so never in a
run today), else `none`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from ..engine.strategy.config import Playbook, StrategyConfig
from ..engine.trigger import REASON_GAP_TOO_SMALL, ResultEvent, results_filing
from ..time import IST

__all__ = [
    "EARNINGS",
    "EP_CANDIDATE_COLUMNS",
    "EP_COLUMNS",
    "EpRecord",
    "catalyst_type",
    "ep_gates",
    "funnel",
    "trigger_depth",
    "trigger_gates",
    "uses_results",
]

GAP_SCAN = "gap_scan"
GAP_UP = "gap_up"
EARNINGS = "earnings_catalyst"
EARLY_VOLUME = "early_volume"
LEVEL = "price_breaks_level"
CATALYST = "catalyst_attached"
MAX_CHASE = "max_chase"
FILLED = "filled"
GAP_TOO_SMALL = REASON_GAP_TOO_SMALL
# The trigger gates in the C3 order; another enabled trigger type follows them in config order.
_TRIGGER_ORDER = (GAP_UP, EARNINGS, EARLY_VOLUME, LEVEL)
CATALYST_EARNINGS = "earnings"
CATALYST_OTHER = "other_announcement"
CATALYST_NONE = "none"
_PLACES = 4

# The EP columns of signals.csv and trades.csv (empty for another playbook's rows).
EP_COLUMNS = (
    "catalyst_type",
    "results_public_at",
    "sales_yoy_pct",
    "eps_yoy_pct",
    "turnaround",
    "gap_pct",
    "trigger_time",
    "vol_vs_adv20",
    "range_126d_pct",
    "adr20_pre_gap_pct",
    "ret_63d_pct",
)
# ep_candidates.csv: one row per pre-open candidate-day in the gap scan.
EP_CANDIDATE_COLUMNS = (
    "trade_date",
    "instrument_id",
    "symbol",
    "playbook",
    "basis",
    "reached",
    "failed_gate",
    "reason",
    "signal_state",
    *EP_COLUMNS,
)


def uses_results(config: StrategyConfig) -> bool:
    """Whether a pre-open playbook of `config` enables `earnings_catalyst`: its run must have
    the results store."""
    return any(
        i.enabled and i.type == EARNINGS for p in config.playbooks if p.preopen for i in p.trigger
    )


def trigger_gates(playbook: Playbook) -> tuple[str, ...]:
    """The playbook's enabled blocking trigger types in the funnel's order (`catalyst_attached`,
    never blocking in a run, left out)."""
    enabled = [i.type for i in playbook.trigger if i.enabled and i.type != CATALYST]
    fixed = [t for t in _TRIGGER_ORDER if t in enabled]
    return (*fixed, *(t for t in dict.fromkeys(enabled) if t not in _TRIGGER_ORDER))


def ep_gates(playbook: Playbook) -> tuple[str, ...]:
    """The EP funnel of `playbook` (module docstring)."""
    triggers = trigger_gates(playbook)
    setup = [i.type for i in playbook.setup.criteria if i.enabled and i.mode == "hard"]
    filters = [i.type for i in playbook.entry_filters if i.enabled]
    if MAX_CHASE not in filters:
        filters.append(MAX_CHASE)
    gap = (GAP_UP,) if GAP_UP in triggers else ()
    rest = tuple(t for t in triggers if t != GAP_UP)
    return (GAP_SCAN, *gap, *dict.fromkeys(setup), *rest, *dict.fromkeys(filters), FILLED)


def trigger_depth(order: Sequence[str], passed: Mapping[str, bool]) -> int:
    """How many of `order` pass from the first on (a missing type does not pass)."""
    depth = 0
    for gate in order:
        if not passed.get(gate, False):
            break
        depth += 1
    return depth


def catalyst_type(filing: ResultEvent | None, announced: bool) -> str:
    return CATALYST_EARNINGS if filing else CATALYST_OTHER if announced else CATALYST_NONE


def _round(value: float | Decimal | None) -> float | None:
    return None if value is None else round(float(value), _PLACES)


@dataclass
class EpRecord:
    """One pre-open candidate-day: the gates it passed (`reached`, the last), the first that
    failed and why, the basis it was decided on and the logged columns (`EP_COLUMNS`)."""

    trade_date: date
    instrument_id: str
    symbol: str
    playbook: str
    gates: tuple[str, ...]
    basis: str = ""
    reached: str = GAP_SCAN
    failed_gate: str | None = None
    reason: str | None = None
    signal_state: str | None = None
    columns: dict[str, Any] = field(default_factory=dict)

    def passed(self, gate: str) -> None:
        """`gate` passed: the record reached it (a gate the playbook lacks is ignored)."""
        if gate in self.gates and self.failed_gate is None:
            self.reached = max(self.reached, gate, key=self.gates.index)

    def failed(self, reason: str | None, gate: str | None = None) -> None:
        """The gate after `reached` (or `gate`) failed, the first failure only."""
        if self.failed_gate is not None:
            return
        at = self.gates.index(self.reached)
        self.failed_gate = gate or (self.gates[at + 1] if at + 1 < len(self.gates) else None)
        self.reason = reason

    def through(self, depth: int, triggers: Sequence[str]) -> None:
        """The first `depth` of the playbook's trigger gates passed (`trigger_depth`)."""
        for gate in triggers[:depth]:
            self.passed(gate)

    def at_trigger(
        self,
        at: datetime | None,
        cum_volume: int | None,
        adv20: Decimal | None,
        results: Sequence[ResultEvent],
        since: datetime | None,
        announced: bool = False,
    ) -> None:
        """The columns read at the trigger instant `at` (or the last bar judged)."""
        filing = None if at is None or since is None else results_filing(results, since, at)
        ratio = None
        if cum_volume is not None and adv20 is not None and adv20 > 0:
            ratio = _round(Decimal(cum_volume) / adv20)
        self.columns.update(
            trigger_time=None if at is None else at.astimezone(IST).time().isoformat("minutes"),
            vol_vs_adv20=ratio,
            catalyst_type=catalyst_type(filing, announced),
            results_public_at=None if filing is None else filing.public_at.isoformat(),
            sales_yoy_pct=_pct(None if filing is None else filing.sales_yoy),
            eps_yoy_pct=_pct(None if filing is None else filing.eps_yoy),
            turnaround=None if filing is None else filing.turnaround,
        )

    @property
    def row_key(self) -> tuple[date, str, str]:
        return (self.trade_date, self.playbook, self.symbol)

    def row(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "playbook": self.playbook,
            "basis": self.basis,
            "reached": self.reached,
            "failed_gate": self.failed_gate,
            "reason": self.reason,
            "signal_state": self.signal_state,
            **{c: self.columns.get(c) for c in EP_COLUMNS},
        }


def _pct(value: Decimal | None) -> float | None:
    return None if value is None else _round(value * 100)


def funnel(records: Iterable[EpRecord], gates: Sequence[str]) -> dict[str, int]:
    """The count of records that reached each gate of `gates`, in order (a record that failed
    gate g counts at the gates before g only)."""
    counts = dict.fromkeys(gates, 0)
    for record in records:
        if record.reached not in counts:
            continue
        for gate in gates[: gates.index(record.reached) + 1]:
            counts[gate] += 1
    return counts


# --- the C3 buckets ---------------------------------------------------------------------------


def _bucket(value: float | None, edges: Sequence[float], labels: Sequence[str]) -> str:
    """The label of the first edge `value` is under (the last label above them all), `unknown`
    for None: `labels` has one more item than `edges`."""
    if value is None:
        return "unknown"
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def sales_bucket(pct: float | None) -> str:
    return _bucket(pct, (25, 50, 100), ("lt25", "25_50", "50_100", "gt100"))


def gap_bucket(pct: float | None) -> str:
    return _bucket(pct, (10, 15, 25), ("lt10", "10_15", "15_25", "gt25"))


def adr_bucket(pct: float | None) -> str:
    return _bucket(pct, (3, 4, 6), ("lt3", "3_4", "4_6", "gt6"))


def range_bucket(pct: float | None) -> str:
    return _bucket(pct, (30, 60), ("lt30", "30_60", "gt60"))


_NINE_45 = time(9, 45)
_ELEVEN = time(11, 0)


def time_bucket(text: str | None) -> str:
    """The trigger time bucket: `0920_0945` (through 09:45), `0945_1100` (through 11:00),
    `after_1100`; `unknown` without a time."""
    if not text:
        return "unknown"
    at = time.fromisoformat(text)
    if at <= _NINE_45:
        return "0920_0945"
    return "0945_1100" if at <= _ELEVEN else "after_1100"


# Each C3 breakdown: its run.json key, the EP column it reads and its bucket rule (None: the
# column's own value).
BREAKDOWNS = (
    ("by_catalyst_type", "catalyst_type", None),
    ("by_sales_yoy", "sales_yoy_pct", sales_bucket),
    ("by_gap", "gap_pct", gap_bucket),
    ("by_trigger_time", "trigger_time", time_bucket),
    ("by_adr20", "adr20_pre_gap_pct", adr_bucket),
    ("by_range_126d", "range_126d_pct", range_bucket),
)

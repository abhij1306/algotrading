"""The S2 episodic-pivot refinement's pure seams (plan Part B): `earnings_catalyst`, the
time-anchored early volume, the neglect direction, the per-playbook universe and exits, the SMA
trail's activation and the EP log's funnel and buckets. Small hand-built inputs only."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
import yaml

from app.hat.backtest import ep_log
from app.hat.backtest.data import results_digest_text, results_events
from app.hat.data.bars import Bar
from app.hat.engine import exits
from app.hat.engine.criteria import CriterionResult, EvalContext
from app.hat.engine.criteria.live import (
    EarlyVolumeParams,
    EarningsCatalystParams,
    LiveContext,
    ResultEvent,
    early_volume,
    earnings_catalyst,
    growth_passes,
)
from app.hat.engine.criteria.setup import NeglectParams, neglect, range_pct
from app.hat.engine.live_entry import ep_gate, ep_stock_log, reads_results
from app.hat.engine.strategy.config import parse_config
from app.hat.engine.strategy.registry import STRATEGIES
from app.hat.engine.trigger import minutes_since_open
from app.hat.time import IST
from tests.hat.hat_criteria_support import EPOCH, Row, frame

AT = datetime(2024, 5, 3, 9, 40, tzinfo=IST)
SINCE = datetime(2024, 5, 2, 9, 15, tzinfo=IST)


def _seed() -> dict:
    return yaml.safe_load(STRATEGIES["momentum-breakout-ep"].config_text)


def _event(
    hours_before: float, sales: str | None, eps: str | None = None, turn: bool | None = None
) -> ResultEvent:
    return ResultEvent(
        AT - timedelta(hours=hours_before),
        date(2024, 3, 31),
        None if sales is None else Decimal(sales),
        None if eps is None else Decimal(eps),
        turn,
    )


def _catalyst(*events: ResultEvent, since: datetime | None = SINCE) -> CriterionResult:
    ctx = LiveContext(at=AT, results=events, results_since=since)
    return earnings_catalyst(ctx, EarningsCatalystParams())


def test_earnings_catalyst_window_is_previous_open_to_the_trigger() -> None:
    """The filing must be public in (previous session's 09:15, trigger]: one at 09:15 the day
    before is outside, one at the trigger instant is inside, one after it is invisible."""
    on_open = ResultEvent(SINCE, date(2024, 3, 31), Decimal("0.6"))
    assert _catalyst(on_open).reason == "no_results_filing"
    assert _catalyst(_event(0, "0.6")).passed
    later = ResultEvent(AT + timedelta(minutes=1), date(2024, 3, 31), Decimal("0.6"))
    assert _catalyst(later).reason == "no_results_filing"
    assert _catalyst(_event(1, "0.6"), since=None).reason == "no_data"


@pytest.mark.parametrize(
    ("sales", "eps", "turn", "passed"),
    [
        ("0.25", "0.25", False, True),  # both on the threshold
        ("0.2499", "1.0", False, False),  # sales short
        ("0.30", "0.10", False, False),  # eps short, no turnaround, sales under 50%
        ("0.30", None, True, True),  # turnaround
        ("0.50", None, False, True),  # sales alone at 50%
        ("0.4999", None, None, False),
    ],
)
def test_earnings_catalyst_growth_rule(
    sales: str, eps: str | None, turn: bool | None, passed: bool
) -> None:
    result = _catalyst(_event(2, sales, eps, turn))
    assert result.passed is passed
    assert result.value == pytest.approx(float(Decimal(sales) * 100))
    assert result.extras["sales_yoy_pct"] == pytest.approx(float(Decimal(sales) * 100))
    assert "results_public_at" in result.extras


def test_earnings_catalyst_thresholds_switch_off() -> None:
    """A null threshold is off: no sales floor, no EPS minimum, no sales-alone path; all three
    null, any filing in the window passes, undefined growth included."""
    undefined = ResultEvent(AT - timedelta(hours=1), date(2024, 3, 31))
    off = EarningsCatalystParams(sales_yoy_pct=None, eps_yoy_pct=None, sales_alone_pct=None)
    assert growth_passes(undefined, off) is True
    assert growth_passes(undefined, EarningsCatalystParams()) is None
    weak = ResultEvent(AT, date(2024, 3, 31), Decimal("-0.5"), Decimal("-0.5"), turnaround=False)
    assert growth_passes(weak, EarningsCatalystParams()) is False
    assert growth_passes(weak, EarningsCatalystParams(sales_yoy_pct=None)) is False  # eps short
    no_eps = EarningsCatalystParams(sales_yoy_pct=None, eps_yoy_pct=None)
    assert growth_passes(weak, no_eps) is True
    sales_only = EarningsCatalystParams(eps_yoy_pct=None, sales_alone_pct=None)
    assert growth_passes(_event(1, "0.25"), sales_only) is True
    assert growth_passes(_event(1, "0.2"), sales_only) is False
    alone_off = EarningsCatalystParams(sales_alone_pct=None)
    assert growth_passes(_event(1, "0.9", "0.1"), alone_off) is False


def test_earnings_catalyst_reads_the_newest_filing_and_fails_undefined_growth() -> None:
    """A revision filed later in the window replaces the first; undefined sales growth fails."""
    first, revised = _event(5, "0.1"), _event(1, "0.6", "0.6")
    assert _catalyst(first, revised).passed
    assert _catalyst(revised, first).passed
    assert _catalyst(_event(1, None, "0.9")).reason == "growth_undefined"


def test_early_volume_time_anchored() -> None:
    """ADV20 x min(1, minutes / 30): half the ADV 15 minutes in, all of it from 30 on; the
    at_trigger default is unchanged."""
    anchored = EarlyVolumeParams(mode="time_anchored")

    def check(volume: int, minutes: int, params: EarlyVolumeParams) -> bool:
        ctx = LiveContext(at=AT, cum_volume=volume, adv20=Decimal(1000), minutes=Fraction(minutes))
        return early_volume(ctx, params).passed

    assert check(500, 15, anchored)
    assert not check(499, 15, anchored)
    assert check(1000, 45, anchored)
    assert not check(999, 45, anchored)
    assert check(500, 300, EarlyVolumeParams())
    assert not check(499, 300, EarlyVolumeParams())
    assert not check(10**9, 0, anchored)  # no minutes: no data


def test_minutes_since_open_counts_to_the_bar_close() -> None:
    opens = datetime(2024, 5, 3, 9, 15, tzinfo=IST)
    bar = Bar("X", opens + timedelta(minutes=20), *(Decimal(1),) * 4, 1)
    assert minutes_since_open(bar, opens) == 25


def _closes(last: int) -> list[Row]:
    rows = [Row(EPOCH + timedelta(days=i), 10_100, 9_900, 10_000, 1000) for i in range(64)]
    rows[-1] = Row(rows[-1].day, last + 100, last - 100, last, 1000)
    return rows


def test_neglect_direction() -> None:
    """`up_only` lets a stock that fell more than the limit through; a rise still fails it."""
    day = EPOCH + timedelta(days=64)
    fell = EvalContext(as_of=day, instrument_id="", bars=frame(_closes(6_000)))
    rose = EvalContext(as_of=day, instrument_id="", bars=frame(_closes(14_000)))
    assert not neglect(fell, NeglectParams()).passed
    assert neglect(fell, NeglectParams(direction="up_only")).passed
    assert not neglect(rose, NeglectParams(direction="up_only")).passed


def test_range_pct() -> None:
    assert range_pct([12.0, 15.0], [10.0, 11.0]) == pytest.approx(50.0)
    assert range_pct([12.0], [None]) is None
    assert range_pct([], []) is None


def test_defaults_keep_the_canonical_form() -> None:
    """The new parameters and fields are left out of the canonical form at their defaults, so
    every stored config keeps its hash."""
    config = parse_config(_seed())
    stored = config.to_json()
    for playbook in stored["playbooks"]:
        assert "universe" not in playbook
        assert "exits" not in playbook
    ep = next(p for p in stored["playbooks"] if p["name"] == "episodic_pivot")
    early = next(t for t in ep["trigger"] if t["type"] == "early_volume")
    assert "mode" not in early["params"]
    assert "direction" not in ep["setup"]["criteria"][0]["params"]
    assert all("above_stop" not in e for e in stored["exits"])
    assert parse_config(stored).hash() == config.hash()


def _with_ep_exits(sma: int) -> dict:
    raw = _seed()
    ep = next(p for p in raw["playbooks"] if p["name"] == "episodic_pivot")
    ep["exits"] = [
        {"rule": "hard_stop"},
        {"rule": "partial_after_days", "days": 3, "sell_fraction": 0.33, "only_if_profit": True},
        {"rule": "move_stop_to_breakeven", "after": "partial"},
        {"rule": "close_below_sma", "sma": sma, "sell_fraction": 1.0, "above_stop": True},
    ]
    ep["universe"] = [{"type": "series_in", "params": {"series": ["EQ", "BE"]}}]
    return raw


def test_breakout_keeps_the_bot_exits_and_universe() -> None:
    """With EP exits and universe set, the breakout playbook's resolved stack and universe are
    the bot's, and its exit decisions at the exit seam are unchanged."""
    base, varied = parse_config(_seed()), parse_config(_with_ep_exits(20))
    assert varied.exits_for("breakout") == base.exits_for("breakout") == base.exits
    assert varied.universe_for("breakout") == base.universe
    assert varied.exits_for("episodic_pivot") != base.exits
    assert [i.type for i in varied.universe_for("episodic_pivot")] == ["series_in"]
    pos = exits.PositionState(100, 100, Decimal(100), Decimal(95), Decimal(95))
    for price, sma in ((Decimal(97), Decimal(98)), (Decimal(110), Decimal(105))):
        view = exits.ExitView(price, days_held=4, sma={10: sma, 20: sma})
        for phase in ("live", "close"):
            assert exits.evaluate(varied.exits_for("breakout"), pos, view, phase) == (
                exits.evaluate(base.exits, pos, view, phase)
            )


def test_playbook_exits_need_one_hard_stop() -> None:
    raw = _with_ep_exits(10)
    ep = next(p for p in raw["playbooks"] if p["name"] == "episodic_pivot")
    ep["exits"] = ep["exits"][1:]
    with pytest.raises(Exception, match="invalid strategy config"):
        parse_config(raw)


def test_sma_trail_acts_only_above_the_stop() -> None:
    """With `above_stop`, a close under the SMA exits only while the SMA is above the current
    stop; without it, as before."""
    config = parse_config(_with_ep_exits(10))
    rule = config.exits_for("episodic_pivot")[-1]
    plain = rule.model_copy(update={"above_stop": False})
    pos = exits.PositionState(100, 100, Decimal(100), Decimal(95), Decimal(97))
    below = exits.ExitView(Decimal(96), days_held=5, sma={10: Decimal("96.5")})
    above = exits.ExitView(Decimal(97.5), days_held=5, sma={10: Decimal(98)})
    assert exits.evaluate([rule], pos, below, "close").actions == ()
    assert exits.evaluate([plain], pos, below, "close").actions
    assert exits.evaluate([rule], pos, above, "close").actions


def test_ep_gate_judges_the_playbook_universe() -> None:
    """A pre-open candidate fails `universe` when its effective universe refuses it (here its
    series), and the setup names its failing criterion."""
    config = parse_config(_with_ep_exits(10))
    playbook = config.playbook("episodic_pivot")
    rows = _closes(10_000)
    gap_day, previous = EPOCH + timedelta(days=64), rows[-1].day
    universe = config.universe_for("episodic_pivot")
    assert ep_gate(playbook, frame(rows), gap_day, previous, universe, "EQ") is None
    assert ep_gate(playbook, frame(rows), gap_day, previous, universe, "SM") == "universe"
    fell = frame(_closes(6_000))
    assert ep_gate(playbook, fell, gap_day, previous, universe, "EQ") == "neglect"
    # every hard setup criterion switched off: the setup passes, the universe still judges
    off = [c.model_copy(update={"enabled": False}) for c in playbook.setup.criteria]
    bare = playbook.model_copy(
        update={"setup": playbook.setup.model_copy(update={"criteria": off})}
    )
    assert ep_gate(bare, fell, gap_day, previous, universe, "EQ") is None
    assert ep_gate(bare, fell, gap_day, previous, universe, "SM") == "universe"


def test_override_paths_reach_the_new_fields() -> None:
    """The sweep's variants are plain override paths: a playbook's exits, universe and a new
    trigger criterion parse."""
    from app.hat.backtest.overrides import Overrides, parse_set

    base = parse_config(_seed())
    sets = (
        parse_set('playbooks[episodic_pivot].trigger[+]={"type": "earnings_catalyst"}'),
        parse_set("playbooks[episodic_pivot].trigger[early_volume].params.mode=time_anchored"),
        parse_set("playbooks[episodic_pivot].setup.criteria[neglect].params.direction=up_only"),
        parse_set(
            'playbooks[episodic_pivot].universe=[{"type": "series_in", "params": {"series":'
            ' ["EQ", "BE"]}}, {"type": "min_turnover", "params": {"days": 20, "inr_cr": 10}}]'
        ),
    )
    varied = Overrides(sets=sets).apply_config(base)
    ep = varied.playbook("episodic_pivot")
    assert ep_log.EARNINGS in [i.type for i in ep.trigger]
    assert ep_log.uses_results(varied)
    assert not ep_log.uses_results(base)
    assert ep.universe is not None
    assert len(ep.universe) == 2
    assert varied.exits_for("breakout") == base.exits


def test_ep_funnel_order_and_depth() -> None:
    config = parse_config(_seed())
    raw = copy.deepcopy(_seed())
    ep = next(p for p in raw["playbooks"] if p["name"] == "episodic_pivot")
    ep["trigger"].append({"type": "earnings_catalyst"})
    with_earnings = parse_config(raw).playbook("episodic_pivot")
    assert ep_log.ep_gates(with_earnings) == (
        "gap_scan",
        "gap_up",
        "neglect",
        "earnings_catalyst",
        "early_volume",
        "price_breaks_level",
        "adtr_filter",
        "max_chase",
        "filled",
    )
    assert "earnings_catalyst" not in ep_log.ep_gates(config.playbook("episodic_pivot"))
    order = ep_log.trigger_gates(with_earnings)
    assert ep_log.trigger_depth(order, {"gap_up": True, "earnings_catalyst": False}) == 1
    assert ep_log.trigger_depth(order, dict.fromkeys(order, True)) == 4


def test_ep_record_and_funnel_counts() -> None:
    gates = ("gap_scan", "gap_up", "neglect", "early_volume", "filled")
    a = ep_log.EpRecord(EPOCH, "i", "A", "ep", gates)
    a.passed("gap_up")
    a.failed("already_extended")
    b = ep_log.EpRecord(EPOCH, "j", "B", "ep", gates)
    for gate in gates[1:]:
        b.passed(gate)
    b.failed("late", "filled")  # a failure after the last pass changes nothing passed
    assert (a.failed_gate, a.reached) == ("neglect", "gap_up")
    assert ep_log.funnel([a, b], gates) == {
        "gap_scan": 2,
        "gap_up": 2,
        "neglect": 1,
        "early_volume": 1,
        "filled": 1,
    }


def test_ep_record_catalyst_type() -> None:
    record = ep_log.EpRecord(EPOCH, "i", "A", "ep", ("gap_scan", "filled"))
    record.at_trigger(AT, 1500, Decimal(1000), (_event(2, "0.3", "0.2"),), SINCE)
    assert record.columns["catalyst_type"] == "earnings"
    assert record.columns["sales_yoy_pct"] == pytest.approx(30.0)
    assert record.columns["vol_vs_adv20"] == pytest.approx(1.5)
    assert record.columns["trigger_time"] == "09:40"
    record.at_trigger(AT, None, None, (), SINCE)
    assert record.columns["catalyst_type"] == "none"
    assert ep_log.catalyst_type(None, True) == "other_announcement"


@pytest.mark.parametrize(
    ("rule", "value", "bucket"),
    [
        (ep_log.sales_bucket, 24.99, "lt25"),
        (ep_log.sales_bucket, 25, "25_50"),
        (ep_log.sales_bucket, 100, "gt100"),
        (ep_log.gap_bucket, 15, "15_25"),
        (ep_log.adr_bucket, 3.5, "3_4"),
        (ep_log.adr_bucket, 6, "gt6"),
        (ep_log.range_bucket, 30, "30_60"),
        (ep_log.range_bucket, None, "unknown"),
        (ep_log.time_bucket, "09:45", "0920_0945"),
        (ep_log.time_bucket, "11:00", "0945_1100"),
        (ep_log.time_bucket, "11:05", "after_1100"),
    ],
)
def test_c3_buckets(rule: Callable[[Any], str], value: object, bucket: str) -> None:
    assert rule(value) == bucket


def test_results_events_map_chains_and_sort() -> None:
    """A linked older ISIN's filings count for its chain; growth joins by result id; events are
    oldest first; an instrument outside the run is dropped."""
    chain, old, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    r1, r2 = uuid.uuid4(), uuid.uuid4()
    t = datetime(2024, 5, 2, 12, tzinfo=UTC)
    rows = [
        (r1, t + timedelta(hours=1), date(2024, 3, 31), old),
        (r2, t, date(2024, 3, 31), chain),
        (uuid.uuid4(), t, date(2024, 3, 31), other),
    ]
    growth = {r1: (Decimal("0.3"), None, True)}
    found = results_events(rows, growth, {chain}, {str(old): str(chain)})
    assert list(found) == [str(chain)]
    first, second = found[str(chain)]
    assert first.public_at == t
    assert first.sales_yoy is None
    assert second.sales_yoy == Decimal("0.3")
    assert second.turnaround is True


def test_ep_stock_log_reads_to_the_previous_session() -> None:
    """The logged 63-session return is neglect's (signed, to the previous close); the 126-session
    range needs 126 sessions; a bar on the gap day is never read."""
    rows = _closes(12_000)
    previous = rows[-1].day
    gap_day = Row(previous + timedelta(days=1), 20_000, 13_000, 19_000, 9000)
    log = ep_stock_log(frame([*rows, gap_day]), previous)
    assert log["ret_63d_pct"] == pytest.approx(20.0)
    assert log["range_126d_pct"] is None
    assert log["adr20_pre_gap_pct"] is not None


def test_playbook_universe_refuses_an_eligible_rank() -> None:
    """The eligible RS rank is the percentile among the bot's universe passers, so a playbook
    with its own universe may not read it."""
    raw = _with_ep_exits(10)
    ep = next(p for p in raw["playbooks"] if p["name"] == "episodic_pivot")
    ep["setup"]["criteria"].append(
        {"type": "rs_rank", "mode": "hard", "params": {"basis": "eligible"}}
    )
    with pytest.raises(Exception, match="invalid strategy config"):
        parse_config(raw)
    del ep["universe"]
    assert parse_config(raw).playbook("episodic_pivot").universe is None


def test_reads_results() -> None:
    raw = _seed()
    base = parse_config(raw).playbook("episodic_pivot")
    ep = next(p for p in raw["playbooks"] if p["name"] == "episodic_pivot")
    ep["trigger"].append({"type": "earnings_catalyst"})
    assert reads_results(parse_config(raw).playbook("episodic_pivot"))
    assert not reads_results(base)
    ep["trigger"][-1]["enabled"] = False
    assert not reads_results(parse_config(raw).playbook("episodic_pivot"))


def test_results_digest_text_sees_a_changed_growth() -> None:
    event = _event(2, "0.3", "0.2", False)
    revised = ResultEvent(event.public_at, event.period_end, Decimal("0.31"), event.eps_yoy, False)
    same = ResultEvent(event.public_at, event.period_end, event.sales_yoy, event.eps_yoy, False)
    assert results_digest_text({"a": [event]}) == results_digest_text({"a": [same]})
    assert results_digest_text({"a": [event]}) != results_digest_text({"a": [revised]})

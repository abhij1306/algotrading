"""PBT scan.* (HAT-CRIT-010, HAT-PORT-023, HAT-BOT-052): the nightly scan of
`app.hat.engine.scan` and its publisher `app.hat.engine.watchlist`, against an independent
statement of who may appear on a watchlist."""

from __future__ import annotations

import copy
import uuid
from datetime import date, timedelta
from decimal import Decimal

import polars as pl
import yaml
from hat_engine_support import SEED, TRADE_DAY, seed_engine_world
from hat_risk_support import AT
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.engine import lifecycle, scan, watchlist
from app.hat.engine.portfolio import service
from app.hat.engine.scan import ScanInputs
from app.hat.engine.strategy.config import StrategyConfig, parse_config
from app.hat.engine.strategy.versions import seed_text
from app.hat.models import HatEvent, HatStrategyVersion, HatWatchlistEntry

D = Decimal
AS_OF = date(2026, 10, 20)
MIN_PRICE = 50
MIN_RS = 50


def make_config(*, with_ep: bool = False) -> StrategyConfig:
    raw = yaml.safe_load(seed_text(SEED))
    raw["universe"] = [{"type": "min_price", "params": {"inr": MIN_PRICE}}]
    raw["regime"] = {
        "index": "NIFTY 500",
        "rule": "close_above_sma_and_rising",
        "params": {"sma": 3, "rising_days": 1},
    }
    raw["setup"] = {
        "threshold": 0.0,
        "criteria": [
            {"type": "rs_rank", "mode": "hard", "params": {"min_percentile": MIN_RS}},
            {
                "type": "rs_rank",
                "mode": "score",
                "weight": 1,
                "params": {"score_from": MIN_RS, "score_full": 100},
            },
        ],
    }
    return parse_config(copy.deepcopy(raw))


CONFIG = make_config()
RISING = [D(v) for v in (100, 101, 102, 103, 104, 110)]
FALLING = [D(v) for v in (110, 109, 108, 107, 106, 90)]
IDS = [str(uuid.UUID(int=n + 1)) for n in range(8)]


@st.composite
def worlds(draw: st.DrawFn) -> tuple[ScanInputs, dict[str, tuple[int, int]]]:
    """Instruments with a close price and an RS percentile; some are members, some have no bars."""
    chosen = draw(st.lists(st.sampled_from(IDS), unique=True, min_size=0, max_size=8))
    quotes = {i: (draw(st.integers(10, 200)), draw(st.integers(0, 100))) for i in chosen}
    with_bars = [i for i in chosen if draw(st.integers(0, 5)) > 0]
    members = [i for i in chosen if draw(st.booleans())]
    rows = []
    for i in with_bars:
        for n in range(6):
            day = AS_OF - timedelta(days=5 - n)
            close = float(quotes[i][0])
            rows.append((i, day, close + 1, close - 1, close, 1_000_000.0, None))
    bars = pl.DataFrame(
        rows,
        schema={
            "instrument_id": pl.Utf8,
            "date": pl.Date,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "traded_value": pl.Float64,
        },
        orient="row",
    )
    features = pl.DataFrame(
        {"instrument_id": chosen, "rs_pct": [float(quotes[i][1]) for i in chosen]},
        schema={"instrument_id": pl.Utf8, "rs_pct": pl.Float64},
    )
    gate = draw(st.booleans())
    inputs = ScanInputs(
        as_of=AS_OF,
        members=members,
        bars=bars,
        regime_closes=RISING if gate else FALLING,
        features=features,
        series=dict.fromkeys(chosen, "EQ"),
    )
    return inputs, {i: quotes[i] for i in with_bars}


def expected_ids(inputs: ScanInputs, quotes: dict[str, tuple[int, int]]) -> set[str]:
    gate = inputs.regime_closes is RISING
    return {
        i
        for i in inputs.members
        if gate and i in quotes and quotes[i][0] >= MIN_PRICE and quotes[i][1] >= MIN_RS
    }


@given(worlds())
def test_watchlist_gates(world: tuple[ScanInputs, dict[str, tuple[int, int]]]) -> None:
    """HAT-CRIT-010: a watchlist row exists exactly for the members that pass the universe stage,
    the regime gate and the setup, in that order; its recorded stages are those three, all
    passed; a closed gate gives no rows; rows are best score first; bars dated after the scan
    date change nothing. Harm: a symbol shown that failed a stage sends the owner a trade the
    strategy would never take."""
    inputs, quotes = world
    result = scan.scan_bot(CONFIG, inputs)
    assert {r.instrument_id for r in result.rows} == expected_ids(inputs, quotes)
    assert result.gate_on == (inputs.regime_closes is RISING)
    if not result.gate_on:
        assert result.rows == () and result.evaluated == 0
    for row in result.rows:
        stages = row.criteria_results["stages"]
        assert [s["stage"] for s in stages] == ["universe", "regime", "setup"]
        assert all(s["passed"] for s in stages)
    scores = [r.score for r in result.rows]
    assert scores == sorted(scores, reverse=True)
    later = inputs.bars.vstack(
        inputs.bars.with_columns(pl.col("date") + pl.duration(days=30), pl.col("close") * 0.01)
    )
    assert scan.scan_bot(CONFIG, ScanInputs(**{**inputs.__dict__, "bars": later})) == result


@given(worlds(), st.data())
def test_no_row_for_removed(
    world: tuple[ScanInputs, dict[str, tuple[int, int]]], data: st.DataObject
) -> None:
    """HAT-PORT-023: an instrument removed from the universe produces no watchlist row, and the
    rows of the others are unchanged. Harm: a removed stock keeps being proposed for entry
    (leaving the universe stops entries only, never an open position)."""
    inputs, _ = world
    members = list(inputs.members)
    removed = set(data.draw(st.lists(st.sampled_from(members), unique=True))) if members else set()
    kept = ScanInputs(**{**inputs.__dict__, "members": [m for m in members if m not in removed]})
    before = scan.scan_bot(CONFIG, inputs)
    after = scan.scan_bot(CONFIG, kept)
    assert not {r.instrument_id for r in after.rows} & removed
    assert after.rows == tuple(r for r in before.rows if r.instrument_id not in removed)


@given(worlds())
def test_episodic_pivot_not_scanned_overnight(
    world: tuple[ScanInputs, dict[str, tuple[int, int]]],
) -> None:
    """HAT-BOT-052: the episodic-pivot playbook never produces a nightly row; its setup is built
    live from the pre-open scan."""
    inputs, _ = world
    assert all(r.playbook != scan.EP_PLAYBOOK for r in scan.scan_bot(CONFIG, inputs).rows)


@settings(max_examples=25)
@given(worlds(), st.data())
def test_publish_once_per_day(
    world: tuple[ScanInputs, dict[str, tuple[int, int]]], data: st.DataObject
) -> None:
    """HAT-CRIT-010, HAT-PORT-023: `scan_and_publish` writes the scan's rows as Watchlisted
    entries and one `watchlist.published` event, uses the bot's own universe (the caller's
    member list is replaced), and a second call for the same bot and day writes nothing."""
    inputs, quotes = world
    engine = memory_engine()
    with Session(engine) as session:
        w = seed_engine_world(session, instruments=0)
        ids = [uuid.UUID(i) for i in IDS]
        members = set(data.draw(st.lists(st.sampled_from(ids), unique=True, max_size=len(ids))))
        service.apply_membership(session, w.universe_id, members, TRADE_DAY - timedelta(days=30))
        version = session.get(HatStrategyVersion, w.version_id)
        version.config = CONFIG.model_dump(mode="json", by_alias=True)
        session.flush()
        first = watchlist.scan_and_publish(session, w.bot_id, inputs, TRADE_DAY, AT)
        entries = session.scalars(select(HatWatchlistEntry)).all()
        assert first.rows == len(entries)
        own = ScanInputs(**{**inputs.__dict__, "members": [str(m) for m in members]})
        assert {str(e.instrument_id) for e in entries} == expected_ids(own, quotes)
        assert all(e.status == lifecycle.WATCHLISTED and e.trade_date == TRADE_DAY for e in entries)
        again = watchlist.scan_and_publish(session, w.bot_id, inputs, TRADE_DAY, AT)
        assert again.skipped == "already_published" and again.rows == 0
        assert session.scalar(select(func.count()).select_from(HatWatchlistEntry)) == len(entries)
        published = session.scalar(
            select(func.count())
            .select_from(HatEvent)
            .where(HatEvent.event_type == watchlist.EVENT_PUBLISHED)
        )
        assert published == 1

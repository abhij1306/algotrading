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
from app.hat.engine.strategy.config import DEFAULT_PLAYBOOK, StrategyConfig, parse_config
from app.hat.engine.strategy.versions import seed_text
from app.hat.models import HatBot, HatEvent, HatStrategyVersion, HatWatchlistEntry

D = Decimal
AS_OF = date(2026, 10, 20)
MIN_PRICE = 50
MIN_RS = 50


def make_config(
    *, with_ep: bool = False, setup: list[dict] | None = None, universe: list[dict] | None = None
) -> StrategyConfig:
    raw = yaml.safe_load(seed_text(SEED))
    raw["universe"] = universe or [{"type": "min_price", "params": {"inr": MIN_PRICE}}]
    raw["regime"] = {
        "index": "NIFTY 500",
        "rule": "close_above_sma_and_rising",
        "params": {"sma": 3, "rising_days": 1},
    }
    raw["setup"] = {
        "threshold": 0.0,
        "criteria": [
            *(setup or [{"type": "rs_rank", "mode": "hard", "params": {"min_percentile": MIN_RS}}]),
            {
                "type": "rs_rank",
                "mode": "score",
                "weight": 1,
                "params": {"score_from": MIN_RS, "score_full": 100},
            },
        ],
    }
    if with_ep:
        # An episodic-pivot playbook with the same setup: were it scanned, it would add rows.
        inline = {k: raw.pop(k) for k in ("setup", "trigger", "entry_filters", "stop")}
        raw["playbooks"] = [
            {"name": DEFAULT_PLAYBOOK, **inline},
            {"name": scan.EP_PLAYBOOK, **copy.deepcopy(inline)},
        ]
    return parse_config(copy.deepcopy(raw))


CONFIG = make_config()
EP_CONFIG = make_config(with_ep=True)
# No stage reads a bar: only the scan's own "traded on the scan date" rule keeps stale members out.
NO_BAR_CONFIG = make_config(universe=[{"type": "series_in", "params": {"series": ["EQ"]}}])
TOP_PCT = 50
RANK_CONFIG = make_config(
    setup=[
        {
            "type": "momentum_rank",
            "mode": "hard",
            "params": {"windows": [21], "top_pct": TOP_PCT, "match": "any"},
        }
    ]
)
RISING = [D(v) for v in (100, 101, 102, 103, 104, 110)]
FALLING = [D(v) for v in (110, 109, 108, 107, 106, 90)]
IDS = [str(uuid.UUID(int=n + 1)) for n in range(8)]


@st.composite
def worlds(draw: st.DrawFn) -> tuple[ScanInputs, dict[str, tuple[int, int]]]:
    """Instruments with a close price, an RS percentile and a 21-day return; some are members,
    some have no bars, and some last traded before the scan date (stale: never evaluated)."""
    chosen = draw(st.lists(st.sampled_from(IDS), unique=True, min_size=0, max_size=8))
    quotes = {i: (draw(st.integers(10, 200)), draw(st.integers(0, 100))) for i in chosen}
    with_bars = [i for i in chosen if draw(st.integers(0, 5)) > 0]
    stale = {i for i in with_bars if draw(st.integers(0, 4)) == 0}
    members = [i for i in chosen if draw(st.booleans())]
    rows = []
    for i in with_bars:
        last = AS_OF - timedelta(days=1 if i in stale else 0)
        for n in range(6):
            day = last - timedelta(days=5 - n)
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
        {
            "instrument_id": chosen,
            "rs_pct": [float(quotes[i][1]) for i in chosen],
            "ret21": [float(draw(st.integers(-50, 300))) for _ in chosen],
        },
        schema={"instrument_id": pl.Utf8, "rs_pct": pl.Float64, "ret21": pl.Float64},
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
    return inputs, {i: quotes[i] for i in with_bars if i not in stale}


def expected_ids(inputs: ScanInputs, quotes: dict[str, tuple[int, int]]) -> set[str]:
    gate = inputs.regime_closes is RISING
    return {
        i
        for i in inputs.members
        if gate and i in quotes and quotes[i][0] >= MIN_PRICE and quotes[i][1] >= MIN_RS
    }


@given(worlds())
def test_watchlist_gates(world: tuple[ScanInputs, dict[str, tuple[int, int]]]) -> None:
    """HAT-CRIT-010: a watchlist row exists exactly for the members that traded on the scan date
    and pass the universe stage, the regime gate and the setup, in that order; its recorded
    stages are those three, all passed; a closed gate gives no rows; rows are best score first;
    bars dated after the scan date change nothing. Harm: a symbol shown that failed a stage, or
    judged on stale bars, sends the owner a trade the strategy would never take."""
    inputs, quotes = world
    result = scan.scan_bot(CONFIG, inputs)
    assert {r.instrument_id for r in result.rows} == expected_ids(inputs, quotes)
    assert result.gate_on == (inputs.regime_closes is RISING)
    if not result.gate_on:
        assert result.rows == ()
        assert result.evaluated == 0
    else:
        assert result.evaluated == len(set(inputs.members) & set(quotes))
    no_bar = scan.scan_bot(NO_BAR_CONFIG, inputs)
    assert {r.instrument_id for r in no_bar.rows} <= set(inputs.members) & set(quotes)
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
    """HAT-BOT-052: the episodic-pivot playbook never produces a nightly row, though its setup
    here is the same as the scanned playbook's; its setup is built live from the pre-open scan."""
    inputs, _ = world
    rows = scan.scan_bot(EP_CONFIG, inputs).rows
    assert all(r.playbook != scan.EP_PLAYBOOK for r in rows)
    assert [r.instrument_id for r in rows] == [
        r.instrument_id for r in scan.scan_bot(CONFIG, inputs).rows
    ]


@given(worlds(), st.lists(st.integers(-50, 1000), max_size=4))
def test_rank_within_members(
    world: tuple[ScanInputs, dict[str, tuple[int, int]]], outsiders: list[int]
) -> None:
    """HAT-CRIT-110, HAT-PORT-021: a universe rank (`momentum_rank`) is taken among the bot's
    members only: feature rows of instruments outside the universe, however strong, change no
    row. Harm: another universe's leaders push this bot's stocks out of its top band (or let
    weak ones in)."""
    inputs, _ = world
    assert inputs.features is not None
    members = {str(m) for m in inputs.members}
    own = inputs.features.filter(pl.col("instrument_id").is_in(sorted(members)))
    extra = pl.DataFrame(
        {
            "instrument_id": [str(uuid.UUID(int=1000 + n)) for n in range(len(outsiders))],
            "rs_pct": [99.0] * len(outsiders),
            "ret21": [float(v) for v in outsiders],
        },
        schema=inputs.features.schema,
    )
    crowded = inputs.features.vstack(extra)
    alone = scan.scan_bot(RANK_CONFIG, ScanInputs(**{**inputs.__dict__, "features": own}))
    assert (
        scan.scan_bot(RANK_CONFIG, ScanInputs(**{**inputs.__dict__, "features": crowded})) == alone
    )


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
        assert all(e.status == lifecycle.WATCHLISTED for e in entries)
        assert all(e.trade_date == TRADE_DAY for e in entries)
        again = watchlist.scan_and_publish(session, w.bot_id, inputs, TRADE_DAY, AT)
        assert again.skipped == "already_published"
        assert again.rows == 0
        assert session.scalar(select(func.count()).select_from(HatWatchlistEntry)) == len(entries)
        published = session.scalar(
            select(func.count())
            .select_from(HatEvent)
            .where(HatEvent.event_type == watchlist.EVENT_PUBLISHED)
        )
        assert published == 1


@settings(max_examples=15)
@given(worlds())
def test_no_universe_publishes_nothing(
    world: tuple[ScanInputs, dict[str, tuple[int, int]]],
) -> None:
    """HAT-CRIT-010 (fail closed): a bot with no universe publishes nothing and records nothing,
    so the same day's run after the universe is assigned still publishes. Harm: an empty
    "published" watchlist blocks that day's real one."""
    inputs, _ = world
    with Session(memory_engine()) as session:
        w = seed_engine_world(session, instruments=0)
        bot = session.get(HatBot, w.bot_id)
        assigned, bot.universe_version_id = bot.universe_version_id, None
        session.flush()
        first = watchlist.scan_and_publish(session, w.bot_id, inputs, TRADE_DAY, AT)
        assert first.skipped == "no_universe"
        assert (
            session.scalar(
                select(func.count())
                .select_from(HatEvent)
                .where(HatEvent.event_type == watchlist.EVENT_PUBLISHED)
            )
            == 0
        )
        bot.universe_version_id = assigned
        session.flush()
        later = watchlist.scan_and_publish(session, w.bot_id, inputs, TRADE_DAY, AT)
        assert later.skipped is None

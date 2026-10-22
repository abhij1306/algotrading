"""PBT dashboard_routes.* (HAT-DEC-T3, HAT-OPS-072): the read-only `/api/hat/*` routes. Every
answer validates against its response model, money and weights leave as decimal strings equal to
the stored values, the `limit` of the list routes is bounded (anything outside 1..200 is a 422),
lists hold exactly the rows they promise in their order, and a failed read is a fixed message
that never carries database text. Mini app with only the dashboard router over in-memory SQLite.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from hat_dashboard_support import API, NOW, TODAY, dashboard_client, instant
from hat_fyers_support import captured_logs
from hat_support import memory_engine
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.hat.api import dashboard
from app.hat.models import (
    HatAllocationDaily,
    HatBot,
    HatEvent,
    HatInstrument,
    HatPosition,
    HatRegimeState,
    HatSignal,
    HatStrategy,
    HatWatchlistEntry,
)

LIST_ROUTES = ("signals", "regime", "events")
ALL_ROUTES = (
    "status",
    "bots",
    "watchlist",
    "signals",
    "positions",
    "allocations",
    "regime",
    "events",
)
MODELS = {
    "status": dashboard.StatusResponse,
    "bots": list[dashboard.BotView],
    "watchlist": list[dashboard.WatchlistView],
    "signals": list[dashboard.SignalView],
    "positions": list[dashboard.PositionView],
    "allocations": dashboard.AllocationsResponse,
    "regime": list[dashboard.RegimeView],
    "events": list[dashboard.EventView],
}
BOT = uuid.UUID(int=0xB1)
INR = "INR"
SHADOW = "shadow"

money = st.decimals(min_value=Decimal("0.05"), max_value=Decimal(10**6), places=2)
ratios = st.decimals(min_value=Decimal(0), max_value=Decimal(3), places=4)


def _engine(*tables: type) -> Engine:
    """The routes' database: one bot named momentum, plus the tables a property fills."""
    engine = memory_engine(HatStrategy, HatBot, HatInstrument, *tables)
    with Session(engine) as session:
        session.add(
            HatStrategy(
                id=uuid.UUID(int=0x51),
                user_id=uuid.uuid4(),
                market_id=uuid.uuid4(),
                name="momentum",
                persona="trader",
            )
        )
        session.add(
            HatBot(
                id=BOT,
                user_id=uuid.uuid4(),
                strategy_id=uuid.UUID(int=0x51),
                broker_account_id=uuid.uuid4(),
                execution_mode=SHADOW,
                status="active",
                lifecycle_state="live",
                risk_cluster="c1",
            )
        )
        session.commit()
    return engine


def _instrument(session: Session, n: int) -> uuid.UUID:
    instrument = uuid.UUID(int=0x1000 + n)
    if session.get(HatInstrument, instrument) is None:
        session.add(
            HatInstrument(
                id=instrument,
                market_id=uuid.uuid4(),
                isin=f"INE{n:09d}",
                series="EQ",
                symbol=f"SYM{n}",
                name=f"Company {n}",
                face_value=Decimal(1),
                active=True,
            )
        )
    return instrument


def _get(client: TestClient, route: str, **params: object) -> object:
    response = client.get(f"{API}/{route}", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    TypeAdapter(MODELS[route]).validate_python(body)
    return body


def _is_decimal_text(value: object) -> bool:
    return isinstance(value, str) and Decimal(value).is_finite()


# --- positions --------------------------------------------------------------------------------

positions = st.lists(
    st.tuples(money, money, st.integers(0, 500), st.booleans(), st.booleans()),
    max_size=6,
)


@given(rows=positions)
def test_positions_are_the_open_rows_as_decimal_strings(
    rows: list[tuple[Decimal, Decimal, int, bool, bool]],
) -> None:
    """`/positions` lists exactly the positions with no close time and a quantity left, oldest
    first, with every price a decimal string equal to the stored one. Harm: the dashboard hides
    an open position, shows a closed one, or rounds a stop."""
    engine = _engine(HatPosition)
    expected = []
    with Session(engine) as session:
        for n, (entry, gap, qty, closed, virtual) in enumerate(rows):
            stop = max(entry - gap, Decimal("0.01"))
            opened = NOW - timedelta(days=len(rows) - n)
            session.add(
                HatPosition(
                    id=uuid.UUID(int=0x2000 + n),
                    bot_id=BOT,
                    signal_id=uuid.uuid4(),
                    instrument_id=_instrument(session, n),
                    is_virtual=virtual,
                    qty_open=qty,
                    avg_entry=entry,
                    initial_stop=stop,
                    current_stop=stop,
                    currency=INR,
                    opened_at=opened,
                    closed_at=NOW if closed else None,
                    state="Open",
                    rules_fired=[],
                )
            )
            if not closed and qty > 0:
                expected.append((str(uuid.UUID(int=0x2000 + n)), f"SYM{n}", qty, entry, stop))
        session.commit()
    body = _get(dashboard_client(engine), "positions")
    engine.dispose()
    shown = [
        (p["id"], p["symbol"], p["qty_open"], Decimal(p["avg_entry"]), Decimal(p["current_stop"]))
        for p in body
    ]
    assert shown == expected
    assert all(_is_decimal_text(p["avg_entry"]) for p in body)
    assert all(p["bot"] == "momentum" for p in body)


# --- allocations ------------------------------------------------------------------------------

allocations = st.lists(
    st.tuples(st.integers(0, 5), st.integers(0, 3), ratios, money),
    max_size=8,
    unique_by=lambda row: row[:2],
)


@given(rows=allocations)
def test_allocations_are_the_latest_day_exactly(
    rows: list[tuple[int, int, Decimal, Decimal]],
) -> None:
    """`/allocations` shows the rows of the latest allocation day only, each weight and the
    capital a decimal string equal to the stored value. Harm: the user reads yesterday's sleeve
    sizes, or a weight rounded through a float."""
    engine = _engine(HatAllocationDaily)
    with Session(engine) as session:
        for back, bot, weight, capital in rows:
            session.add(
                HatAllocationDaily(
                    trade_date=TODAY - timedelta(days=back),
                    bot_id=uuid.UUID(int=0xC0 + bot),
                    base_weight=weight,
                    regime_mult=weight,
                    health_mult=Decimal(1),
                    final_weight=weight,
                    capital=capital,
                    risk_pct=Decimal("0.005"),
                    currency=INR,
                )
            )
        session.commit()
    body = _get(dashboard_client(engine), "allocations")
    engine.dispose()
    latest = min((back for back, *_ in rows), default=None)
    day = None if latest is None else (TODAY - timedelta(days=latest)).isoformat()
    assert body["trade_date"] == day
    expected = sorted(
        (str(uuid.UUID(int=0xC0 + bot)), weight, capital)
        for back, bot, weight, capital in rows
        if back == latest
    )
    shown = [(r["bot_id"], Decimal(r["final_weight"]), Decimal(r["capital"])) for r in body["rows"]]
    assert shown == expected
    assert all(_is_decimal_text(r["capital"]) for r in body["rows"])


# --- watchlist --------------------------------------------------------------------------------

watchlists = st.lists(
    st.tuples(st.integers(-2, 4), st.integers(0, 5), ratios), max_size=10, unique_by=lambda r: r[:2]
)


@given(rows=watchlists)
def test_watchlist_is_the_latest_day_on_or_before_today(
    rows: list[tuple[int, int, Decimal]],
) -> None:
    """`/watchlist` shows, per bot, its latest watchlist day on or before today (a future day is
    never shown), best score first, each score a decimal string. Harm: tomorrow's or a stale
    list is read as today's."""
    engine = _engine(HatWatchlistEntry)
    with Session(engine) as session:
        for back, n, score in rows:
            session.add(
                HatWatchlistEntry(
                    bot_id=BOT,
                    strategy_version_id=uuid.uuid4(),
                    playbook="default",
                    instrument_id=_instrument(session, n),
                    trade_date=TODAY - timedelta(days=back),
                    score=score,
                    criteria_results={},
                    status="watching",
                )
            )
        session.commit()
    body = _get(dashboard_client(engine), "watchlist")
    engine.dispose()
    past = [row for row in rows if row[0] >= 0]
    latest = min((back for back, *_ in past), default=None)
    (view,) = body
    expected_day = None if latest is None else (TODAY - timedelta(days=latest)).isoformat()
    assert view["trade_date"] == expected_day
    expected = sorted(
        ((f"SYM{n}", score) for back, n, score in past if back == latest),
        key=lambda item: (-item[1], item[0]),
    )
    assert [(e["symbol"], Decimal(e["score"])) for e in view["entries"]] == expected
    assert all(_is_decimal_text(e["score"]) for e in view["entries"])


# --- the list routes and their limit ----------------------------------------------------------


def _seed_lists(session: Session, count: int) -> None:
    """`count` signals, regime days and events, the n-th one newer than the (n-1)-th."""
    model = uuid.uuid4()
    for n in range(count):
        at = NOW - timedelta(minutes=count - n)
        session.add(
            HatSignal(
                id=uuid.UUID(int=0x3000 + n),
                bot_id=BOT,
                strategy_version_id=uuid.uuid4(),
                risk_policy_version_id=uuid.uuid4(),
                playbook="default",
                instrument_id=_instrument(session, n % 3),
                trade_date=TODAY - timedelta(days=count - n),
                triggered_at=at,
                trigger_price=Decimal("101.25"),
                stop_price=Decimal("97.5"),
                qty=n,
                risk_amount=Decimal("37.5") * n,
                currency=INR,
                mode=SHADOW,
                state="Triggered",
                risk_decision={},
            )
        )
        session.add(
            HatRegimeState(
                trade_date=TODAY - timedelta(days=count - n),
                market_id=model,
                model_version_id=model,
                state="bull_quiet",
                inputs={},
                confirmed=n % 2 == 0,
            )
        )
        session.add(
            HatEvent(
                id=n + 1,
                occurred_at=at,
                user_id=uuid.uuid4(),
                aggregate_type="signal",
                aggregate_id=uuid.UUID(int=0x3000 + n),
                correlation_id=uuid.UUID(int=0x3000 + n),
                event_type="signal.triggered",
                actor="engine",
                payload={"n": n},
            )
        )
    session.commit()


def _newest_first(route: str, body: list[dict]) -> list[datetime | date | int]:
    if route == "signals":
        return [instant(s["triggered_at"]) for s in body]
    if route == "regime":
        return [date.fromisoformat(r["trade_date"]) for r in body]
    return [e["id"] for e in body]


@given(count=st.integers(0, 12), limit=st.one_of(st.none(), st.integers(1, 200)))
def test_lists_hold_the_newest_rows_up_to_the_limit(count: int, limit: int | None) -> None:
    """`/signals`, `/regime` and `/events` return min(rows, limit) rows (limit 50 when not
    given), newest first, and every signal amount is a decimal string. Harm: the dashboard
    shows a stale slice or an unbounded read loads the whole event log."""
    engine = _engine(HatSignal, HatRegimeState, HatEvent)
    with Session(engine) as session:
        _seed_lists(session, count)
    params = {} if limit is None else {"limit": limit}
    client = dashboard_client(engine)
    for route in LIST_ROUTES:
        body = _get(client, route, **params)
        assert isinstance(body, list)
        assert len(body) == min(count, limit or dashboard.DEFAULT_LIMIT)
        order = _newest_first(route, body)
        assert order == sorted(order, reverse=True)
    signals = _get(client, "signals")
    engine.dispose()
    assert all(_is_decimal_text(s["risk_amount"]) for s in signals)  # type: ignore[union-attr]
    for signal in signals:  # type: ignore[union-attr]
        assert Decimal(signal["risk_amount"]) == Decimal("37.5") * signal["qty"]


@given(
    route=st.sampled_from(LIST_ROUTES),
    limit=st.one_of(
        st.integers(max_value=0),
        st.integers(min_value=dashboard.MAX_LIMIT + 1),
        st.from_regex(r"[a-z.]{1,5}|[0-9]{1,3}\.[1-9]", fullmatch=True),
    ),
)
def test_a_limit_out_of_bounds_is_refused(route: str, limit: int | str) -> None:
    """A `limit` below 1, above 200 or not an integer is a 422 and reads nothing. Harm: one
    request reads the whole event log."""
    engine = _engine()
    response = dashboard_client(engine).get(f"{API}/{route}", params={"limit": limit})
    engine.dispose()
    assert response.status_code == 422


# --- failures ---------------------------------------------------------------------------------


@given(route=st.sampled_from(ALL_ROUTES))
def test_a_failed_read_is_a_fixed_message(route: str) -> None:
    """When the database cannot be read (here: no HAT tables) every route answers 503 with the
    fixed message, and neither the body nor the response carries the driver's text. Harm: SQL,
    table names or credentials leak to the browser."""
    engine = memory_engine(HatStrategy)
    with captured_logs():
        response = dashboard_client(engine).get(f"{API}/{route}")
    engine.dispose()
    assert response.status_code == 503
    assert response.json() == {"detail": dashboard.DB_ERROR}
    for text in ("no such table", "hat_", "SELECT", "sqlite"):
        assert text not in response.text

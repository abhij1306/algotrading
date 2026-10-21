"""PBT backtest.* (09, HAT-BT-010..061): the day loop of `app.hat.engine.backtest` on generated
markets (random walks on a paise grid, `hat_backtest_support`), against rules written out
independently: the live scan run directly, the fill proxies, R and money identities, and the
version and run rules of `publish_version`.

A run takes about a second, so the run-level properties use fewer examples than the profile."""

import json
import math
import uuid
from datetime import date
from decimal import Decimal

import hat_backtest_support as s
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hat_support import memory_engine
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from app.hat.backtest.fills import entry_fill, stop_fill
from app.hat.engine.backtest import (
    KNOWN_GAPS,
    BacktestResult,
    MarketData,
    TradeRow,
    compute_metrics,
    run_backtest,
    trades_parquet,
)
from app.hat.engine.portfolio.universe import Interval
from app.hat.engine.strategy.versions import VersionError, publish_version, seed_default_strategies
from app.hat.models import HatBacktestRun, HatStrategyVersion
from app.hat.schema import seed_hat_reference

D = Decimal
SESSIONS = 34  # 21 warm-up sessions, then 13 simulated ones
RUNS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
CLOSE_EXITS = {"close_below_sma", "time_stop", "partial_after_days", "trailing_swing_low"}
PRICE = st.integers(100, 100_000).map(lambda n: D(n) / 100)  # 1.00 .. 1000.00 on the paise grid
DAYS = s.sessions(SESSIONS)


def _run(data: MarketData, **kw: object) -> BacktestResult:
    return run_backtest(s.spec(s.config(), data.days, **kw), data)  # type: ignore[arg-type]


def _bar(data: MarketData, instrument_id: str, day: date) -> tuple[D, D, D, D]:
    row = data.bars.filter(
        (data.bars["instrument_id"] == instrument_id) & (data.bars["date"] == day)
    ).row(0, named=True)
    return tuple(D(repr(row[k])).quantize(D("0.0001")) for k in ("open", "high", "low", "close"))  # type: ignore[return-value]


def _watch_key(row: object) -> tuple:
    return (row.playbook, str(row.instrument_id), row.pivot, row.planned_stop)  # type: ignore[attr-defined]


def _assert_parity(result: BacktestResult, data: MarketData, day: date) -> None:
    """The run's watchlist for `day` equals the live scan of the session before it."""
    prev = data.days[data.days.index(day) - 1]
    live = sorted(s.scan_watch(s.config(), data, prev), key=_watch_key)
    ran = sorted((w for w in result.watchlists if w.trade_date == day), key=_watch_key)
    assert [_watch_key(w) for w in ran] == [_watch_key(w) for w in live]
    for got, want in zip(ran, live, strict=True):
        assert math.isclose(float(got.score), want.score, abs_tol=1e-6)


# --- HAT-BT-010 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.integers(s.WARM_UP, SESSIONS - 1))
@RUNS
def test_scan_parity(paths: dict, n: int) -> None:
    """The watchlist the loop publishes for each simulated date equals the live nightly scan
    (`scan_bot`) of the previous session for the same version, universe and inputs."""
    data = s.market(paths, DAYS)
    result = _run(data)
    _assert_parity(result, data, DAYS[n])


# --- HAT-BT-011 -------------------------------------------------------------------------------


def _upto(result: BacktestResult, day: date) -> dict[str, list]:
    return {
        "watch": [w for w in result.watchlists if w.trade_date <= day],
        "signals": [x for x in result.signals if x.trade_date <= day],
        "fills": [f for f in result.fills if f.day <= day],
        "trades": [t for t in result.trades if t.exit_date <= day],
        "equity": [e for e in result.equity if e[0] <= day],
    }


@given(
    s.paths(SESSIONS),
    s.paths(SESSIONS),
    st.integers(s.WARM_UP, SESSIONS - 2),
    st.lists(st.integers(900, 1100), min_size=SESSIONS, max_size=SESSIONS),
)
@RUNS
def test_no_lookahead(base: dict, other: dict, cut: int, index: list[int]) -> None:
    """Changing every bar and index close dated after d leaves every decision on or before d
    (watchlists, signals, fills, closed trades, equity) unchanged."""
    day = DAYS[cut]
    mutated = {i: path[: cut + 1] + other.get(i, path)[cut + 1 :] for i, path in base.items()}
    closes = [1000.0 + n for n in range(SESSIONS)]
    changed = closes[: cut + 1] + [float(c) for c in index[cut + 1 :]]
    first = _run(s.market(base, DAYS, index=closes))
    second = _run(s.market(mutated, DAYS, index=changed))
    assert _upto(first, day) == _upto(second, day)


# --- HAT-BT-012 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.integers(s.WARM_UP + 1, SESSIONS - 1), st.booleans())
@RUNS
def test_survivorship_free(paths: dict, gone: int, removed: bool) -> None:
    """An instrument that stops trading at session x is a member, and scanned, before x; no
    fill of it is dated after its last traded bar, and every position in it is closed no later
    than that bar."""
    victim = s.instrument(0)
    cut = {i: (p[:gone] if i == victim else p) for i, p in paths.items()}
    last = DAYS[gone - 1]
    members = [
        Interval(uuid.UUID(i), DAYS[0], DAYS[gone] if (i == victim and removed) else None)
        for i in sorted(cut)
    ]
    data = s.market(cut, DAYS, members=members, last_traded={victim: last})
    result = _run(data)
    assert all(f.day <= last for f in result.fills if f.instrument_id == victim)
    assert all(t.exit_date <= last for t in result.trades if t.instrument_id == victim)
    for n in range(s.WARM_UP, gone):
        _assert_parity(result, data, DAYS[n])


# --- HAT-BT-013 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_version_pinned(paths: dict) -> None:
    """Every simulated signal and trade carries the run's strategy and risk-policy versions."""
    result = _run(s.market(paths, DAYS))
    pinned = (s.VERSION, s.policy_version().version_id)
    assert all((x.strategy_version_id, x.risk_policy_version_id) == pinned for x in result.signals)
    assert all((t.strategy_version_id, t.risk_policy_version_id) == pinned for t in result.trades)
    assert result.metrics["strategy_version_id"] == str(s.VERSION)
    assert result.metrics["risk_policy_version_id"] == str(pinned[1])


# --- HAT-BT-020 / 021 -------------------------------------------------------------------------


@given(PRICE, PRICE, PRICE)
@example(D(10), D(12), D(10))
@example(D(10), D(12), D(12))
def test_entry_fill_proxy(open_: D, high: D, level: D) -> None:
    """An entry fills at the level when the open is at or below it and the high reaches it, at
    the open when the day opens above it, and not at all when the high stays below it."""
    high = max(high, open_)
    expected = open_ if open_ > level else (level if level <= high else None)
    assert entry_fill(open_, high, level) == expected


@given(s.paths(SESSIONS), st.sampled_from([D("0.5"), D(1), D(5)]))
@RUNS
def test_entry_fill_proxy_chase(paths: dict, chase: D) -> None:
    """In a run, every opened entry filled at the proxy price of its watchlist level and within
    max chase of it: a gap-over beyond level x (1 + pct/100) is never bought. The generated
    opens gap up by at most 3%, so the chase is drawn below that too."""
    data = s.market(paths, DAYS)
    cfg = s.config(chase=float(chase))
    result = run_backtest(s.spec(cfg, DAYS, fills=s.zero_fills()), data)
    levels = {(w.trade_date, w.instrument_id): w.pivot for w in result.watchlists}
    for signal in result.signals:
        if signal.state != "Open":
            continue
        level = levels[(signal.trade_date, signal.instrument_id)]
        open_, high, _, _ = _bar(data, signal.instrument_id, signal.trade_date)
        assert signal.trigger_price == entry_fill(open_, high, level)
        assert signal.trigger_price <= level * (1 + chase / 100)


@given(PRICE, PRICE, PRICE)
@example(D(10), D(9), D(9))
@example(D(8), D(7), D(9))
def test_exit_fill_proxy(open_: D, low: D, stop: D) -> None:
    """A stop fills at min(open, stop) when the day reaches it (a gap through fills at the open)
    and is never better than the stop; a day that stays above it does not fill."""
    low = min(low, open_)
    filled = stop_fill(open_, low, stop)
    if low <= stop:
        assert filled == min(open_, stop)
        assert filled <= stop
    else:
        assert filled is None


@given(s.paths(SESSIONS))
@RUNS
def test_exit_fill_proxy_close(paths: dict) -> None:
    """In a run without slippage, a trade closed by a close-based rule sold its last shares at
    that day's official close, and the 15:25 difference is recorded as a known gap."""
    data = s.market(paths, DAYS)
    result = _run(data, fills=s.zero_fills())
    assert "close_exits_at_official_close" in result.metrics["known_gaps"]
    for trade in result.trades:
        if trade.exit_reason not in CLOSE_EXITS:
            continue
        sells = [
            f
            for f in result.fills
            if f.instrument_id == trade.instrument_id
            and f.side == "sell"
            and f.day == trade.exit_date
        ]
        close = _bar(data, trade.instrument_id, trade.exit_date)[3]
        assert any(f.price == close for f in sells)


# --- HAT-BT-024 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.integers(0, 50).map(D))
@RUNS
def test_decimal_money(paths: dict, slippage: D) -> None:
    """Every trade has a whole quantity of at least 1 and Decimal money; the trades' net P&L sums
    exactly to the change in equity (every position is closed at the end of the period)."""
    result = _run(s.market(paths, DAYS), slippage=slippage)
    for trade in result.trades:
        assert isinstance(trade.qty, int)
        assert trade.qty >= 1
        for value in (trade.entry_price, trade.initial_stop, trade.realized_pnl, trade.fees):
            assert isinstance(value, Decimal)
    assert all(isinstance(v, Decimal) for _, v in result.equity)
    net = sum((t.realized_pnl for t in result.trades), D(0))
    assert result.equity[-1][1] - s.spec(s.config(), DAYS).initial_equity == net


# --- HAT-BT-032 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_trades_match_metrics(paths: dict) -> None:
    """The trade count and sum of R in `trades.parquet` equal the run's metrics."""
    result = _run(s.market(paths, DAYS))
    table = pq.read_table(pa.BufferReader(trades_parquet(result.trades)))
    rs = table.column("r_multiple").to_pylist()
    assert table.num_rows == result.metrics["trades"]
    assert str(sum(rs, D(0)).quantize(D("0.000001"))) == result.metrics["sum_r"]


# --- HAT-BT-040 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS))
@RUNS
def test_r_and_expectancy(paths: dict) -> None:
    """R = realised P&L / ((entry - initial stop) x quantity); a trade that left whole at its
    initial stop without costs is -1 R; expectancy is the mean R."""
    result = _run(s.market(paths, DAYS), fills=s.zero_fills())
    for t in result.trades:
        risk = (t.entry_price - t.initial_stop) * t.qty
        assert risk > 0
        assert t.r_multiple == (t.realized_pnl / risk).quantize(D("0.000001"))
        if t.realized_pnl == (t.initial_stop - t.entry_price) * t.qty:
            assert t.r_multiple == -1
    if result.trades:
        mean = sum((t.r_multiple for t in result.trades), D(0)) / len(result.trades)
        assert result.metrics["expectancy_r"] == str(mean.quantize(D("0.000001")))
    else:
        assert result.metrics["expectancy_r"] is None


# --- HAT-BT-041 -------------------------------------------------------------------------------


def _trade(day: int, r: D, n: int) -> TradeRow:
    return TradeRow(
        entry_date=DAYS[0],
        exit_date=DAYS[day],
        playbook="p",
        instrument_id=s.instrument(n),
        qty=1,
        entry_price=D(100),
        initial_stop=D(90),
        exit_reason="hard_stop",
        realized_pnl=r * 10,
        fees=D(0),
        r_multiple=r,
        mfe_r=max(r, D(0)),
        mae_r=min(r, D(0)),
        holding_days=1,
        strategy_version_id=s.VERSION,
        risk_policy_version_id=s.policy_version().version_id,
    )


R_VALUES = st.integers(-300, 500).map(lambda n: D(n) / 100)


@given(
    st.lists(st.tuples(st.integers(0, 9), R_VALUES), max_size=30),
    st.lists(st.integers(1, 3_000_000).map(D), min_size=1, max_size=10),
    st.randoms(use_true_random=False),
)
def test_metrics_bounds(rows: list[tuple[int, D]], curve: list[D], rnd: object) -> None:
    """Max drawdown and win rate are in [0, 1], and the metrics do not depend on the order in
    which trades closing on the same day are listed."""
    trades = sorted(
        (_trade(day, r, n) for n, (day, r) in enumerate(rows)), key=lambda t: t.exit_date
    )
    equity = [(DAYS[n], v) for n, v in enumerate(curve)]
    spec = s.spec(s.config(), DAYS)
    metrics = compute_metrics(trades, equity, spec)
    assert 0 <= metrics["max_drawdown"] <= 1
    assert metrics["win_rate"] is None or 0 <= metrics["win_rate"] <= 1
    shuffled = list(trades)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    shuffled.sort(key=lambda t: t.exit_date)  # stable: same-day trades keep the shuffled order
    assert compute_metrics(shuffled, equity, spec) == metrics


# --- HAT-BT-050 -------------------------------------------------------------------------------


@given(s.paths(SESSIONS), st.randoms(use_true_random=False))
@RUNS
def test_deterministic(paths: dict, rnd: object) -> None:
    """Two runs of the same market and config give byte-identical trade files and equal metrics,
    whatever the order of the input rows and mappings."""
    data = s.market(paths, DAYS)
    order = list(range(data.bars.height))
    rnd.shuffle(order)  # type: ignore[attr-defined]
    members = list(data.members)
    rnd.shuffle(members)  # type: ignore[attr-defined]
    shuffled = MarketData(
        days=data.days,
        bars=data.bars[order],
        features=data.features.sample(fraction=1.0, shuffle=True, seed=rnd.randint(0, 999)),  # type: ignore[attr-defined]
        index_closes=data.index_closes,
        members=tuple(members),
        industries=dict(reversed(list(data.industries.items()))),
        series=dict(reversed(list(data.series.items()))),
        last_traded=data.last_traded,
    )
    first, second = _run(data), _run(shuffled)
    assert trades_parquet(first.trades) == trades_parquet(second.trades)
    assert json.dumps(first.metrics, sort_keys=True) == json.dumps(second.metrics, sort_keys=True)
    assert first.metrics["known_gaps"] == list(KNOWN_GAPS)


# --- HAT-BT-061 -------------------------------------------------------------------------------

ENGINE = memory_engine()
seed_hat_reference(ENGINE)


@given(st.sampled_from(["running", "completed", "failed"]), st.booleans())
@settings(max_examples=25, deadline=None)
def test_version_status_requires_run(status: str, same_version: bool) -> None:
    """A draft becomes `backtested` only with a completed run of that same version; a run that
    is still running, failed, or of another version is refused and leaves the draft a draft."""
    with Session(ENGINE) as session:
        strategy = seed_default_strategies(session, uuid.uuid4())[0]
        draft = HatStrategyVersion(
            strategy_id=strategy.id,
            version=99,
            status="draft",
            config={},
            config_hash="h",
        )
        session.add(draft)
        session.flush()
        run = HatBacktestRun(
            strategy_version_id=draft.id if same_version else uuid.uuid4(),
            risk_policy_version_id=uuid.uuid4(),
            family_key="f",
            period_start=DAYS[0],
            period_end=DAYS[-1],
            data_snapshot={},
            status=status,
            metrics={},
        )
        session.add(run)
        session.flush()
        if status == "completed" and same_version:
            assert publish_version(session, draft.id, backtest_run_id=run.id).status == "backtested"
            assert draft.backtest_run_id == run.id
        else:
            with pytest.raises(VersionError):
                publish_version(session, draft.id, backtest_run_id=run.id)
            assert draft.status == "draft"
            assert draft.backtest_run_id is None
        session.rollback()

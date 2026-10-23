"""PBT regime.*: the 3-day confirmation, the inputs, the classification and
the multiplier table, on the real `app.hat.engine.portfolio.regime`."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.engine import regime_gate
from app.hat.engine.portfolio import regime
from app.hat.engine.portfolio.regime import RegimeError
from app.hat.engine.strategy.config import Regime

D = Decimal
RANK = {regime.BULL_QUIET: 0, regime.BULL_VOLATILE: 1, regime.CORRECTION: 2, regime.BEAR: 3}
RAWS = st.lists(st.sampled_from(regime.STATES), min_size=1, max_size=30)
LEVEL = st.integers(1, 100_000).map(lambda n: D(n) / 100)
PCT = st.integers(0, 10_000).map(lambda n: D(n) / 100)


def model_confirmed(raws: list[str], days: int = 3) -> list[str]:
    """The confirmed state after each day, written from the rule: the first reading is the
    confirmed state; it then changes to a state only after `days` readings of it in a row."""
    out = [raws[0]]
    for i in range(1, len(raws)):
        window = raws[max(0, i - days + 1) : i + 1]
        if len(window) == days and len(set(window)) == 1 and window[0] != out[-1]:
            out.append(window[0])
        else:
            out.append(out[-1])
    return out


def confirm_all(raws: list[str], days: int = 3) -> list[regime.Confirmation]:
    """`confirm_step` over a whole series, as the nightly job applies it one day at a time."""
    out: list[regime.Confirmation] = []
    held: regime.Confirmation | None = None
    for raw in raws:
        held = regime.confirm_step(held, raw, days)
        out.append(held)
    return out


@given(RAWS)
def test_three_day_confirmation(raws: list[str]) -> None:
    """The confirmed state equals the independent model on every prefix; it changes only on the
    third consecutive reading of a different state; and it only ever takes values that were
    read."""
    steps = confirm_all(raws)
    assert [s.confirmed for s in steps] == model_confirmed(raws)
    for i in range(1, len(raws)):
        if steps[i].confirmed != steps[i - 1].confirmed:
            assert raws[i] == raws[i - 1] == raws[i - 2] == steps[i].confirmed
            assert i >= 2
    assert {s.confirmed for s in steps} <= set(raws)
    for i in range(2, len(raws)):
        if raws[i] == raws[i - 1] == raws[i - 2]:
            assert steps[i].confirmed == raws[i]  # three equal days always confirm


@given(RAWS, st.integers(1, 6))
def test_confirmation_days_parameter(raws: list[str], days: int) -> None:
    """The rule holds for any confirmation length, not only 3."""
    steps = confirm_all(raws, days)
    assert [s.confirmed for s in steps] == model_confirmed(raws, days)


@given(st.text(max_size=8))
def test_confirmation_refuses_an_unknown_state(raw: str) -> None:
    """A state outside the four is an error, never confirmed."""
    if raw in regime.STATES:
        return
    with pytest.raises(RegimeError):
        regime.confirm_step(None, raw)


SERIES_LEN = 262
FIRST = date(2025, 1, 1)


@st.composite
def series_pair(draw: st.DrawFn) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    days = [FIRST + timedelta(days=i) for i in range(SERIES_LEN)]
    closes = draw(st.lists(LEVEL, min_size=SERIES_LEN, max_size=SERIES_LEN))
    vix = draw(
        st.lists(
            st.integers(500, 4000).map(lambda n: D(n) / 100),
            min_size=SERIES_LEN,
            max_size=SERIES_LEN,
        )
    )
    return dict(zip(days, closes, strict=True)), dict(zip(days, vix, strict=True))


@settings(max_examples=40)
@given(series_pair(), PCT, st.integers(0, SERIES_LEN - 252), st.data())
def test_inputs(pair: tuple, breadth: Decimal, back: int, data: st.DataObject) -> None:
    """The inputs of a day read nothing dated after it, the percentile and the SMAs follow their
    definitions, and a missing or unusable input fails instead of being guessed."""
    closes, vix = pair
    day = FIRST + timedelta(days=SERIES_LEN - 1 - back)
    got = regime.compute_inputs(day, closes, vix, breadth)
    # no lookahead: dropping every later value changes nothing, and changing them changes nothing
    upto_c = {d: v for d, v in closes.items() if d <= day}
    upto_v = {d: v for d, v in vix.items() if d <= day}
    assert regime.compute_inputs(day, upto_c, upto_v, breadth) == got
    future = {d: v + 1 for d, v in closes.items() if d > day} | upto_c
    assert regime.compute_inputs(day, future, upto_v, breadth) == got
    # definitions
    last200 = [upto_c[d] for d in sorted(upto_c)][-200:]
    last50 = last200[-50:]
    window = [upto_v[d] for d in sorted(upto_v)][-252:]
    assert got.sma200 == sum(last200, D(0)) / 200
    assert got.sma50 == sum(last50, D(0)) / 50
    assert got.vix == window[-1]
    assert got.vix_pct == D(100) * sum(1 for v in window if v < window[-1]) / 252
    assert 0 <= got.vix_pct < 100
    assert got.breadth_pct == breadth
    # failures
    no_close_today = {d: v for d, v in upto_c.items() if d != day}
    no_vix_today = {d: v for d, v in upto_v.items() if d != day}
    with pytest.raises(RegimeError):
        regime.compute_inputs(day, no_close_today, upto_v, breadth)
    with pytest.raises(RegimeError):
        regime.compute_inputs(day, upto_c, no_vix_today, breadth)
    bad = data.draw(st.sampled_from((None, D(-1), D("100.01"), D("NaN"))))
    with pytest.raises(RegimeError):
        regime.compute_inputs(day, upto_c, upto_v, bad)  # type: ignore[arg-type]
    short = day - timedelta(days=SERIES_LEN - 1 - back - 100)
    short_closes = {d: v for d, v in closes.items() if d <= short}
    short_vix = {d: v for d, v in vix.items() if d <= short}
    with pytest.raises(RegimeError):
        regime.compute_inputs(short, short_closes, short_vix, breadth)


@given(st.lists(LEVEL, min_size=252, max_size=252), LEVEL, LEVEL)
def test_vix_percentile_is_monotone(values: list[Decimal], low: Decimal, high: Decimal) -> None:
    """With the other 251 days fixed, a higher VIX today never lowers its percentile."""
    low, high = min(low, high), max(low, high)
    p_low = regime.vix_percentile([*values[:-1], low])
    p_high = regime.vix_percentile([*values[:-1], high])
    assert p_low is not None
    assert p_high is not None
    assert 0 <= p_low <= p_high < 100


@given(LEVEL, LEVEL, LEVEL, PCT, PCT, LEVEL, PCT, PCT)
def test_classification_is_monotone(
    close: Decimal,
    sma50: Decimal,
    sma200: Decimal,
    vix_pct: Decimal,
    breadth: Decimal,
    lower_close: Decimal,
    higher_vix: Decimal,
    lower_breadth: Decimal,
) -> None:
    """Worse inputs never give a safer state: a lower close, a higher VIX percentile or lower
    breadth moves the state toward Bear or leaves it."""
    base = RANK[regime.classify(close, sma50, sma200, vix_pct, breadth)]
    worse_close = min(close, lower_close)
    assert RANK[regime.classify(worse_close, sma50, sma200, vix_pct, breadth)] >= base
    assert RANK[regime.classify(close, sma50, sma200, max(vix_pct, higher_vix), breadth)] >= base
    assert RANK[regime.classify(close, sma50, sma200, vix_pct, min(breadth, lower_breadth))] >= base


HISTORY = 252
# few distinct values, so a close equal to its average (the edge of `>`) is common
CLOSES = st.lists(st.integers(1, 6).map(D), min_size=0, max_size=30)


def sma(closes: list[Decimal], n: int, back: int = 0) -> Decimal | None:
    end = len(closes) - back
    if end < n:
        return None
    return sum(closes[end - n : end], D(0)) / n


@given(CLOSES, st.integers(1, 10), st.integers(1, 5))
def test_strategy_rules(closes: list[Decimal], window: int, rising: int) -> None:
    """Close_above_sma_and_rising is true iff close_t > SMA_t and SMA_t >
    SMA_{t-rising}; sma_fast_above_slow_both_rising is true iff SMA_fast > SMA_slow and both are
    above their value `rising` sessions earlier; a series too short for the lookback is false.
    Through the config, the fast/slow rule without `rising_days` reads 3 sessions (owner default),
    one with it reads that many, and the configured rule gives what the function gives.
    Harm: the gate lets entries through in a falling market or blocks them in a rising one, or
    flips on one down tick."""
    now, before = sma(closes, window), sma(closes, window, rising)
    want = now is not None and before is not None and closes[-1] > now and now > before
    assert regime_gate.close_above_sma_and_rising(closes, window, rising) is want
    fast, slow = window, window + 5

    def fast_slow(back: int) -> bool:
        f0, f1 = sma(closes, fast), sma(closes, fast, back)
        s0, s1 = sma(closes, slow), sma(closes, slow, back)
        return None not in (f0, f1, s0, s1) and f0 > s0 and f0 > f1 and s0 > s1  # type: ignore[operator]

    assert regime_gate.sma_fast_above_slow_both_rising(closes, fast, slow, rising) is fast_slow(
        rising
    )
    if fast >= 2:
        for params, back in (
            ({"fast": fast, "slow": slow}, 3),
            ({"fast": fast, "slow": slow, "rising_days": rising}, rising),
        ):
            regime = Regime(
                index="NIFTY 500", rule="sma_fast_above_slow_both_rising", params=params
            )
            assert regime_gate.regime_allows_longs(regime, closes) is fast_slow(back)

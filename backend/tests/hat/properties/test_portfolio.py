"""A backtest as configuration: the portfolio file (`backtest.portfolio`), the playbook keep-list
override (`Overrides.playbooks`), the ETF store's pure readers (`backtest.etf_data`) and an
overlay record read back for a rerun (`overlay.replay_overlay`). Small hand-built inputs; no
run, no database."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.hat.backtest import etf_data
from app.hat.backtest import overlay as ov
from app.hat.backtest.overrides import OverrideError, Overrides
from app.hat.backtest.portfolio import parse_portfolio
from app.hat.backtest.spec import BacktestError
from app.hat.engine.strategy.config import parse_config
from tests.hat.hat_backtest_support import config as support_config

S1 = "e8cfee88-c0a4-4e52-b17d-23b2b16c7b7c"
S2 = "64274b85-696f-4aea-944e-717a62546682"


def _no_policy(path: str) -> object:
    raise AssertionError(f"no policy file expected: {path}")


def _doc(**extra: object) -> dict[str, object]:
    return {
        "portfolio": "test",
        "universe": "NIFTY 500",
        "window": {"from": date(2018, 1, 1), "to": "2020-12-31"},
        "strategies": [{"version": S1}],
        **extra,
    }


# --- the portfolio file ------------------------------------------------------------------------


def test_one_strategy_is_the_research_run_the_cli_builds() -> None:
    request = parse_portfolio(_doc(), _no_policy)
    assert request.version_id == uuid.UUID(S1)
    assert (request.start, request.end) == (date(2018, 1, 1), date(2020, 12, 31))
    assert request.universe == "NIFTY 500"
    assert request.bot_id is None
    assert request.co_sleeves == ()
    assert request.overlay is None
    assert request.conventions.sleeve_weight is None  # the policy's base weight
    assert request.conventions.overrides.empty
    assert request.labels.variant == "test"


def test_two_strategies_share_the_account_at_their_weights() -> None:
    doc = _doc(
        strategies=[
            {"version": S1, "weight": 0.5, "set": ["exits[2].at_r=3"]},
            {"version": S2, "weight": "0.4", "playbooks": ["ep"]},
        ]
    )
    request = parse_portfolio(doc, _no_policy)
    assert request.conventions.sleeve_weight == Decimal("0.5")
    assert request.conventions.overrides.sets == (("exits[2].at_r", 3),)
    (co,) = request.co_sleeves
    assert (co.version_id, co.weight) == (uuid.UUID(S2), Decimal("0.4"))
    assert co.overrides.playbooks == ("ep",)
    assert not co.overrides.touches_policy


def test_several_strategies_need_every_weight() -> None:
    doc = _doc(strategies=[{"version": S1, "weight": 0.5}, {"version": S2}])
    with pytest.raises(BacktestError, match="backtest_portfolio_bad_file"):
        parse_portfolio(doc, _no_policy)


def test_a_strategy_twice_is_refused() -> None:
    doc = _doc(strategies=[{"version": S1, "weight": 0.5}, {"version": S1, "weight": 0.5}])
    with pytest.raises(BacktestError, match="backtest_portfolio_duplicate_strategy"):
        parse_portfolio(doc, _no_policy)


@pytest.mark.parametrize(
    "doc",
    [
        {"portfolio": "x", "universe": "u", "window": {"from": "2018-01-01", "to": "2019-01-01"}},
        _doc(colour="red"),
        _doc(window={"from": "2018-01-01"}),
        _doc(strategies=[]),
        _doc(strategies=[{"version": S1, "weigth": 1}]),
        _doc(strategies=[{"version": S1, "playbooks": []}]),
        _doc(conventions={"entry_barz": "daily"}),
        _doc(flat_ladder="yes"),
        _doc(max_positions=2.5),
    ],
)
def test_a_malformed_file_is_refused(doc: dict[str, object]) -> None:
    with pytest.raises(BacktestError, match="backtest_portfolio_bad_file"):
        parse_portfolio(doc, _no_policy)


def test_conventions_policy_knobs_and_fills_carry_over() -> None:
    doc = _doc(
        conventions={"entry_bars": "daily", "regime_mult": "policy", "suspension_sessions": 3},
        start_capital=2_500_000,
        slippage_bps=10,
        risk_pct="0.004",
        max_positions=6,
        flat_ladder=True,
        policy_set=["kill.bot_drawdown_pct=0.15"],
        split="in_sample",
    )
    request = parse_portfolio(doc, _no_policy, ("--portfolio", "f.yaml"))
    c = request.conventions
    assert (c.entry_bars, c.regime_mult, c.suspension_sessions) == ("daily", "policy", 3)
    assert (c.start_capital, request.slippage_bps) == (Decimal(2_500_000), Decimal(10))
    o = c.overrides
    assert (o.risk_pct, o.max_positions, o.flat_ladder) == (Decimal("0.004"), 6, True)
    assert o.policy_sets == (("kill.bot_drawdown_pct", "0.15"),)
    assert request.args == ("--portfolio", "f.yaml")


def test_an_unknown_convention_value_keeps_its_own_code() -> None:
    with pytest.raises(BacktestError, match="backtest_bad_slot_order"):
        parse_portfolio(_doc(conventions={"slot_order": "alphabetical"}), _no_policy)


def test_the_overlay_block_is_parsed_and_off_without_it() -> None:
    request = parse_portfolio(_doc(overlay={"sleeve": "regime", "tax": False}), _no_policy)
    assert request.overlay is not None
    assert request.overlay.sleeve == ov.REGIME
    assert request.overlay.tax is False
    with pytest.raises(BacktestError, match="overlay_bad_block"):
        parse_portfolio(_doc(overlay={"slieve": "regime"}), _no_policy)


def test_a_hold_out_names_the_run_it_validates() -> None:
    with pytest.raises(BacktestError, match="backtest_hold_out_validates"):
        parse_portfolio(_doc(split="hold_out"), _no_policy)
    request = parse_portfolio(_doc(split="hold_out", validates="run-1"), _no_policy)
    assert request.labels.validates == "run-1"


def test_a_policy_path_is_read_through_the_reader() -> None:
    seen: list[str] = []

    def read(path: str) -> object:
        seen.append(path)
        raise OverrideError("backtest_policy_unreadable")

    with pytest.raises(BacktestError, match="backtest_policy_unreadable"):
        parse_portfolio(_doc(policy="candidate.yaml"), read)
    assert seen == ["candidate.yaml"]


# --- the playbook keep-list --------------------------------------------------------------------


def _config_with(names: list[str]) -> dict[str, object]:
    """The support config's document with one copy of its playbook per name."""
    doc = support_config().to_json()
    first = doc["playbooks"][0]
    doc["playbooks"] = [{**first, "name": n} for n in names]
    return doc


def test_playbooks_keeps_only_those_named() -> None:
    config = parse_config(_config_with(["default", "ep"]))
    kept = Overrides(playbooks=("ep",)).apply_config(config)
    assert [p.name for p in kept.playbooks] == ["ep"]
    assert not Overrides(playbooks=("ep",)).empty
    assert Overrides(playbooks=("ep",)).record() == {"playbooks": ["ep"]}


@pytest.mark.parametrize("names", [("vcp",), ()])
def test_playbooks_naming_none_of_the_configs_is_refused(names: tuple[str, ...]) -> None:
    config = parse_config(_config_with(["default", "ep"]))
    with pytest.raises(OverrideError, match="backtest_override_no_item"):
        Overrides(playbooks=names).apply_config(config)


# --- the ETF store's readers -------------------------------------------------------------------


def test_e4_is_exact_to_four_places() -> None:
    assert etf_data.e4("259.4") == 2_594_000
    assert etf_data.e4(Decimal("0.0001")) == 1
    for bad in ("1.00001", "-1", "nan", "x"):
        with pytest.raises(etf_data.EtfDataError):
            etf_data.e4(bad)


def test_five_min_rows_start_dedupe_session_and_factor() -> None:
    factors = {date(2021, 11, 1): Decimal(1), date(2026, 10, 5): Decimal("0.5")}
    candles = [
        # end-labelled history: the 09:20 label is the 09:15 bar; a 09:15 label is the pre-open's
        ["2021-11-01T09:15:00+0530", 1, 1, 1, 1, 10],
        ["2021-11-01T09:20:00+0530", 2, 3, 1, 2, 10],
        ["2021-11-01T09:20:00+0530", 9, 9, 9, 9, 99],  # read twice: the first is kept
        # start-labelled after the switch, the price halved by the day's factor
        ["2026-10-05T09:15:00+0530", "10", "12", "8", "11", 5],
        ["2026-10-05T15:30:00+0530", 1, 1, 1, 1, 1],  # past the close
        ["2026-10-06T09:15:00+0530", 1, 1, 1, 1, 1],  # no daily bar that day
    ]
    rows = etf_data.five_min_rows("NIFTYBEES", candles, factors)
    starts = [datetime.fromtimestamp(r[1], UTC).isoformat() for r in rows]
    assert starts == ["2021-11-01T03:45:00+00:00", "2026-10-05T03:45:00+00:00"]
    assert rows[0][2:] == (20_000, 30_000, 10_000, 20_000, 10)
    assert rows[1][2:] == (50_000, 60_000, 40_000, 55_000, 5)


def test_proxy_ranges_join_across_unchanged_days() -> None:
    days = [
        (date(2015, 1, 2), "proxy"),
        (date(2015, 1, 5), "none"),
        (date(2015, 1, 6), "proxy"),
        (date(2015, 1, 7), "dividend"),
        (date(2015, 1, 8), "proxy"),
    ]
    assert etf_data.proxy_ranges(days) == [
        (date(2015, 1, 2), date(2015, 1, 6)),
        (date(2015, 1, 8), date(2015, 1, 8)),
    ]
    assert etf_data.proxy_ranges([(date(2020, 4, 1), "dividend")]) == []


# --- an overlay read back ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        {},
        {"sleeve": "regime_gold", "tax": {"slab_rate": "0.2"}, "cash_buffer_pct": "0.1"},
        {"tax": False, "regime": {"sma": 100, "confirm": 3}, "slippage_bps": {"LIQUIDBEES": 2}},
        {"same_day_sale_credit_pct": [{"from": "2000-01-01", "pct": "0.5"}]},
    ],
)
def test_an_overlay_record_replays_to_its_config(block: dict[str, object]) -> None:
    config = ov.parse_overlay(block)
    assert ov.replay_overlay(config.record()) == config

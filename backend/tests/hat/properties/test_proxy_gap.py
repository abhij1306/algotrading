"""The pure parts of the shadow-vs-proxy gap (G15, G36 part, PR 40):
`proxy_gap.compare` and its summary, and the window, leave-out and column rules `shadow_gap`
applies. No database, run or market is built."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.hat.backtest.proxy_gap import GapError, Leg, columns_to_read, comparable, compare, window
from app.hat.backtest.report import END_OF_PERIOD

D = Decimal
EP_PLAYBOOK = "episodic_pivot"
DAY = date(2026, 3, 2)
A, B = "instrument-a", "instrument-b"


def _leg(
    instrument: str = A,
    *,
    playbook: str = "pb",
    price: str = "100",
    r: str = "1",
    reason: str = "hard_stop",
    score: str | None = None,
) -> Leg:
    return Leg(
        playbook, instrument, DAY, D(price), D(r), reason, None if score is None else D(score)
    )


def test_compare_pairs_by_key() -> None:
    report = compare(
        [_leg(A, price="101", r="1.5", score="0.8"), _leg(B), _leg(B)],
        [_leg(A, price="100", r="2", reason="time_stop", score="0.8"), _leg(B)],
    )
    (gap,) = report.gaps
    assert gap.key == ("pb", A, DAY)
    assert gap.entry_gap_bps == D(100)
    assert gap.r_gap == D("-0.5")
    assert (gap.shadow_exit, gap.proxy_exit, gap.same_score) == ("hard_stop", "time_stop", True)
    # a key held twice on a side pairs nothing; each other trade counts once on its own side
    assert report.shadow_only == (("pb", B, DAY), ("pb", B, DAY))
    assert report.proxy_only == (("pb", B, DAY),)


def test_compare_edge_cases() -> None:
    # the same setup on another instrument is not its proxy
    assert compare([_leg(A)], [_leg(B)]).gaps == ()
    # no score on either side is no agreement
    assert compare([_leg(A)], [_leg(A)]).gaps[0].same_score is False
    # a proxy priced at zero is left out, not counted on both sides
    zero = compare([_leg(A)], [_leg(A, price="0")])
    assert (zero.gaps, zero.shadow_only, zero.proxy_only) == ((), (("pb", A, DAY),), ())


def test_summary() -> None:
    summary = compare(
        [_leg(A, r="2"), _leg(B, r="1", reason="time_stop")],
        [_leg(A, r="1"), _leg(B, r="2", reason="time_stop", price="200")],
    ).summary()
    assert summary["matched"] == 2
    assert summary["mean_r_gap"] == "0.000000"
    assert summary["mean_entry_gap_bps"] == "-2500.000000"
    assert summary["same_exit"] == 2
    assert summary["r_gap_by_exit"] == {
        "hard_stop": {"trades": 1, "mean_r_gap": "1.000000"},
        "time_stop": {"trades": 1, "mean_r_gap": "-1.000000"},
    }
    assert compare([], []).summary()["mean_r_gap"] is None


def test_window_within_the_run() -> None:
    end = DAY + timedelta(days=9)
    assert window(DAY, end, None, None) == (DAY, end)
    assert window(DAY, end, DAY - timedelta(days=3), end + timedelta(days=2)) == (DAY, end)
    inner = (DAY + timedelta(days=2), DAY + timedelta(days=5))
    assert window(DAY, end, *inner) == inner


def test_comparable_leaves_out() -> None:
    ep_shadow, ep_proxy = _leg(A, playbook=EP_PLAYBOOK), _leg(A, playbook=EP_PLAYBOOK)
    ended = _leg(B, reason=END_OF_PERIOD)
    kept = _leg(A)
    shadow, proxy = comparable([ep_shadow, _leg(B), kept], [ep_proxy, ended, kept], {EP_PLAYBOOK})
    assert (shadow, proxy) == ([kept], [kept])
    # with nothing left out, EP is compared
    assert comparable([ep_shadow], [ep_proxy], frozenset()) == ([ep_shadow], [ep_proxy])


def test_columns_to_read() -> None:
    trade = ["playbook", "instrument_id", "entry_date", "entry_price", "r_multiple", "exit_reason"]
    assert columns_to_read([*trade, "score", "fees"]) == [*trade, "score"]
    assert columns_to_read(trade) == trade  # a run before PR 65 has no score column
    with pytest.raises(GapError) as refused:
        columns_to_read(trade[1:])
    assert refused.value.code == "gap_no_trades_file"

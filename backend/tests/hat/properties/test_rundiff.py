"""The rerun diff's pure parts (`backtest.rundiff`) on hand-built trade rows and configs."""

from __future__ import annotations

from app.hat.backtest import rundiff


def _trade(symbol: str, entry: str, **fields: str) -> dict[str, str]:
    row = {
        "instrument_id": f"id-{symbol}",
        "symbol": symbol,
        "playbook": "pb",
        "signal_date": entry,
        "entry_date": entry,
        "leg": "1",
        "entry_price": "100.0",
        "initial_stop": "95.0",
        "qty": "10",
        "exit_date": "2024-02-01",
        "exit_price": "110.0",
        "exit_reason": "trail",
        "fees": "5.0",
        "net_pnl": "95.0",
    }
    return {**row, **fields}


def test_pairs_same_changed_moved_and_unpaired() -> None:
    before = [
        _trade("AAA", "2024-01-02"),
        _trade("BBB", "2024-01-03"),
        _trade("CCC", "2024-01-04", signal_date="2024-01-03"),
        _trade("DDD", "2024-01-05", net_pnl="50.0"),
    ]
    after = [
        _trade("AAA", "2024-01-02", net_pnl="95.004"),  # within half a paisa: the same
        _trade("BBB", "2024-01-03", qty="12", fees="6.0", net_pnl="114.0"),
        _trade("CCC", "2024-01-05", signal_date="2024-01-03", exit_reason="stop"),
        _trade("EEE", "2024-01-06", net_pnl="20.0"),
    ]
    diff = rundiff.diff_trades(before, after)
    assert [p.before["symbol"] for p in diff.same] == ["AAA"]
    changed = {p.before["symbol"]: (p.how, p.changed) for p in diff.changed}
    assert changed == {
        "BBB": ("entry", ("qty", "fees", "net_pnl")),
        "CCC": ("moved", ("entry_date", "exit_reason")),
    }
    assert [p.before["symbol"] for p in diff.sizing_only] == ["BBB"]
    assert [r["symbol"] for r in diff.only_before] == ["DDD"]
    assert [r["symbol"] for r in diff.only_after] == ["EEE"]
    assert diff.first_divergence() == "2024-01-03"
    assert diff.pnl_change() == {
        "only_before": -50.0,
        "only_after": 20.0,
        "sizing_only": 19.0,
        "entry_or_exit": 0.0,
        "total": -11.0,
    }


def test_same_key_twice_pairs_in_file_order() -> None:
    before = [_trade("AAA", "2024-01-02", leg="1"), _trade("AAA", "2024-01-02", leg="1", qty="3")]
    after = [_trade("AAA", "2024-01-02", leg="1"), _trade("AAA", "2024-01-02", leg="1", qty="3")]
    diff = rundiff.diff_trades(before, after)
    assert len(diff.same) == 2
    assert not diff.changed
    assert diff.first_divergence() is None


def test_config_diff_leaves_out_identity_and_lists_leaves() -> None:
    before = {"run_id": "a", "fills": {"cost_model": "cnc-delivery-1", "bps": 20}, "gaps": ["x"]}
    after = {
        "run_id": "b",
        "fills": {"cost_model": "cnc-delivery-2", "bps": 20},
        "gaps": ["x", "y"],
    }
    assert rundiff.config_diff(before, after) == [
        ("fills.cost_model", "cnc-delivery-1", "cnc-delivery-2"),
        ("gaps.1", None, "y"),
    ]


def test_headline_rows_carry_change_and_excess_over_tri() -> None:
    before = {"metrics": {"cagr_pct": 10.0, "benchmark_cagr_pct": 15.0, "trades": 4}}
    after = {"metrics": {"cagr_pct": 12.5, "benchmark_cagr_pct": 15.0, "trades": None}}
    rows = {label: (a, b, d) for label, a, b, d in rundiff.headline_rows(before, after)}
    assert rows["CAGR %"] == (10.0, 12.5, 2.5)
    assert rows["trades"] == (4, None, None)
    assert rows["CAGR over TRI, pts"] == (-5.0, -2.5, 2.5)

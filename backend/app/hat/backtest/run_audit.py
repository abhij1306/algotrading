"""The run audit (docs/backtests/process.md): deterministic checks of a completed run's own files,
written as `audit.json` in its folder beside `run.json`, with a status (`pass`, `warn` or `fail`)
and the findings. It recomputes from `trades.csv`, `equity.csv` and `refusals.csv` what
`run.json` states, so a writer bug, a fill outside its day or a P&L that does not add up is caught
before anyone reads the metrics.

The checks:
- `binding`: a variant with a base made a different trade list (`no_op` warns: the change never
  triggered; a base without a binding warns), changed one knob against it (`one_change` warns
  with the knobs, a declared bundle's reason beside them; a base whose `config.json` predates
  knobs is counted in resolved paths), and ran under the base's risk policy version
  (`policy_drift` warns).
- `fills`: every entry and exit price inside its day's low and high, widened by the run's
  slippage (a fill without a bar that day, a suspension mark or a delisting, is counted).
- `sequence`: signal date <= entry date <= each leg's exit date, the legs in order.
- `pnl`: each leg's net = gross - fees; each trade's R = its net over its initial risk; the
  trades' count, net P&L (against final equity), fees, expectancy and win rate as `run.json` says.
- `costs`: fees on every trade; a cost share above `COST_SHARE_WARN_PCT` of gross profit warns.
- `liquidity`: each entry's % of ADV recomputed; entries above `ADV_LIMIT_PCT` warn.
- `concentration`: the top-5 trades' share recomputed; above `TOP5_WARN_PCT` warns, and a positive
  CAGR that is not positive without them warns.
- `equity_jumps`: a session whose equity moves more than `EQUITY_JUMP_PCT` must be explained by the
  positions held: with none held it fails, and beyond `BAND_PCT` of the value held it warns.
- `refusals`: `refusals.csv` counted by reason equals `run.json`'s `refusals`.
- `trials`: `dsr_n_trials` present (DSR is meaningless without it).
- `data`: the run's data audit status (`data.audit`); a warning names the data audit's checks
  that did not pass and the owner's explanation of that audit digest, if there is one
  (`data_audits/<digest>.explained.md`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..money import exact

__all__ = ["AUDIT_VERSION", "audit_run", "changed_of"]

AUDIT_VERSION = "2"
PASS, WARN, FAIL = "pass", "warn", "fail"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}
MAX_LISTED = 25
CRORE = Decimal(10_000_000)
MONEY_TOLERANCE = Decimal("0.01")  # a leg's money is written to 4 places
R_TOLERANCE = Decimal("0.0001")  # R to 6 places, summed over legs
PRICE_TOLERANCE = Decimal("0.0001")  # prices are written to 4 places
PCT_TOLERANCE = 0.01  # the metrics' percentages are written to 4 places
# The fees of a delivery round trip are about 0.25% of its value before slippage, so costs above
# half the gross profit mean the edge is mostly paid away.
COST_SHARE_WARN_PCT = 50.0
ADV_LIMIT_PCT = 5.0  # the standard's `trades_over_5pct_adv` line: above it a fill is not realistic
TOP5_WARN_PCT = 50.0  # half the net P&L from five trades: a result carried by a few names
# A session's equity move this large needs positions that can make it; a held stock moves at
# most NSE's widest price band in a session (an ex-date's demerger aside, which warns).
EQUITY_JUMP_PCT = 5.0
BAND_PCT = 20.0


def _check(name: str, summary: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> dict:
    status = max((f["severity"] for f in findings), key=_RANK.__getitem__, default=PASS)
    return {"name": name, "status": status, "summary": dict(summary), "findings": list(findings)}


def _finding(code: str, severity: str, items: Sequence[Any], **extra: object) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "count": len(items),
        "items": list(items[:MAX_LISTED]),
        **extra,
    }


def _rows(text: str | None) -> list[dict[str, str]]:
    return [] if not text else list(csv.DictReader(io.StringIO(text)))


def _dec(text: object) -> Decimal | None:
    return None if text in (None, "") else exact(text, floats=True)


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _trades(rows: Sequence[Mapping[str, str]]) -> dict[str, list[Mapping[str, str]]]:
    """The legs of each trade, by `trade_id`, in leg order."""
    out: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for r in rows:
        out[r.get("trade_id", "")].append(r)
    for legs in out.values():
        legs.sort(key=lambda r: int(r.get("leg") or 0))
    return dict(out)


def _sum(legs: Sequence[Mapping[str, str]], column: str) -> Decimal:
    return sum((_dec(r.get(column)) or Decimal(0) for r in legs), Decimal(0))


# --- the checks --------------------------------------------------------------------------------


def changed_of(run: Mapping[str, Any]) -> tuple[list[str], str]:
    """What a variant changed against its base and how it was counted: its knobs, or for a base
    that recorded none, the resolved config's changed paths (`config.overrides`)."""
    knobs = (run.get("identity") or {}).get("changed_knobs")
    if isinstance(knobs, list):
        return [str(k) for k in knobs], "knobs"
    return sorted((run.get("config") or {}).get("overrides") or {}), "resolved_paths"


def check_binding(run: Mapping[str, Any]) -> dict[str, Any]:
    identity, config = run.get("identity") or {}, run.get("config") or {}
    base = identity.get("base_run_id")
    binding = run.get("binding") or {}
    findings = []
    if base is not None and binding.get("trade_list_hash") is None:
        findings.append(_finding("binding_unknown", WARN, [base]))
    elif base is not None and binding.get("no_op"):
        findings.append(_finding("no_op", WARN, [base]))
    changed, basis = changed_of(run) if base is not None else ([], "knobs")
    if len(changed) > 1:
        bundle = identity.get("bundle")
        findings.append(_finding("one_change", WARN, changed, basis=basis, bundle=bundle))
    policy, base_policy = config.get("risk_policy_version"), config.get("base_risk_policy_version")
    if base is not None and base_policy is not None and policy != base_policy:
        findings.append(_finding("policy_drift", WARN, [{"run": policy, "base": base_policy}]))
    summary = {
        "base_run_id": base,
        "trades_changed": binding.get("trades_changed"),
        "changed": len(changed) if base is not None else None,
        "risk_policy_version": policy,
        "base_risk_policy_version": base_policy,
    }
    return _check("binding", summary, findings)


def check_fills(
    trades: Mapping[str, Sequence[Mapping[str, str]]],
    bars: Mapping[tuple[str, date], tuple[Decimal, Decimal]],
    slippage_bps: Decimal,
) -> dict[str, Any]:
    """Every entry (once per trade) and every leg's exit inside the day's (low, high) of `bars`,
    keyed (instrument id, date), widened by the slippage; a fill on a day without a bar is
    counted (a suspension mark or a delisting fills off the bars)."""
    slip = slippage_bps / Decimal(10_000)
    outside, no_bar, checked = [], [], 0

    def test(
        trade_id: str, instrument_id: str, day: date | None, price: Decimal | None, what: str
    ) -> None:
        nonlocal checked
        if day is None or price is None:
            outside.append({"trade_id": trade_id, "fill": what, "reason": "unreadable"})
            return
        found = bars.get((instrument_id, day))
        if found is None:
            no_bar.append({"trade_id": trade_id, "fill": what, "date": day.isoformat()})
            return
        checked += 1
        low, high = found
        if not low * (1 - slip) - PRICE_TOLERANCE <= price <= high * (1 + slip) + PRICE_TOLERANCE:
            outside.append(
                {
                    "trade_id": trade_id,
                    "fill": what,
                    "date": day.isoformat(),
                    "price": str(price),
                    "low": str(low),
                    "high": str(high),
                }
            )

    for trade_id, legs in sorted(trades.items()):
        first = legs[0]
        instrument_id = first.get("instrument_id", "")
        test(
            trade_id,
            instrument_id,
            _day(first.get("entry_date")),
            _dec(first.get("entry_price")),
            "entry",
        )
        for leg in legs:
            test(
                trade_id,
                instrument_id,
                _day(leg.get("exit_date")),
                _dec(leg.get("exit_price")),
                f"exit:{leg.get('leg')}",
            )
    findings = []
    if outside:
        findings.append(_finding("fill_outside_bar", FAIL, outside))
    entries_without = [r for r in no_bar if r["fill"] == "entry"]
    if entries_without:
        findings.append(_finding("entry_without_bar", FAIL, entries_without))
    summary = {
        "fills_checked": checked,
        "fills_without_bar": len(no_bar),
        "slippage_bps": str(slippage_bps),
    }
    return _check("fills", summary, findings)


def check_sequence(trades: Mapping[str, Sequence[Mapping[str, str]]]) -> dict[str, Any]:
    wrong = []
    for trade_id, legs in sorted(trades.items()):
        signal, entry = _day(legs[0].get("signal_date")), _day(legs[0].get("entry_date"))
        exits = [_day(r.get("exit_date")) for r in legs]
        if (
            entry is None
            or any(d is None for d in exits)
            or (signal is not None and signal > entry)
            or any(d < entry for d in exits if d is not None)
            or exits != sorted(exits, key=lambda d: d or date.min)
        ):
            wrong.append(
                {
                    "trade_id": trade_id,
                    "signal": str(signal),
                    "entry": str(entry),
                    "exits": [str(d) for d in exits],
                }
            )
    findings = [_finding("exit_before_entry", FAIL, wrong)] if wrong else []
    return _check("sequence", {"trades": len(trades)}, findings)


def _close(a: float | None, b: float | None, tolerance: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tolerance


def _leg_off(leg: Mapping[str, str]) -> bool:
    """Whether a leg's net is not its gross less its fees (or one of them is missing)."""
    gross, fee, net = _dec(leg.get("gross_pnl")), _dec(leg.get("fees")), _dec(leg.get("net_pnl"))
    return gross is None or fee is None or net is None or abs(gross - fee - net) > MONEY_TOLERANCE


def _r_off(trade_id: str, legs: Sequence[Mapping[str, str]]) -> dict[str, Any] | None:
    """The trade's finding when its R is not its net over its initial risk (or it has no risk)."""
    net, r = _sum(legs, "net_pnl"), _sum(legs, "r_net")
    risk = (_dec(legs[0].get("risk_per_share")) or Decimal(0)) * _sum(legs, "qty")
    if risk > 0 and abs(net / risk - r) <= R_TOLERANCE * len(legs):
        return None
    return {
        "trade_id": trade_id,
        "r_net": str(r),
        "recomputed": str(net / risk) if risk > 0 else None,
    }


def check_pnl(
    run: Mapping[str, Any], trades: Mapping[str, Sequence[Mapping[str, str]]]
) -> dict[str, Any]:
    """Each leg's net = gross - fees and each trade's R = its net over its initial risk
    (`risk_per_share` x its shares); then the trade count, net P&L against final equity less the
    start capital, fees, expectancy and win rate against `run.json`."""
    metrics = run.get("metrics") or {}
    start = _dec((run.get("config") or {}).get("start_capital"))
    legs_off, r_off = [], []
    nets: list[Decimal] = []
    rs: list[Decimal] = []
    fees = Decimal(0)
    for trade_id, legs in sorted(trades.items()):
        legs_off += [{"trade_id": trade_id, "leg": leg.get("leg")} for leg in legs if _leg_off(leg)]
        off = _r_off(trade_id, legs)
        if off is not None:
            r_off.append(off)
        nets.append(_sum(legs, "net_pnl"))
        rs.append(_sum(legs, "r_net"))
        fees += _sum(legs, "fees")
    findings = []
    if legs_off:
        findings.append(_finding("leg_net_not_gross_less_fees", FAIL, legs_off))
    if r_off:
        findings.append(_finding("r_not_net_over_risk", FAIL, r_off))
    total = sum(nets, Decimal(0))
    final = _dec(metrics.get("final_equity"))
    stated = {
        "trades": (len(trades), metrics.get("trades"), 0),
        "net_pnl_vs_equity": (
            float(total),
            None if final is None or start is None else float(final - start),
            max(1.0, float(start or 0) * 1e-6),
        ),
        "fees": (float(fees), metrics.get("fees"), 1.0),
        "expectancy_r": (
            float(sum(rs, Decimal(0)) / len(rs)) if rs else None,
            metrics.get("expectancy_r"),
            1e-3,
        ),
        "win_pct": (
            100 * sum(1 for r in rs if r > 0) / len(rs) if rs else None,
            metrics.get("win_pct"),
            PCT_TOLERANCE,
        ),
    }
    off = [
        {"field": k, "recomputed": a, "run_json": b}
        for k, (a, b, tol) in stated.items()
        if not _close(a, b, tol)
    ]
    if off:
        findings.append(_finding("metrics_disagree_with_trades", FAIL, off))
    summary = {"trades": len(trades), "net_pnl": float(total), "fees": float(fees)}
    return _check("pnl", summary, findings)


def check_costs(
    run: Mapping[str, Any], trades: Mapping[str, Sequence[Mapping[str, str]]]
) -> dict[str, Any]:
    free = [t for t, legs in sorted(trades.items()) if _sum(legs, "fees") <= 0]
    share = (run.get("metrics") or {}).get("cost_share_pct")
    findings = []
    if free:
        findings.append(_finding("trade_without_costs", FAIL, free))
    if share is not None and share > COST_SHARE_WARN_PCT:
        findings.append(
            _finding("cost_share_high", WARN, [share], threshold_pct=COST_SHARE_WARN_PCT)
        )
    summary = {"cost_share_pct": share, "cost_model": (run.get("config") or {}).get("cost_model")}
    return _check("costs", summary, findings)


def check_liquidity(trades: Mapping[str, Sequence[Mapping[str, str]]]) -> dict[str, Any]:
    """Each entry's value (price x the trade's shares) over its 20-day average traded value,
    recomputed and compared with `position_pct_adv`; entries above `ADV_LIMIT_PCT` warn."""
    wrong, large, missing = [], [], 0
    pcts = []
    for trade_id, legs in sorted(trades.items()):
        first = legs[0]
        adv, price = _dec(first.get("adv_20d_cr")), _dec(first.get("entry_price"))
        if adv is None or adv <= 0 or price is None:
            missing += 1
            continue
        pct = float(price * _sum(legs, "qty") / (adv * CRORE) * 100)
        pcts.append(pct)
        stated = _dec(first.get("position_pct_adv"))
        if stated is None or abs(float(stated) - pct) > PCT_TOLERANCE:
            wrong.append(
                {
                    "trade_id": trade_id,
                    "position_pct_adv": first.get("position_pct_adv"),
                    "recomputed": round(pct, 4),
                }
            )
        if pct > ADV_LIMIT_PCT:
            large.append(
                {"trade_id": trade_id, "symbol": first.get("symbol"), "pct_adv": round(pct, 3)}
            )
    findings = []
    if wrong:
        findings.append(_finding("position_pct_adv_wrong", FAIL, wrong))
    if large:
        findings.append(_finding("entry_over_adv_limit", WARN, large, limit_pct=ADV_LIMIT_PCT))
    summary = {
        "max_position_pct_adv": round(max(pcts), 4) if pcts else None,
        "entries_without_adv": missing,
    }
    return _check("liquidity", summary, findings)


def check_concentration(
    run: Mapping[str, Any], trades: Mapping[str, Sequence[Mapping[str, str]]]
) -> dict[str, Any]:
    metrics = run.get("metrics") or {}
    nets = [_sum(legs, "net_pnl") for legs in trades.values()]
    total = sum(nets, Decimal(0))
    share = (
        float(sum(sorted(nets, reverse=True)[:5], Decimal(0)) / total * 100) if total > 0 else None
    )
    stated = metrics.get("top5_trades_pnl_share_pct")
    findings = []
    if not _close(share, stated, PCT_TOLERANCE):
        findings.append(
            _finding("top5_share_disagrees", FAIL, [{"recomputed": share, "run_json": stated}])
        )
    if share is not None and share > TOP5_WARN_PCT:
        findings.append(
            _finding("top5_concentrated", WARN, [round(share, 2)], threshold_pct=TOP5_WARN_PCT)
        )
    cagr, ex = metrics.get("cagr_pct"), metrics.get("cagr_ex_top5_trades_pct")
    if cagr is not None and ex is not None and cagr > 0 >= ex:
        findings.append(
            _finding("edge_in_top5", WARN, [{"cagr_pct": cagr, "cagr_ex_top5_trades_pct": ex}])
        )
    summary = {
        "top5_trades_pnl_share_pct": share,
        "top10_symbols_pnl_share_pct": metrics.get("top10_symbols_pnl_share_pct"),
        "cagr_ex_top5_trades_pct": ex,
    }
    return _check("concentration", summary, findings)


def check_equity_jumps(
    run: Mapping[str, Any], equity: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """Each session whose equity moves more than `EQUITY_JUMP_PCT` against the session before
    (the start capital before the first): none held either side fails, a move beyond `BAND_PCT`
    of the larger value held warns."""
    previous = _dec((run.get("config") or {}).get("start_capital"))
    held_before = Decimal(0)
    unheld, beyond, jumps = [], [], 0
    for row in equity:
        value, held = _dec(row.get("equity")), _dec(row.get("position_value")) or Decimal(0)
        if value is None or previous is None or previous <= 0:
            previous, held_before = value, held
            continue
        move = value - previous
        if abs(move) / previous * 100 > EQUITY_JUMP_PCT:
            jumps += 1
            item = {"date": row.get("date"), "move_pct": round(float(move / previous * 100), 3)}
            exposure = max(held_before, held)
            if exposure <= 0:
                unheld.append(item)
            elif abs(move) > exposure * Decimal(BAND_PCT) / 100:
                beyond.append(item)
        previous, held_before = value, held
    findings = []
    if unheld:
        findings.append(_finding("equity_jump_without_positions", FAIL, unheld))
    if beyond:
        findings.append(_finding("equity_jump_beyond_band", WARN, beyond, band_pct=BAND_PCT))
    summary = {"sessions": len(equity), "jumps": jumps, "threshold_pct": EQUITY_JUMP_PCT}
    return _check("equity_jumps", summary, findings)


def check_refusals(run: Mapping[str, Any], refusals: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    counted = Counter(r.get("reason", "") for r in refusals)
    stated = {k: v for k, v in (run.get("refusals") or {}).items() if v}
    findings = []
    if dict(counted) != stated:
        diff = [
            {"reason": k, "refusals_csv": counted.get(k, 0), "run_json": stated.get(k, 0)}
            for k in sorted(set(counted) | set(stated))
            if counted.get(k, 0) != stated.get(k, 0)
        ]
        findings.append(_finding("refusal_counts_disagree", FAIL, diff))
    return _check("refusals", {"refusals": sum(counted.values())}, findings)


def check_trials(run: Mapping[str, Any]) -> dict[str, Any]:
    metrics = run.get("metrics") or {}
    trials = metrics.get("dsr_n_trials")
    findings = []
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
        findings.append(_finding("no_trial_count", FAIL, [trials]))
    elif metrics.get("dsr_prob") is None:
        findings.append(_finding("no_dsr", WARN, [trials]))
    return _check("trials", {"dsr_n_trials": trials, "dsr_prob": metrics.get("dsr_prob")}, findings)


def check_data(
    run: Mapping[str, Any],
    data_audit: Mapping[str, Any] | None = None,
    explained: str | None = None,
) -> dict[str, Any]:
    """The run's data audit status; a warning or failure names the data audit's checks that did
    not pass (from `data_audit`, the audit document) and `explained`, the path of the owner's
    explanation of that digest (None: there is none)."""
    audit = (run.get("data") or {}).get("audit") or {}
    status = audit.get("status")
    findings = []
    if status is None:
        findings.append(_finding("no_data_audit", FAIL, []))
    elif status != PASS:
        checks = [
            c.get("name")
            for c in (data_audit or {}).get("checks") or []
            if isinstance(c, Mapping) and c.get("status") != PASS
        ]
        findings.append(
            _finding(
                f"data_audit_{status}",
                WARN if status == WARN else FAIL,
                [audit.get("digest")],
                checks=checks,
                explained=explained,
            )
        )
    return _check("data", {"status": status, "digest": audit.get("digest")}, findings)


def audit_run(
    run: Mapping[str, Any],
    csvs: Mapping[str, str],
    bars: Mapping[tuple[str, date], tuple[Decimal, Decimal]],
    slippage_bps: Decimal,
    *,
    data_audit: Mapping[str, Any] | None = None,
    explained: Path | None = None,
) -> dict[str, Any]:
    """`audit.json` of a completed run: its `run.json` document, its CSV files by name, the
    (low, high) of each (instrument id, date) it filled on, on the price basis it fills on, its
    slippage in bps, and its data audit with the owner's explanation of it (`check_data`). The
    digest is sha256 of the rest of the document."""
    trades = _trades(_rows(csvs.get("trades")))
    checks = [
        check_binding(run),
        check_fills(trades, bars, slippage_bps),
        check_sequence(trades),
        check_pnl(run, trades),
        check_costs(run, trades),
        check_liquidity(trades),
        check_concentration(run, trades),
        check_equity_jumps(run, _rows(csvs.get("equity"))),
        check_refusals(run, _rows(csvs.get("refusals"))),
        check_trials(run),
        check_data(run, data_audit, None if explained is None else str(explained)),
    ]
    body = {
        "audit_version": AUDIT_VERSION,
        "run_id": (run.get("identity") or {}).get("run_id"),
        "status": max((c["status"] for c in checks), key=_RANK.__getitem__),
        "checks": checks,
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return {**body, "digest": hashlib.sha256(text.encode()).hexdigest()}

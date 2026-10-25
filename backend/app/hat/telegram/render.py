"""Message text. Pure functions from plain views to plain text, no Markdown, so no
value can change how a message is read. Numbers are the engine's own Decimals, printed exactly
to two places; nothing here computes a size, a stop or a price (HAT-TG-023).

Alert details are fixed codes and ids by the `Notifier` contract; a value that is not such a
token is printed as `?`, so free text from a broker or a server can never reach the chat
(HAT-TG-014).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..money import exact
from ..time import IST

__all__ = [
    "URGENT_ALERTS",
    "BotDay",
    "CardView",
    "CloseGapRow",
    "GttNight",
    "PositionEventView",
    "PositionView",
    "RejectionRow",
    "StatusView",
    "VetoVerdictView",
    "WatchlistDigest",
    "alert_text",
    "card_text",
    "confidence_line",
    "confirm_text",
    "kill_line",
    "daily_report_text",
    "login_text",
    "mode_text",
    "money",
    "position_event_text",
    "positions_text",
    "rejection_digest_text",
    "shadow_text",
    "status_text",
    "veto_digest_text",
    "watchlist_digest_text",
    "watchlist_text",
]

# 15c: a GTT the nightly check could not confirm, or a triggered GTT that left shares unsold,
# may need the owner before the next open.
# PR 47: a held instrument that changed series (its exits keep running, D210).
# PR 48: a sale resting unfilled, locked at the lower circuit or sent as a band limit.
# PR 36: a trade whose stored state is not the fold of its events (the nightly journal check).
# P5: a job that failed, missed its deadline or hangs; a calendar about to run out; a drawdown
# kill tripped (bots manage-only). A8: a periodic loop (live exits, housekeeping) stopped.
# DB4b: the runtime lock was lost and not taken again (no new entry until a restart; no order
# at all when another runtime holds it).
# Q8: a position `ca.rescale` could not rescale stays manage-only (`corporate_action.pending`).
# An exit view that raised: that position's exit rules are off until it reads again.
# Q11: the broker calls of the day are spent: no new entry until the next trade date.
# A lost entry of an earlier session may hold shares with no position and no stop.
URGENT_ALERTS = frozenset(
    {
        "job.failed",
        "job.missed",
        "job.running_long",
        "periodic.stalled",
        "runtime.lock_lost",
        "calendar.short",
        "kill.drawdown",
        "journal.mismatch",
        "gtt.unverified",
        "gtt.unfilled",
        "fill.excess",
        "fill.off_lifecycle",
        "corporate_action.unhandled",
        "corporate_action.pending",
        "corporate_action.demerger_exit",
        "corporate_action.demerger_late",
        "position.series_changed",
        "position.split_gap",
        "exits.view_failed",
        "position.isin_changed",
        "sale.unfilled",
        "entry.unresolved",
        "broker.budget_spent",
    }
)
_SAFE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_DIGEST_LIMIT = 40


def money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{Decimal(value):.2f}"


def _r(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{Decimal(value):+.2f}R"


def _ist(at: datetime) -> str:
    return at.astimezone(IST).strftime("%H:%M IST")


def _safe(value: object) -> str:
    text = str(value)
    return text if _SAFE.match(text) else "?"


# --- trade cards -------------------------------------------------------------------------------


@dataclass(frozen=True)
class CardView:
    """A signal as the engine fixed it: entry, stop, qty and risk come from `hat_signal`."""

    signal_id: uuid.UUID
    symbol: str
    playbook: str
    entry: Decimal
    stop: Decimal
    qty: int
    risk: Decimal
    max_chase: Decimal | None = None
    expires_at: datetime | None = None
    headline: str | None = None
    pdf_url: str | None = None
    # the signal's confidence label and basis (D132); None on a signal stored before PR 71
    confidence: str | None = None
    basis: Mapping[str, object] = field(default_factory=dict)


def _basis_decimal(raw: object) -> Decimal | None:
    return exact(raw) if isinstance(raw, str | int) else None


def confidence_line(label: str | None, basis: Mapping[str, object]) -> str:
    """The card's confidence line (D132, HAT-TG-023): the label and the backtest bucket it was
    read from, or why there is none. It describes backtest evidence, not this trade's odds."""
    name = label.upper() if label in ("high", "medium", "low") else "LOW"
    trades, mean = basis.get("trades"), _basis_decimal(basis.get("mean_r"))
    if not isinstance(trades, int) or isinstance(trades, bool) or mean is None:
        reason = _safe(basis.get("reason", "no_label"))
        return f"Confidence: {name} - no backtest evidence ({reason})"
    text = f"Confidence: {name} - backtest {trades} similar trades, {_r(mean)}"
    low, high = _basis_decimal(basis.get("ci90_low")), _basis_decimal(basis.get("ci90_high"))
    if low is not None and high is not None:
        text += f" (90% CI {low:+.2f}..{high:+.2f})"
    return text


def _trade_lines(card: CardView) -> list[str]:
    per_share = card.entry - card.stop
    return [
        f"{card.symbol} ({card.playbook})",
        f"Entry {money(card.entry)}  Stop {money(card.stop)}  Qty {card.qty}",
        f"Risk {money(card.risk)}  1R/share {money(per_share)}  "
        f"2R target {money(card.entry + 2 * per_share)}",
        confidence_line(card.confidence, card.basis),
    ]


def card_text(card: CardView) -> str:
    """The confirm-mode approval card. Size, stop and price are fixed; the only controls are
    the Take and Skip buttons."""
    lines = ["Approval needed", *_trade_lines(card)]
    if card.max_chase is not None:
        lines.append(f"Max chase {money(card.max_chase)}")
    if card.headline:
        lines.append(f"Catalyst: {card.headline[:200]}")
    if card.pdf_url:
        lines.append(card.pdf_url[:300])
    if card.expires_at is not None:
        lines.append(f"Expires {_ist(card.expires_at)}")
    return "\n".join(lines)


def shadow_text(card: CardView) -> str:
    """The shadow-mode trade message: what the bot would have bought."""
    return "\n".join(["Shadow trade", *_trade_lines(card)])


@dataclass(frozen=True)
class PositionEventView:
    symbol: str
    qty: int | None
    price: Decimal | None
    r_so_far: Decimal | None
    dividends: Decimal | None = None  # a closed trade's, already in its R (R-DIV)


_EVENT_TITLES = {
    "order.filled": "Filled",
    "exit.partial": "Partial exit",
    "position.closed": "Position closed",
    "stop.moved": "Stop moved",
}


def position_event_text(event_type: str, view: PositionEventView) -> str:
    """A fill, exit or stop move on a real position: price, qty and R so far (and a closed
    trade's dividends, when it earned any)."""
    title = _EVENT_TITLES.get(event_type, event_type)
    qty = "n/a" if view.qty is None else str(view.qty)
    text = (
        f"{title}: {view.symbol}\n"
        f"Price {money(view.price)}  Qty {qty}  R so far {_r(view.r_so_far)}"
    )
    if view.dividends:
        text += f"  incl. dividends {money(view.dividends)}"
    return text


# --- alerts ------------------------------------------------------------------------------------


def alert_text(code: str, trade_date: date, detail: Mapping[str, object]) -> str:
    """A health alert: its code, the trade date and the detail's fixed codes and ids."""
    head = "URGENT: " if code in URGENT_ALERTS else "Alert: "
    lines = [f"{head}{_safe(code)} ({trade_date.isoformat()})"]
    lines += [
        f"{_safe(k)}: {_safe(v)}" for k, v in sorted(detail.items(), key=lambda kv: str(kv[0]))
    ]
    return "\n".join(lines)


# --- digests and reports -----------------------------------------------------------------------


@dataclass(frozen=True)
class RejectionRow:
    bot: str
    symbol: str
    reason: str


def rejection_digest_text(trade_date: date, rows: Sequence[RejectionRow]) -> str:
    lines = [f"Rejected signals {trade_date.isoformat()}: {len(rows)}"]
    lines += [f"{r.bot} {r.symbol}: {_safe(r.reason)}" for r in rows[:_DIGEST_LIMIT]]
    if len(rows) > _DIGEST_LIMIT:
        lines.append(f"... and {len(rows) - _DIGEST_LIMIT} more")
    return "\n".join(lines)


@dataclass(frozen=True)
class BotDay:
    bot: str
    trade_date: date | None
    open_risk: Decimal | None
    realized_r: Decimal | None
    equity: Decimal | None
    equity_peak: Decimal | None


@dataclass(frozen=True)
class GttNight:
    """The last night's `gtt.verify` runs: how many accounts ran and failed, the positions
    whose GTT was confirmed, and the GTTs refreshed or re-placed."""

    runs: int = 0
    failed: int = 0
    verified: int = 0
    refreshed: int = 0


@dataclass(frozen=True)
class CloseGapRow:
    symbol: str
    live_price: str
    official_close: str
    gap_pct: str


def daily_report_text(
    trade_date: date, bots: Sequence[BotDay], gtt: GttNight, gaps: Sequence[CloseGapRow]
) -> str:
    lines = [f"Daily P&L {trade_date.isoformat()}"]
    for b in bots:
        as_of = b.trade_date.isoformat() if b.trade_date else "no snapshot"
        lines.append(
            f"{b.bot} ({as_of}): open risk {money(b.open_risk)}, realised {_r(b.realized_r)}, "
            f"equity {money(b.equity)} vs peak {money(b.equity_peak)}"
        )
    lines.append(
        f"GTT check: {gtt.runs} run(s), {gtt.failed} failed, {gtt.verified} verified, "
        f"{gtt.refreshed} refreshed"
    )
    if gaps:
        lines.append("Close gaps (live vs official close):")
        lines += [
            f"{g.symbol}: {_safe(g.live_price)} vs {_safe(g.official_close)} ({_safe(g.gap_pct)}%)"
            for g in gaps[:_DIGEST_LIMIT]
        ]
    return "\n".join(lines)


@dataclass(frozen=True)
class VetoVerdictView:
    decisions: int
    skipped_mean: Decimal | None
    taken_mean: Decimal | None
    verdict: str


def veto_digest_text(
    start: date,
    end: date,
    avoided: Decimal,
    missed: Decimal,
    trades: int,
    verdict: VetoVerdictView | None,
) -> str:
    lines = [
        f"Veto digest {start.isoformat()} to {end.isoformat()}",
        f"Skips closed: {trades}  avoided {_r(avoided)}  missed {_r(missed)}",
    ]
    if verdict is not None:
        lines.append(
            f"After {verdict.decisions} decisions: skipped mean {_r(verdict.skipped_mean)}, "
            f"taken mean {_r(verdict.taken_mean)}: {verdict.verdict}"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class WatchlistDigest:
    bot: str
    trade_date: date
    regime: str | None
    new: tuple[str, ...]
    dropped: tuple[str, ...]
    top: tuple[tuple[str, Decimal], ...]


def watchlist_digest_text(digest: WatchlistDigest) -> str:
    lines = [
        f"Watchlist {digest.bot} {digest.trade_date.isoformat()} (regime {digest.regime or 'n/a'})",
        f"New: {', '.join(digest.new) or '-'}",
        f"Dropped: {', '.join(digest.dropped) or '-'}",
        "Top:",
    ]
    lines += [f"{i}. {symbol} {score:.3f}" for i, (symbol, score) in enumerate(digest.top, start=1)]
    return "\n".join(lines)


def login_text(
    links: Iterable[tuple[str, str | None]], *, repeat: bool, open_positions: int = 0
) -> str:
    """The login prompt, one link per line after the head. The repeat with real open positions
    is URGENT: their broker stops still rest, but breakeven, trim and the MA exits run only on a
    live session (HAT-TG-021)."""
    head = "Still not logged in:" if repeat else "Log in to your broker for today:"
    if repeat and open_positions > 0:
        head = (
            f"URGENT: still not logged in. {open_positions} open position(s) have broker stops "
            "only: breakeven, trim and MA exits are off until you log in:"
        )
    lines = [head]
    for label, link in links:
        lines.append(f"{label}: {link or 'login link unavailable'}")
    return "\n".join(lines)


# --- command replies ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusView:
    sessions: tuple[tuple[str, str], ...]
    feed: str
    kill_account: bool
    bots: tuple[tuple[str, str, str, str], ...] = field(default=())  # name, mode, state, regime


# Why a bot runs a mode other than the one asked for (`execution.modes.ModeView.why`).
MODE_REASONS: Mapping[str, str] = {
    "no_gtt_delivery": "account not active or no GTT for delivery",
    "ddpi_not_confirmed": "DDPI not confirmed",
    "weights_not_confirmed": "base weights not confirmed",
    "unknown_mode": "unknown mode",
}


def mode_text(
    asked: str, effective: str, why: str | None, pending: str | None, pending_from: datetime | None
) -> str:
    """`confirm`, or `shadow (asked confirm: DDPI not confirmed)`, then the queued change:
    `; auto from 2026-10-23 09:15 IST`."""
    text = effective
    if effective != asked:
        if why is not None and why.startswith("sleeve_"):
            reason = f"sleeve {why.removeprefix('sleeve_')}"
        else:
            reason = MODE_REASONS.get(why or "", "?")
        text = f"{effective} (asked {asked}: {reason})"
    if pending is not None and pending_from is not None:
        when = pending_from.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        text += f"; {pending} from {when}"
    return text


def status_text(view: StatusView) -> str:
    lines = ["Status"]
    lines += [f"Broker {label}: {state}" for label, state in view.sessions] or ["Broker: none"]
    lines.append(f"Feed: {view.feed}")
    lines.append(f"Account kill: {'TRIPPED' if view.kill_account else 'armed'}")
    lines += [
        f"{name}: {mode}, {state}, regime {regime}" for name, mode, state, regime in view.bots
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class PositionView:
    bot: str
    symbol: str
    qty: int
    entry: Decimal
    stop: Decimal
    r_so_far: Decimal | None
    days_held: int | None
    virtual: bool


def positions_text(rows: Sequence[PositionView]) -> str:
    if not rows:
        return "No open positions"
    lines = [f"Open positions: {len(rows)}"]
    for p in rows[:_DIGEST_LIMIT]:
        days = "n/a" if p.days_held is None else str(p.days_held)
        tag = " (shadow)" if p.virtual else ""
        lines.append(
            f"{p.bot} {p.symbol}{tag}: qty {p.qty}, entry {money(p.entry)}, stop {money(p.stop)}, "
            f"{_r(p.r_so_far)}, {days} day(s)"
        )
    return "\n".join(lines)


def watchlist_text(bot: str, trade_date: date | None, rows: Sequence[tuple[str, Decimal]]) -> str:
    if trade_date is None:
        return f"{bot}: no watchlist"
    lines = [f"Watchlist {bot} {trade_date.isoformat()}: {len(rows)}"]
    lines += [f"{symbol} {score:.3f}" for symbol, score in rows[:_DIGEST_LIMIT]]
    return "\n".join(lines)


_CONFIRM_TEXT = {
    "flatten": (
        "Flatten closes every open real position at market and cancels the working entries. It"
        " trips the account kill switch, even with the market closed (then nothing is sold):"
        " every bot stays manage-only until /arm. This is logged as a discipline breach."
    ),
    "arm": (
        "Re-arm these kill switches? A re-arm restarts the drawdown peak at the equity then;"
        " only these are re-armed:"
    ),
    "mode_auto": (
        "Switch to auto mode from the next session? Real orders will go out without a card."
    ),
}


def confirm_text(action: str, expires_at: datetime, lines: Sequence[str] = ()) -> str:
    body = "\n".join([_CONFIRM_TEXT.get(action, action), *lines])
    return f"{body}\nConfirm before {_ist(expires_at)}."


def kill_line(name: str, reason: str, tripped_at: datetime) -> str:
    """One kill an `/arm` lists: whose, why and when it tripped."""
    when = tripped_at.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
    return f"- {name}: {_safe(reason)}, tripped {when}"

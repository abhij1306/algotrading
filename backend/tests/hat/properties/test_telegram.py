"""PBT telegram.* (08; HAT-TG-005..050, HAT-DEC-T15, HAT-DEC-I13, HAT-SCH-056, HAT-OPS-071): the
Telegram service, event relay, notifier, reports and command log of `app.hat.telegram`, on
SQLite, over a recording fake of the Bot API transport (`hat_telegram_support.FakeTransport`)
and the real router over the Fyers adapter and `FakeBroker`. Nothing reaches Telegram or a
broker."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hat.fakes.broker import FakeBroker, rejected
from hat_engine_support import request_for
from hat_exec_support import confirm_gates
from hat_manage_support import ManageRig
from hat_manage_support import at as manage_at
from hat_risk_support import CALENDAR
from hat_support import memory_engine
from hat_telegram_support import (
    AT,
    BOT,
    BOT_USERNAME,
    CHAT,
    LIVE,
    OWNER,
    TRADE_DAY,
    FakeTransport,
    TgRig,
    button_update,
    ist,
    text_update,
)
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy import event as sa_event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.hat.engine import approvals
from app.hat.events import record
from app.hat.execution import modes
from app.hat.execution.broker import Side
from app.hat.execution.router import PlacementCheck
from app.hat.models import (
    HatApproval,
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatCommandLog,
    HatDailyBotSnapshot,
    HatEvent,
    HatInstrument,
    HatJobRun,
    HatMarket,
    HatModeChange,
    HatOrder,
    HatPosition,
    HatSignal,
    HatTelegramMessage,
    HatWatchlistEntry,
)
from app.hat.ops.jobs import JobResult, RunStatus, run_job
from app.hat.ops.retention import purge_job
from app.hat.risk.sessions import SessionCalendar, SessionState
from app.hat.telegram import reads, store
from app.hat.telegram.callbacks import ManagerFlattener
from app.hat.telegram.client import BotClient, HttpsTransport, TelegramError
from app.hat.telegram.commands import COMMANDS, Reply, parse
from app.hat.telegram.confirm import CONFIRM_WINDOW, PendingConfirmations
from app.hat.telegram.notifier import RELAYED_CODES
from app.hat.telegram.relay import GAP_WAIT, POSITION_EVENTS, RELAY_LAG, EventRelay
from app.hat.telegram.render import URGENT_ALERTS
from app.hat.telegram.reports import Reports, is_week_end, telegram_jobs
from app.hat.telegram.service import UNKNOWN_CHAT, TelegramService
from app.hat.telegram.tokens import SKIP, TAKE, ApprovalSigner, token_hash
from app.hat.time import IST, as_utc, utc_now

ENGINE = memory_engine(HatTelegramMessage, HatCommandLog, HatJobRun)
FACTORY = sessionmaker(bind=ENGINE)
JOB = "retention.purge"

days = st.dates(min_value=date(2024, 1, 1), max_value=date(2040, 12, 31))
stamps = st.lists(
    st.tuples(st.integers(min_value=-5, max_value=900), st.integers(min_value=0, max_value=23)),
    max_size=12,
)


def instant(day: date, age: tuple[int, int]) -> datetime:
    days_ago, hour = age
    return datetime.combine(day - timedelta(days=days_ago), datetime.min.time(), tzinfo=UTC) + (
        timedelta(hours=hour)
    )


@given(day=days, messages=stamps, commands=stamps)
def test_retention_purge(
    day: date, messages: list[tuple[int, int]], commands: list[tuple[int, int]]
) -> None:
    """After the `retention.purge` job runs for a date d, each chat table holds exactly the rows
    stamped on or after d minus one year (IST date), the job is recorded as succeeded once, and
    running it again for the same date does not touch the tables."""
    with FACTORY() as session:
        session.query(HatTelegramMessage).delete()
        session.query(HatCommandLog).delete()
        session.query(HatJobRun).delete()
        for number, age in enumerate(messages):
            session.add(
                HatTelegramMessage(
                    chat_id=7, kind="card", sent_at=instant(day, age), message_id=number
                )
            )
        for age in commands:
            session.add(
                HatCommandLog(
                    chat_id=7,
                    command="/status",
                    accepted=True,
                    received_at=instant(day, age),
                )
            )
        session.commit()

    def body() -> JobResult:
        with FACTORY() as session:
            return purge_job(session, day)

    first = run_job(JOB, day, body, session_factory=FACTORY)
    assert first.status is RunStatus.SUCCEEDED
    assert first.executed

    floor = date(day.year - 1, day.month, 28 if (day.month, day.day) == (2, 29) else day.day)
    with FACTORY() as session:
        kept_messages = sorted(
            as_utc(r.sent_at) for r in session.scalars(select(HatTelegramMessage))
        )
        kept_commands = sorted(
            as_utc(r.received_at) for r in session.scalars(select(HatCommandLog))
        )
    expected_messages = sorted(
        t for t in (instant(day, a) for a in messages) if t.astimezone(IST).date() >= floor
    )
    expected_commands = sorted(
        t for t in (instant(day, a) for a in commands) if t.astimezone(IST).date() >= floor
    )
    assert kept_messages == expected_messages
    assert kept_commands == expected_commands
    assert first.rows == len(messages) + len(commands) - len(expected_messages) - len(
        expected_commands
    )

    second = run_job(JOB, day, body, session_factory=FACTORY)
    assert not second.executed
    assert second.rows == first.rows


# --- shared helpers for the PR 16 properties ---------------------------------------------------

D = Decimal
# Rows the service writes for every update (its log, its message links, its cursors).
CHAT_TABLES = {"hat_command_log", "hat_telegram_message", "hat_telegram_cursor"}
EVENING = ist(TRADE_DAY, 20)  # after the close: /mode may be queued
STRANGERS = st.integers(min_value=-(10**12), max_value=10**12).filter(lambda c: c != CHAT)
ANY_TEXT = st.text(max_size=40)
COMMAND_WORDS = st.sampled_from(
    ("auto", "shadow", "confirm", "signal", "live", "fyers", "all", "SBIN", "10", "95.5", "now")
)
COMMAND_TEXTS = st.one_of(
    st.tuples(st.sampled_from(COMMANDS), st.lists(COMMAND_WORDS, max_size=3)).map(
        lambda c: " ".join([c[0], *c[1]])
    ),
    ANY_TEXT,
)
SLOW = settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def wall_relay(rig: TgRig) -> EventRelay:
    """The relay on the wall clock (events are stamped with it), its cursor placed at the newest
    event so only what follows is relayed."""
    relay = EventRelay(rig.client, rig.factory, rig.signer, clock=lambda: utc_now() + RELAY_LAG * 2)
    relay.run_once()
    return relay


def card_of(rig: TgRig, relay: EventRelay, instrument: int = 0) -> tuple[uuid.UUID, str, str]:
    """Route a confirm signal and relay its card: (signal id, Take data, Skip data)."""
    outcome = rig.route(instrument)
    relay.run_once()
    card = [p for p in rig.transport.named("sendMessage") if "reply_markup" in p][-1]
    [[take, skip]] = card["reply_markup"]["inline_keyboard"]
    assert (take["text"], skip["text"]) == ("Take", "Skip")
    return outcome.signal_id, take["callback_data"], skip["callback_data"]


def last_button(rig: TgRig) -> str:
    """The callback data of the newest message with a button (a Confirm)."""
    payload = [p for p in rig.transport.named("sendMessage") if "reply_markup" in p][-1]
    return payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]


def state(rig: TgRig) -> dict[str, list[str]]:
    """Every HAT table but the chat's own rows, as sorted row reprs."""
    out = {}
    with rig.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name.startswith("hat_") and table.name not in CHAT_TABLES:
                out[table.name] = sorted(repr(tuple(row)) for row in conn.execute(table.select()))
    return out


def all_text(rig: TgRig) -> str:
    """Every value of every HAT table, as one string."""
    parts = []
    with rig.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            for row in conn.execute(table.select()):
                parts.extend(str(value) for value in row)
    return "\n".join(parts)


def events_of(rig: TgRig, after: int = 0) -> list[HatEvent]:
    with rig.factory() as session:
        return list(
            session.scalars(select(HatEvent).where(HatEvent.id > after).order_by(HatEvent.id))
        )


def newest_event(rig: TgRig) -> int:
    events = events_of(rig)
    return events[-1].id if events else 0


def alert_code(text: str) -> str:
    """The code of a health alert message ("Alert: <code> (<date>)")."""
    return text.split("\n", 1)[0].split(": ", 1)[1].rsplit(" (", 1)[0]


# --- HAT-DEC-T15, HAT-TG-024: one alert per health failure -------------------------------------

HEALTH = st.lists(
    st.sampled_from(
        (
            "quality.failed",
            "feed.stale",
            "broker.session_lost",
            "broker.capabilities_changed",
            "corporate_action.unhandled",
        )
    ),
    max_size=4,
)
DIRECT = st.lists(
    st.sampled_from(
        (
            "stop.not_placed",
            "stop.not_moved",
            "gtt.swap_failed",
            "gtt.missing",
            "gtt.unverified",
            "gtt.unfilled",
            "fill.excess",
            "entry.unresolved",
            "fill.refused",
            "exit.not_placed",
            "reconcile.mismatch",
            "order.rejected",
            "quality_failed",
        )
    ),
    max_size=4,
)
_HEALTH_AGGREGATE = {
    "quality.failed": ("ingest", "scheduler"),
    "feed.stale": ("session", "engine"),
    "broker.session_lost": ("session", "broker"),
    "broker.capabilities_changed": ("session", "broker"),
    "corporate_action.unhandled": ("position", "scheduler"),
}


def _held_position(rig: TgRig) -> HatPosition:
    return HatPosition(
        bot_id=rig.world.bot_id,
        signal_id=uuid.uuid4(),
        instrument_id=rig.world.instruments[0],
        is_virtual=False,
        qty_open=10,
        avg_entry=Decimal(100),
        initial_stop=Decimal(95),
        current_stop=Decimal(95),
        currency="INR",
        opened_at=ist(TRADE_DAY, 9, 0),
        state="Open",
        rules_fired=[],
    )


@SLOW
@given(HEALTH, st.integers(min_value=0, max_value=3), DIRECT)
def test_health_alert_for_every_failure(
    health: list[str], rejections: int, direct: list[str]
) -> None:
    """HAT-DEC-T15, HAT-OPS-071: every `quality.failed`, `feed.stale`, `broker.session_lost`,
    `broker.capabilities_changed`, `corporate_action.unhandled` and `order.rejected` event
    becomes exactly one health alert in the chat, and every other alert the services raise
    becomes one message; an alert whose event the relay sends is not sent twice.
    `gtt.unverified`, `gtt.unfilled`, `fill.excess` and `corporate_action.unhandled` (HAT-EXEC-144)
    are URGENT. Harm: the owner does
    not learn that a bot went manage-only or an order failed, or learns it twice and starts
    ignoring alerts."""
    fake = FakeBroker()
    fake.script("place_order", *[rejected("Insufficient funds")] * rejections)
    rig = TgRig(mode="auto", fake=fake, telegram_alerts=True)
    relay = wall_relay(rig)
    rig.route()  # auto: the entry is sent, rejected up to twice
    with rig.factory() as session:
        for event_type in health:
            aggregate, actor = _HEALTH_AGGREGATE[event_type]
            aggregate_id, signal_id = uuid.uuid4(), None
            if aggregate == "position":  # a position event names the position and its signal
                position = _held_position(rig)
                session.add(position)
                session.flush()
                aggregate_id, signal_id = position.id, position.signal_id
            record(
                session,
                aggregate_type=aggregate,
                aggregate_id=aggregate_id,
                event_type=event_type,
                actor=actor,
                payload={"status": "expired"},
                correlation_id=signal_id,
            )
        session.commit()
    for code in direct:
        rig.notifier.alert(code, TRADE_DAY, {"position_id": str(uuid.uuid4())})
    relay.run_once()
    sent = [t for t in rig.transport.texts() if t.startswith(("Alert: ", "URGENT: "))]
    expected = Counter(health) + Counter(c for c in direct if c not in RELAYED_CODES)
    expected["order.rejected"] += min(rejections, 2)
    if rejections >= 2:
        expected["entry.not_placed"] += 1
    assert Counter(alert_code(t) for t in sent) == +expected
    assert {
        "gtt.unverified",
        "gtt.unfilled",
        "fill.excess",
        "corporate_action.unhandled",
    } <= URGENT_ALERTS
    for text in sent:
        assert text.startswith("URGENT: ") == (alert_code(text) in URGENT_ALERTS)


@SLOW
@given(
    st.integers(min_value=2, max_value=5),
    st.integers(min_value=0, max_value=4),
    st.sampled_from((0, 5, 59, 61, 180)),
)
@example(count=3, held=0, minutes=5)  # committed late, inside the wait: still sent
@example(count=3, held=0, minutes=61)  # after the wait: dropped
def test_relay_late_commit(count: int, held: int, minutes: int) -> None:
    """HAT-DEC-T15, HAT-TG-024: an event whose transaction commits after the relay has passed
    its id (a lower id committed late) is still relayed, exactly once, if it commits within
    `GAP_WAIT`; the events around it are relayed once each, and an id that never fills is
    given up after `GAP_WAIT`. Harm: a health alert or approval card held by a slow
    transaction is lost for good."""
    held = held % count
    rig = TgRig()
    clock = {"now": utc_now() + RELAY_LAG * 2}
    relay = EventRelay(rig.client, rig.factory, rig.signer, clock=lambda: clock["now"])
    relay.run_once()  # the cursor starts at the newest event
    aggregate, actor = _HEALTH_AGGREGATE["feed.stale"]
    with rig.factory() as session:
        for _ in range(count):
            record(
                session,
                aggregate_type=aggregate,
                aggregate_id=uuid.uuid4(),
                event_type="feed.stale",
                actor=actor,
                payload={"status": "stale"},
            )
        session.commit()
        rows = list(session.scalars(select(HatEvent).where(HatEvent.event_type == "feed.stale")))
        late = rows[held]
        kept = {c.name: getattr(late, c.name) for c in HatEvent.__table__.columns}
        session.delete(late)  # not yet committed, as far as the relay can see
        session.commit()
    relay.run_once()
    assert len(rig.transport.texts()) == count - 1
    with rig.factory() as session:
        session.add(HatEvent(**kept))  # the slow transaction commits, under its original id
        session.commit()
    clock["now"] += timedelta(minutes=minutes)
    relay.run_once()
    relay.run_once()
    passed_over = held < count - 1  # the last one is simply after the cursor
    expected = count if not passed_over or timedelta(minutes=minutes) <= GAP_WAIT else count - 1
    texts = rig.transport.texts()
    assert len(texts) == expected
    assert all("feed.stale" in text for text in texts)


# --- HAT-TG-005, HAT-DEC-I13: other chats change nothing ---------------------------------------


@SLOW
@given(
    STRANGERS,
    st.one_of(
        COMMAND_TEXTS.map(lambda t: ("text", t)),
        st.sampled_from(("take", "skip", "confirm")).map(lambda b: ("button", b)),
    ),
)
@example(chat=CHAT + 1, inbound=("text", "/kill"))
@example(chat=CHAT + 1, inbound=("button", "take"))
def test_unknown_chat_has_no_effect(chat: int, inbound: tuple[str, str]) -> None:
    """HAT-TG-005, HAT-DEC-I13: an update from any chat but the allowlisted one, even a valid
    Take of a live card or the Confirm of a pending flatten, changes no table but one refused
    `hat_command_log` row (`unknown_chat`), gets no reply, no answer and no edit, and calls no
    broker. Harm: a stranger controls the bots."""
    rig = TgRig()
    relay = wall_relay(rig)
    _signal_id, take, skip = card_of(rig, relay)
    rig.service.handle(text_update(1, CHAT, "/flatten"))  # the owner's pending confirmation
    buttons = {"take": take, "skip": skip, "confirm": last_button(rig)}
    before, outbound = state(rig), len(rig.transport.outbound())
    logged, broker = len(rig.log()), len(rig.fake.calls)
    kind, value = inbound
    update = (
        text_update(2, chat, value) if kind == "text" else button_update(2, chat, buttons[value])
    )
    handled = rig.service.handle(update)
    assert handled.reply is None
    assert state(rig) == before
    assert len(rig.transport.outbound()) == outbound
    assert rig.fake.calls[broker:] == []
    [row] = rig.log()[logged:]
    assert (row.chat_id, row.accepted, row.reason) == (chat, False, "unknown_chat")


# --- HAT-TG-012, HAT-SCH-074: one log row per update ------------------------------------------


@SLOW
@given(COMMAND_TEXTS, st.sampled_from((AT, EVENING, ist(date(2026, 10, 24), 11))), st.booleans())
def test_every_command_logged(text: str, now: datetime, killed: bool) -> None:
    """HAT-TG-012, HAT-SCH-074: whatever the text and the system state, handling one message
    writes exactly one `hat_command_log` row with the sender's chat id; a refused one carries a
    reason and changed nothing. Harm: discipline metrics and audits miss user actions."""
    rig = TgRig().at(now)
    relay = wall_relay(rig)
    card_of(rig, relay)
    if killed:
        rig.service.handle(text_update(1, CHAT, "/kill"))
    before, logged = state(rig), len(rig.log())
    handled = rig.service.handle(text_update(2, CHAT, text))
    [row] = rig.log()[logged:]
    assert handled.reply is not None
    assert row.chat_id == CHAT
    assert row.accepted == handled.reply.accepted
    if not row.accepted:
        assert row.reason
        assert state(rig) == before


# --- HAT-TG-030: the closed command set ---------------------------------------------------------

UNPARSED = st.one_of(
    ANY_TEXT.filter(lambda t: parse(t) is None),
    st.sampled_from(COMMANDS).map(lambda c: " ".join([c, *["x"] * 5])),
    st.sampled_from(
        (
            "/buy SBIN 10",
            "/sell SBIN",
            "/take",
            "/order SBIN 100 95",
            "/start",
            "/help",
            "/mode",
            "/mode auto",
            "buy SBIN",
            "/kill now",
            "/flatten SBIN",
            "/regime off",
            "/gate off",
        )
    ),
)


@SLOW
@given(UNPARSED, st.sampled_from((AT, EVENING)))
def test_unknown_command_rejected(text: str, now: datetime) -> None:
    """HAT-TG-030, HAT-DEC-B12: a text that is not one of the ten commands with its arguments
    (an order, a gate override, a command with extra words) is refused (`unknown_command` or
    `bad_arguments`) and changes nothing. Harm: malformed input triggers an action."""
    rig = TgRig().at(now)
    card_of(rig, wall_relay(rig))
    before, broker = state(rig), len(rig.fake.calls)
    handled = rig.service.handle(text_update(1, CHAT, text))
    assert handled.reply is not None
    assert not handled.reply.accepted
    assert handled.reply.reason in {"unknown_command", "bad_arguments", "not_a_command"}
    assert state(rig) == before
    assert rig.fake.calls[broker:] == []


SAFETY_TEXTS = st.sampled_from(
    (
        "/kill",
        "/flatten",
        "/arm",
        "/pause",
        "/resume",
        f"/mode {BOT} auto",
        f"/mode {BOT} shadow",
        "/status",
    )
)
BOT_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=32,
)


def _cased(name: str, upper: list[bool]) -> str:
    return "".join(ch.upper() if up else ch.lower() for ch, up in zip(name, upper, strict=False))


@SLOW
@given(
    SAFETY_TEXTS,
    BOT_NAMES.filter(lambda n: n.lower() != BOT_USERNAME.lower()),
    st.lists(st.booleans(), min_size=len(BOT_USERNAME), max_size=len(BOT_USERNAME)),
    st.sampled_from((AT, EVENING)),
)
@example("/kill", BOT_USERNAME + "x", [False] * len(BOT_USERNAME), AT)
def test_other_bot_ignored(text: str, other: str, upper: list[bool], now: datetime) -> None:
    """HAT-TG-030: in a group, a command addressed to another bot (`/kill@other_bot`) is
    ignored: one refused row (`other_bot`), no reply, no state change, no broker call. The same
    command addressed to this bot, its `getMe` username in any case, runs as if unsuffixed; while
    `getMe` fails, every suffixed command is refused (fail closed).
    Harm: a kill, flatten or re-arm meant for another bot acts on this one."""
    rig = TgRig().at(now)
    card_of(rig, wall_relay(rig))
    command, *args = text.split()
    before, broker, sent = state(rig), len(rig.fake.calls), len(rig.transport.texts())
    logged = len(rig.log())
    handled = rig.service.handle(text_update(1, CHAT, " ".join([f"{command}@{other}", *args])))
    [row] = rig.log()[logged:]
    assert handled.reply is not None
    assert not handled.reply.accepted
    assert handled.reply.reason == row.reason == "other_bot"
    assert row.command == command
    assert state(rig) == before
    assert rig.fake.calls[broker:] == []
    assert rig.transport.texts()[sent:] == []

    own = rig.service.handle(text_update(2, CHAT, f"/kill@{_cased(BOT_USERNAME, upper)}"))
    plain = TgRig().at(now)
    card_of(plain, wall_relay(plain))
    plain.transport.failing.add("getMe")  # its own name unknown: every suffix is refused
    blind = plain.service.handle(text_update(1, CHAT, f"/kill@{BOT_USERNAME}"))
    assert blind.reply is not None
    assert blind.reply.reason == "other_bot"
    plain.transport.failing.clear()
    unsuffixed = plain.service.handle(text_update(2, CHAT, "/kill"))
    assert own.reply is not None
    assert unsuffixed.reply is not None
    assert (own.command, own.reply.accepted, own.reply.reason) == (
        unsuffixed.command,
        unsuffixed.reply.accepted,
        unsuffixed.reply.reason,
    )
    assert own.reply.accepted
    assert state(rig) != before


# --- HAT-TG-031: no order from text -------------------------------------------------------------


@SLOW
@given(st.lists(st.one_of(COMMAND_TEXTS, st.sampled_from(("take", "yes", "TAKE"))), max_size=6))
def test_no_free_text_orders(texts: list[str]) -> None:
    """HAT-TG-031: whatever text arrives, even the Take button's own callback data typed as a
    message while a card is open, no order is placed and no entry `hat_order` is written. Harm:
    a typo or a forged message opens a position outside the risk policy."""
    rig = TgRig()
    _signal_id, take, _skip = card_of(rig, wall_relay(rig))
    for n, text in enumerate([*texts, take], start=1):
        rig.service.handle(text_update(n, CHAT, text))
    assert rig.places() == []
    with rig.factory() as session:
        assert session.scalars(select(HatOrder).where(HatOrder.purpose == "entry")).all() == []


# --- HAT-TG-011: none dropped, none applied twice --------------------------------------------


class CrashError(BaseException):
    """The process dies here: not an `Exception`, so no handler's error branch catches it."""


class CrashingService(TelegramService):
    """The service of one process life that dies at `crash`: (update id, where), where is
    `before` (the update has not run), `commit` (at `handle`'s last commit, the one that writes
    the command-log row and the offset; a handler's own earlier commits stand) or `after` (the
    update is done, its reply not yet sent)."""

    def __init__(self, rig: TgRig, crash: tuple[int, str] | None) -> None:
        super().__init__(
            rig.client,
            self._session,
            signer=rig.signer,
            confirmations=PendingConfirmations(),  # in memory: a new process starts with none
            desk=rig.desk,
            flattener=rig.flattener,
            clock=lambda: rig.now,
        )
        self._real = rig.factory
        self._crash = crash
        self._dying = False

    def _session(self) -> Session:
        session = self._real()
        if self._dying:
            commit = session.commit

            @sa_event.listens_for(session, "before_flush")
            def seen(flushing: Session, _context: object, _instances: object) -> None:
                if any(isinstance(row, HatCommandLog) for row in flushing.new):
                    flushing.info["logged"] = True

            def dying_commit() -> None:
                # the command-log row is flushed when written: the commit after that is the
                # one that writes it and the offset; a handler's earlier commits come before
                if session.info.get("logged"):
                    raise CrashError
                commit()

            session.commit = dying_commit  # type: ignore[method-assign]
        return session

    def handle(self, update: dict) -> object:  # type: ignore[override]
        where = self._crash[1] if self._crash and self._crash[0] == update["update_id"] else None
        if where == "before":
            raise CrashError
        self._dying = where == "commit"
        handled = super().handle(update)
        if where == "after":
            raise CrashError
        return handled


CRASH_POINTS = ("before", "commit", "after")
# What each update is: a text command, a message from another chat, a command whose handler
# raises (the error row), one that fails before any handler runs (`poison`: its refusal cannot
# be written), one whose first try meets a database error that passes (`flaky`), or the Take of
# a card (only the first one drawn; later ones are text).
KINDS = ("text", "text", "stranger", "failing", "poison", "flaky", "take")


class FailingService(CrashingService):
    """Its handler raises an ordinary error for the updates in `failing`, a passing database
    error on the first try of those in `flaky` (recorded in `flaked`, shared across process
    lives), and its unknown-chat refusal raises for those in `poison`."""

    def __init__(
        self,
        rig: TgRig,
        crash: tuple[int, str] | None,
        failing: set[int],
        poison: set[int] = frozenset(),  # type: ignore[assignment]
        flaky: set[int] = frozenset(),  # type: ignore[assignment]
        flaked: set[int] | None = None,
    ) -> None:
        super().__init__(rig, crash)
        self._failing = failing
        self._poison = poison
        self._flaky = flaky
        self._flaked = flaked if flaked is not None else set()
        self._current: int | None = None

    def _refuse_stranger(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
        if self._current in self._poison:
            raise ValueError("the refusal cannot be written")
        return super()._refuse_stranger(*args, **kwargs)  # type: ignore[arg-type]

    def handle(self, update: dict) -> object:  # type: ignore[override]
        self._current = update["update_id"]
        return super().handle(update)

    def _run(self, *args: object, **kwargs: object) -> tuple[str, str | None, Reply]:  # type: ignore[override]
        if self._current in self._failing:
            raise RuntimeError("handler failed")
        if self._current in self._flaky and self._current not in self._flaked:
            self._flaked.add(self._current)
            raise OperationalError("SELECT 1", {}, Exception("lock timeout"))
        return super()._run(*args, **kwargs)  # type: ignore[arg-type]


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(min_value=1, max_value=4), st.sampled_from(KINDS)),
        min_size=1,
        max_size=6,
    ),
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=5), st.sampled_from(CRASH_POINTS)),
        max_size=3,
    ),
)
@example(updates=[(1, "take"), (1, "text")], crashes=[(0, "commit")])
@example(updates=[(1, "text"), (1, "text")], crashes=[(1, "before")])
@example(updates=[(1, "failing"), (1, "stranger"), (1, "text")], crashes=[(0, "commit")])
@example(updates=[(1, "poison"), (1, "flaky"), (1, "text")], crashes=[])
def test_offset_commits_with_update(
    updates: list[tuple[int, str]], crashes: list[tuple[int, str]]
) -> None:
    """HAT-TG-011: for every sequence of updates (text commands, messages from other chats,
    commands whose handler fails, updates that fail before a handler runs, commands that meet a
    passing database error once, a Take button) and every crash point in the poll loop (before
    an update runs, at the commit of its command-log row and offset, after it), polling again
    until the queue is done (backing off after a database error) leaves every update logged:
    each text command (the retried ones included), stranger and failure exactly once, the Take
    placing exactly one entry and accepted at most once (a Take whose decision committed before
    the crash comes back as `already_decided`), and the offset past the last update. Harm: a
    dropped `/kill` does not act, or one bad update holds every later one back; a replayed
    `/flatten` confirmation or Take acts twice."""
    rig = TgRig()
    ids, kinds = [], {}
    for gap, kind in updates:
        ids.append((ids[-1] if ids else 0) + gap)
        kinds[ids[-1]] = "text" if kind == "take" and "take" in kinds.values() else kind
    take = card_of(rig, wall_relay(rig))[1] if "take" in kinds.values() else ""
    queued = {
        "text": lambda i: text_update(i, CHAT, f"/watchlist u{i}"),
        "flaky": lambda i: text_update(i, CHAT, f"/watchlist u{i}"),
        "failing": lambda i: text_update(i, CHAT, f"/watchlist f{i}"),
        "stranger": lambda i: text_update(i, CHAT + 1, f"/watchlist s{i}"),
        "poison": lambda i: text_update(i, CHAT + 1, f"/watchlist p{i}"),
        "take": lambda i: button_update(i, CHAT, take),
    }
    rig.transport.queued = [queued[kinds[i]](i) for i in ids]
    of = {kind: {i for i in ids if kinds[i] == kind} for kind in KINDS}
    flaked: set[int] = set()
    plan = [(ids[n % len(ids)], where) for n, where in crashes]
    for _round in range(len(plan) + len(of["flaky"]) + 2):
        crash = plan.pop(0) if plan else None
        service = FailingService(rig, crash, of["failing"], of["poison"], of["flaky"], flaked)
        try:
            service.poll_once(timeout=0)
        except CrashError:
            pass
        except OperationalError:
            if crash is not None:
                plan.insert(0, crash)  # the run backed off before reaching the crash point
    rows = rig.log()
    by_reason = Counter(row.reason for row in rows)
    texts = [row.args for row in rows if row.command not in ("take", "text") and row.args]
    assert sorted(texts) == sorted(f"u{i}" for i in of["text"] | of["flaky"])
    assert by_reason["error"] == len(of["failing"]) + len(of["poison"])
    assert by_reason[UNKNOWN_CHAT] == len(of["stranger"])
    with rig.factory() as session:
        assert store.cursor(session, store.UPDATES) == ids[-1] + 1
    takes = [row for row in rows if row.command == "take"]
    if "take" in kinds.values():
        assert takes
        assert [row.accepted for row in takes].count(True) <= 1
        assert {row.reason for row in takes if not row.accepted} <= {"already_decided"}
        assert len(rig.places()) == 1
    else:
        assert takes == []


# --- HAT-TG-013: user events are attributed -----------------------------------------------------

USER_EVENTS = frozenset(
    {
        "bot.paused",
        "bot.resumed",
        "kill.tripped",
        "kill.rearmed",
        "mode.change_requested",
        "signal.approved",
        "signal.skipped",
        "flatten.executed",
    }
)
CHANGING = ("/pause", "/resume", "/kill", f"/mode {BOT} shadow", "take", "skip", "confirm")
USER_ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(
            (
                "/pause",
                "/resume",
                "/kill",
                "/arm",
                f"/mode {BOT} shadow",
                f"/mode {BOT} auto",
                "/flatten",
            )
            + ("take", "skip", "confirm")
        ),
        st.sampled_from((AT, EVENING)),
    ),
    min_size=1,
    max_size=8,
)


@SLOW
@given(USER_ACTIONS)
@example([("/flatten", AT), ("confirm", AT)])  # a confirmed flatten (rarely drawn in 15)
def test_user_events_attributed(actions: list[tuple[str, datetime]]) -> None:
    """HAT-TG-013: every event a command or a button press records about the user's action
    (pause, resume, kill, re-arm, mode change, Take, Skip, flatten) has `actor = 'user'` and the
    sender's chat id in its payload, and every accepted state change records one. Harm: skips,
    pauses and flattens cannot be counted in the discipline metrics."""
    rig = TgRig()
    _signal_id, take, skip = card_of(rig, wall_relay(rig))
    for n, (action, now) in enumerate(actions, start=1):
        rig.at(now)
        mark = newest_event(rig)
        if action in ("take", "skip"):
            update = button_update(n, CHAT, take if action == "take" else skip)
        elif action == "confirm":
            buttons = [p for p in rig.transport.named("sendMessage") if "reply_markup" in p]
            data = last_button(rig) if len(buttons) > 1 else "cnothing"
            update = button_update(n, CHAT, data)
        else:
            update = text_update(n, CHAT, action)
        handled = rig.service.handle(update)
        new = events_of(rig, mark)
        for event in new:
            if event.event_type in USER_EVENTS or event.actor == "user":
                assert event.actor == "user", event.event_type
                assert event.payload["chat_id"] == CHAT
        if handled.reply is not None and handled.reply.accepted and action in CHANGING:
            assert any(e.event_type in USER_EVENTS for e in new), action


# --- HAT-TG-014: no secret, token or broker text in output --------------------------------------


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self._format = logging.Formatter("%(name)s %(levelname)s %(message)s")

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self._format.format(record))


class CapturedLogs:
    """Every record of the `app` loggers, formatted with any traceback, while in the block."""

    def __enter__(self) -> list[str]:
        self._handler = Captured()
        self._logger = logging.getLogger("app")
        self._level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self._handler.lines

    def __exit__(self, *_exc: object) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)


SECRET_TEXT = st.text(
    alphabet="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789", min_size=16, max_size=32
)


@SLOW
@given(st.binary(min_size=32, max_size=48), SECRET_TEXT, SECRET_TEXT)
def test_no_secret_in_output(secret: bytes, bot_token: str, broker_text: str) -> None:
    """HAT-TG-014: no message sent and no log record written contains the bot token, the
    approval secret, an issued card token or button, or the text of a broker error, even when
    the broker rejects the entry with that text and the HTTP layer fails with the token in its
    URL and error body. Harm: a leaked token or secret lets an outsider control the bot or
    forge approvals."""
    fake = FakeBroker()
    fake.script("place_order", rejected(f"BRK {broker_text}"), rejected(f"BRK {broker_text}"))
    with CapturedLogs() as logs:
        rig = TgRig(secret=secret, fake=fake, telegram_alerts=True)
        relay = wall_relay(rig)
        signal_id, take, skip = card_of(rig, relay)
        rig.service.handle(button_update(1, CHAT, take))
        rig.service.handle(button_update(2, CHAT, skip))
        relay.run_once()
        failures = []
        for post in (
            lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError(f"https://api.telegram.org/bot{bot_token}/sendMessage {broker_text}")
            ),
            lambda *_a, **_k: type(
                "R", (), {"status_code": 401, "json": lambda _s: {"description": bot_token}}
            )(),
        ):
            client = BotClient(HttpsTransport(bot_token, post=post), CHAT)
            with pytest.raises(TelegramError) as raised:
                client.send("x")
            assert raised.value.__cause__ is None
            assert raised.value.__context__ is None or raised.value.__suppress_context__
            failures.append(f"{raised.value} {raised.value!r} {raised.value.code}")
        logging.getLogger("app.hat.telegram").error("transport %r", HttpsTransport(bot_token))
    with rig.factory() as session:
        approval = session.get(HatApproval, signal_id)
        assert approval is not None
        card_token = rig.signer.issue(signal_id, approval.expires_at)
    said = [
        *rig.transport.texts(),
        *(p["text"] for p in rig.transport.named("answerCallbackQuery")),
        *logs,
        *failures,
    ]
    forbidden = [bot_token, secret.hex(), card_token, take[1:], skip[1:], broker_text]
    forbidden += [secret.decode("latin-1")]
    for text in said:
        for value in forbidden:
            assert value not in text


# --- HAT-TG-020: only the allowlisted chat ------------------------------------------------------


@SLOW
@given(
    st.lists(
        st.sampled_from(
            (
                "route",
                "relay",
                "/status",
                "/flatten",
                "/kill",
                "stranger",
                "take",
                "alert",
                "report",
            )
        ),
        max_size=8,
    )
)
@example(["route", "relay", "take"])  # a Take on a live card clears its buttons
def test_send_only_to_allowlist(actions: list[str]) -> None:
    """HAT-TG-020: whatever the service, the relay, the notifier and the reports send, every
    `sendMessage` and `editMessageReplyMarkup` goes to the allowlisted chat, and an update from
    another chat is not answered at all. Harm: trade details and approval buttons reach
    someone else."""
    rig = TgRig(telegram_alerts=True)
    relay = wall_relay(rig)
    reports = Reports(rig.client, rig.factory, clock=lambda: ist(TRADE_DAY, 15, 40))
    take = None
    for n, action in enumerate(actions, start=1):
        if action == "route":
            rig.route(n % 2)
        elif action == "relay":
            relay.run_once()
            cards = [p for p in rig.transport.named("sendMessage") if "reply_markup" in p]
            if cards and cards[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Take":
                take = cards[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        elif action == "stranger":
            before = len(rig.transport.outbound())
            rig.service.handle(text_update(n, CHAT + 1, "/status"))
            assert len(rig.transport.outbound()) == before
        elif action == "take":
            rig.service.handle(button_update(n, CHAT, take or "tforged"))
        elif action == "alert":
            rig.notifier.alert("stop.not_placed", TRADE_DAY, {"position_id": "p"})
        elif action == "report":
            reports.daily_report()
        else:
            rig.service.handle(text_update(n, CHAT, action))
    for method in ("sendMessage", "editMessageReplyMarkup"):
        for payload in rig.transport.named(method):
            assert payload["chat_id"] == CHAT


# --- HAT-TG-021: login prompts ------------------------------------------------------------------


class Links:
    def link(self, account: HatBrokerAccount) -> str:
        return f"https://login.test/{account.account_ref}"


@SLOW
# the positions' account is logged in, another is not: the repeat is not URGENT
@example(offset=0, statuses=["live", "missing"], inactive=False, positions=["real"])
@given(
    st.integers(min_value=-7, max_value=7),
    st.lists(st.sampled_from(("live", "expired", "missing")), min_size=1, max_size=3),
    st.booleans(),
    st.lists(st.sampled_from(("real", "virtual", "closed")), max_size=3),
)
def test_login_prompt_links(
    offset: int, statuses: list[str], inactive: bool, positions: list[str]
) -> None:
    """HAT-TG-021: on a trading day the 08:30 job sends one prompt with one link per active
    account, once however often it is run; the 09:05 job sends one iff some active account has
    no live `hat_broker_session` that day, listing exactly those, and it is URGENT, naming
    their real open positions, iff those accounts hold any; on a non-trading day neither sends
    anything. Harm: a missed login blocks all entries for the day, positions sit with broker
    stops only while the app's exits are off and the owner is not told, or noise trains the
    owner to ignore prompts."""
    day = TRADE_DAY + timedelta(days=offset)
    rig = TgRig()
    with rig.factory() as session:
        accounts = [session.get(HatBrokerAccount, rig.account_id)]
        for n in range(1, len(statuses)):
            account = HatBrokerAccount(
                user_id=accounts[0].user_id,
                broker="fyers",
                account_ref=f"Y{n:04d}",
                capabilities={"gtt_delivery": True},
                status="active",
            )
            session.add(account)
            accounts.append(account)
        if inactive:
            session.add(
                HatBrokerAccount(
                    user_id=accounts[0].user_id,
                    broker="fyers",
                    account_ref="OFF0001",
                    capabilities={},
                    status="disconnected",
                )
            )
        session.flush()
        for kind in positions:  # on the rig's bot, so on the first account
            session.add(
                HatPosition(
                    bot_id=rig.world.bot_id,
                    signal_id=uuid.uuid4(),
                    instrument_id=rig.world.instruments[0],
                    is_virtual=kind == "virtual",
                    qty_open=10,
                    avg_entry=Decimal(100),
                    initial_stop=Decimal(95),
                    current_stop=Decimal(95),
                    currency="INR",
                    opened_at=ist(day, 9, 0) - timedelta(days=3),
                    closed_at=ist(day, 9, 0) - timedelta(days=1) if kind == "closed" else None,
                    state="Open",
                    rules_fired=[],
                )
            )
        session.execute(delete(HatBrokerSession))  # the rig's live sessions: this test sets its own
        for account, status in zip(accounts, statuses, strict=True):
            if status != "missing":
                session.add(
                    HatBrokerSession(broker_account_id=account.id, trade_date=day, status=status)
                )
        session.commit()
        refs = {a.account_ref: s for a, s in zip(accounts, statuses, strict=True)}
    clock = {"now": ist(day, 8, 30)}
    reports = Reports(rig.client, rig.factory, links=Links(), clock=lambda: clock["now"])
    jobs = {job.name: job for job in telegram_jobs(reports)}
    for _ in range(2):
        run_job(
            "telegram.login_prompt",
            day,
            lambda: jobs["telegram.login_prompt"].fn(day),
            session_factory=rig.factory,
        )
    first = list(rig.transport.texts())
    clock["now"] = ist(day, 9, 5)
    run_job(
        "telegram.login_reprompt",
        day,
        lambda: jobs["telegram.login_reprompt"].fn(day),
        session_factory=rig.factory,
    )
    second = rig.transport.texts()[len(first) :]
    trading = CALENDAR.days[day].is_trading
    if not trading:
        assert first == second == []
        return
    [prompt] = first
    assert {line.split("/")[-1] for line in prompt.splitlines()[1:]} == set(refs)
    out = {ref for ref, status in refs.items() if status != "live"}
    if not out:
        assert second == []
    else:
        [again] = second
        assert {line.split("/")[-1] for line in again.splitlines()[1:]} == out
        held = positions.count("real") if "X0001" in out else 0
        head = again.splitlines()[0]
        assert head.startswith("URGENT") == (held > 0)
        if held:
            assert f" {held} open position(s) " in head


# --- HAT-TG-022: the watchlist digest -----------------------------------------------------------

POOL = tuple(f"W{n:02d}" for n in range(14))
WATCHLIST = st.dictionaries(
    st.sampled_from(POOL), st.integers(min_value=0, max_value=10_000), max_size=14
)


@SLOW
@given(WATCHLIST, WATCHLIST)
def test_watchlist_digest_content(yesterday: dict[str, int], today: dict[str, int]) -> None:
    """HAT-TG-022: for two consecutive watchlists of a bot, the digest's new symbols are today
    minus yesterday, the dropped ones yesterday minus today, and the top list is the first
    min(10, n) of today by score descending; the message says exactly that. Harm: the owner acts
    on a wrong summary of what the bot will trade."""
    rig = TgRig()
    previous = TRADE_DAY - timedelta(days=1)
    with rig.factory() as session:
        market_id = session.scalar(select(HatMarket.id))
        ids = {}
        for n, symbol in enumerate(POOL):
            instrument = HatInstrument(
                market_id=market_id,
                isin=f"INW{n:09d}",
                series="EQ",
                symbol=symbol,
                name=symbol,
                face_value=D(1),
                active=True,
            )
            session.add(instrument)
            session.flush()
            ids[symbol] = instrument.id
        for day, entries in ((previous, yesterday), (TRADE_DAY, today)):
            for symbol, score in entries.items():
                session.add(
                    HatWatchlistEntry(
                        bot_id=rig.world.bot_id,
                        strategy_version_id=rig.world.version_id,
                        instrument_id=ids[symbol],
                        playbook="pb",
                        trade_date=day,
                        score=D(score) / 1000,
                        criteria_results={},
                        status="active",
                    )
                )
        session.commit()
    Reports(rig.client, rig.factory).send_watchlist_digest(rig.world.bot_id, TRADE_DAY)
    [text] = rig.transport.texts()
    lines = text.splitlines()
    assert lines[1] == "New: " + (", ".join(sorted(set(today) - set(yesterday))) or "-")
    assert lines[2] == "Dropped: " + (", ".join(sorted(set(yesterday) - set(today))) or "-")
    top = [line.split(" ", 2)[1:] for line in lines[4:]]
    assert len(top) == min(10, len(today))
    scores = [D(score) for _symbol, score in top]
    assert scores == sorted(scores, reverse=True)
    assert all(D(today[symbol]) / 1000 == D(score) for symbol, score in top)
    rest = [D(v) / 1000 for s, v in today.items() if s not in {sym for sym, _ in top}]
    assert all(r <= min(scores) for r in rest) if scores else not rest


@SLOW
@given(st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=3))
def test_watchlist_digest_once(sends: list[int]) -> None:
    """HAT-TG-022 (audit L7): each night's digest of a bot goes out once, however often the
    nightly job sends it (a retry after a later bot's send failed); the next night's watchlist
    gets its own. Harm: every retry repeats the digests already delivered."""
    rig = TgRig()
    reports = Reports(rig.client, rig.factory)
    bot = rig.world.bot_id
    for night, repeats in enumerate(sends):
        day = TRADE_DAY + timedelta(days=night)
        with rig.factory() as session:
            record(
                session,
                aggregate_type="bot",
                aggregate_id=bot,
                event_type="watchlist.published",
                actor="engine",
                payload={"trade_date": day.isoformat()},
                bot_id=bot,
                occurred_at=utc_now(),  # the night's scan, before its sends
            )
            session.commit()
        for _ in range(repeats):
            reports.send_watchlist_digest(bot, day)
        assert len(rig.transport.texts()) == night + 1


# --- HAT-TG-023: the card is the signal ---------------------------------------------------------


@SLOW
@given(
    st.sampled_from((modes.CONFIRM, modes.SHADOW)),
    st.integers(min_value=1000, max_value=10_000),
    st.integers(min_value=1, max_value=200),
)
def test_card_matches_signal(mode: str, entry_ticks: int, risk_ticks: int) -> None:
    """HAT-TG-023: the approval card (confirm) or the shadow message of a signal shows the
    `hat_signal` row's trigger price, stop, qty and risk exactly; a card shows the approval's
    expiry and offers exactly two controls, Take and Skip, signed for that signal; a shadow
    message offers none. Harm: the owner approves a trade other than the one the engine places,
    or changes its size or stop."""
    entry = D(entry_ticks) * D("0.05")
    stop = entry - D(risk_ticks) * D("0.05")
    rig = TgRig(mode=mode)
    relay = wall_relay(rig)
    outcome = rig.route(entry_price=entry, stop_price=stop, trigger_level=entry, ltp=entry)
    relay.run_once()
    with rig.factory() as session:
        signal = session.get(HatSignal, outcome.signal_id)
        approval = session.get(HatApproval, outcome.signal_id)
        assert signal is not None
    sent = rig.transport.named("sendMessage")
    if signal.qty <= 0 or signal.state == "rejected":
        assert sent == []
        return
    [message] = sent
    lines = message["text"].splitlines()
    money_line = next(line for line in lines if line.startswith("Entry "))
    assert money_line == (
        f"Entry {D(signal.trigger_price):.2f}  Stop {D(signal.stop_price):.2f}  Qty {signal.qty}"
    )
    assert any(line.startswith(f"Risk {D(signal.risk_amount):.2f} ") for line in lines)
    if mode == modes.SHADOW:
        assert lines[0] == "Shadow trade"
        assert "reply_markup" not in message
        return
    assert approval is not None
    expires = as_utc(approval.expires_at).astimezone(IST).strftime("%H:%M IST")
    assert lines[-1] == f"Expires {expires}"
    [row] = message["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in row] == ["Take", "Skip"]
    claims = [rig.signer.verify(b["callback_data"]) for b in row]
    assert [c.action for c in claims if c] == [TAKE, SKIP]
    assert {c.signal_id for c in claims if c} == {signal.id}


# --- HAT-TG-024, HAT-OPS-071: reports -----------------------------------------------------------

REFUSALS = st.lists(
    st.sampled_from(("market_closed", "stale_feed", "quality_failed", "regime_gate")), max_size=3
)
SNAPSHOTS = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=5),
        st.integers(min_value=0, max_value=10**7),
        st.integers(min_value=-500, max_value=500),
    ),
    max_size=3,
    unique_by=lambda s: s[0],
)


def _refused_check(reason: str) -> PlacementCheck:
    return {
        "market_closed": replace(LIVE, calendar=None),
        "stale_feed": replace(LIVE, feed_stale=True),
        "quality_failed": replace(LIVE, quality_failed=True),
        "regime_gate": replace(LIVE, gate_on=False),
    }[reason]


@SLOW
@given(REFUSALS, SNAPSHOTS, st.integers(min_value=1, max_value=3))
def test_report_content(
    refusals: list[str], snapshots: list[tuple[int, int, int]], fills: int
) -> None:
    """HAT-TG-024: the 15:35 digest lists exactly the day's `signal.rejected` signals with their
    reason codes; the 15:40 report shows each bot's open risk, realised R and equity against its
    peak from its latest `hat_daily_bot_snapshot`; every fill, partial, close and stop move of a
    real position is one message, and none of a virtual one. Harm: rejected signals, drawdowns
    and money moving go unseen."""
    # auto: the router places at once, so a refused check rejects the signal (a confirm bot
    # would post a card and check at the Take); one instrument per refusal (one signal a day)
    rig = TgRig(mode="auto", instruments=3)
    with rig.factory() as session:
        for n, reason in enumerate(refusals):
            request = request_for(rig.world, rig.world.instruments[n])
            rig.router.route(
                session,
                replace(request, triggered_at=AT),
                now=AT,
                check=_refused_check(reason),
                confidence="high",
            )
            session.commit()
        for back, equity, realised in snapshots:
            session.add(
                HatDailyBotSnapshot(
                    bot_id=rig.world.bot_id,
                    trade_date=TRADE_DAY - timedelta(days=back),
                    equity=D(equity),
                    equity_peak=D(equity) + D(back),
                    open_risk=D(back * 100),
                    realized_r=D(realised) / 100,
                    exposure_level="L2",
                    currency="INR",
                )
            )
        session.commit()
        rejected_rows = [
            (e.payload["reason"])
            for e in session.scalars(
                select(HatEvent)
                .where(HatEvent.event_type == "signal.rejected")
                .order_by(HatEvent.id)
            )
        ]
        label = reads.bot_label(session, session.get(HatBot, rig.world.bot_id))
        symbols = [reads.symbol_of(session, i) for i in rig.world.instruments[: len(refusals)]]
    clock = {"now": ist(TRADE_DAY, 15, 35)}
    reports = Reports(rig.client, rig.factory, clock=lambda: clock["now"])
    reports.rejection_digest()
    clock["now"] = ist(TRADE_DAY, 15, 40)
    reports.daily_report()
    digest, daily = rig.transport.texts()
    assert digest.splitlines()[0].endswith(f": {len(rejected_rows)}")
    assert [line.rsplit(": ", 1)[1] for line in digest.splitlines()[1:]] == rejected_rows
    assert len(rejected_rows) == len(refusals)
    assert [line.rsplit(": ", 1)[0] for line in digest.splitlines()[1:]] == [
        f"{label} {symbol}" for symbol in symbols
    ]
    [bot_line] = [line for line in daily.splitlines() if line.startswith(label)]
    if snapshots:
        back, equity, realised = min(snapshots)
        assert f"({(TRADE_DAY - timedelta(days=back)).isoformat()})" in bot_line
        assert f"open risk {D(back * 100):.2f}" in bot_line
        assert f"realised {D(realised) / 100:+.2f}R" in bot_line
        assert f"equity {D(equity):.2f} vs peak {D(equity) + D(back):.2f}" in bot_line
    else:
        assert "(no snapshot)" in bot_line

    # position messages of a real trade (the manage rig: real fills over FakeBroker)
    engine = memory_engine()
    with sessionmaker(engine, expire_on_commit=False)() as session:
        manage = ManageRig(session)
        transport = FakeTransport()
        relay = EventRelay(
            BotClient(transport, CHAT),
            sessionmaker(engine, expire_on_commit=False),
            ApprovalSigner(bytes(32)),
            clock=lambda: utc_now() + RELAY_LAG * 2,
        )
        relay.run_once()
        outcome = manage.enter(manage_at(10, 30))
        row = manage.entry_row(outcome.signal_id)
        assert row is not None
        part = max(1, row.qty // fills)
        done = 0
        for n in range(fills):
            qty = row.qty - done if n == fills - 1 else part
            manage.fill(row, qty, D(row.price))
            done += qty
            manage.sync(manage_at(10, 40 + n))
        session.commit()
        relay.run_once()
        expected = 0
        for event in session.scalars(
            select(HatEvent).where(HatEvent.event_type.in_(tuple(POSITION_EVENTS)))
        ):
            if event.aggregate_type == "order":
                order = session.get(HatOrder, event.aggregate_id)
                expected += order is not None and not order.is_virtual
            else:
                position = session.get(HatPosition, event.aggregate_id)
                expected += position is not None and not position.is_virtual
    titles = ("Filled: ", "Partial exit: ", "Position closed: ", "Stop moved: ")
    messages = [t for t in transport.texts() if t.startswith(titles)]
    assert expected >= 1
    assert len(messages) == expected
    assert all(t.split("\n", 1)[0].endswith("SBIN") for t in messages)


DAY_KIND = st.sampled_from(("trading", "holiday", "unknown"))


@given(st.lists(DAY_KIND, min_size=14, max_size=14), st.integers(min_value=0, max_value=6))
@example(kinds=["trading"] * 5 + ["unknown"] * 9, day=4)  # a calendar that runs out on a Friday
@example(kinds=["trading"] * 3 + ["unknown"] * 11, day=2)  # ... and on a Wednesday
def test_veto_digest_on_week_end(kinds: list[str], day: int) -> None:
    """HAT-TG-024, HAT-EXEC-130: the weekly veto digest goes out on a trading day exactly when
    no later weekday of its week may still trade (a holiday in the calendar does not; a day
    the calendar does not have yet may). Saturdays and Sundays never trade. Harm: the "weekly"
    digest comes every day once the calendar runs out, or never in a week with a holiday."""
    monday = date(2026, 10, 19)
    days = [monday + timedelta(days=n) for n in range(14)]
    rows = []
    for d, kind in zip(days, kinds, strict=True):
        if kind == "unknown":
            continue
        trading = kind == "trading" and d.weekday() < 5
        opens = ist(d, 9, 15) if trading else None
        closes = ist(d, 15, 30) if trading else None
        rows.append((d, trading, opens, closes))
    calendar = SessionCalendar.from_rows(rows)
    target = days[day]

    def may_trade(d: date) -> bool:
        if d.weekday() >= 5:
            return False
        kind = kinds[days.index(d)]
        return kind != "holiday"

    is_trading = kinds[day] == "trading" and target.weekday() < 5
    rest_of_week = [d for d in days if d > target and d.isocalendar()[1] == target.isocalendar()[1]]
    expected = is_trading and not any(may_trade(d) for d in rest_of_week)
    assert is_week_end(calendar, target) is expected


# --- HAT-TG-032: /mode only outside market hours ------------------------------------------------


@SLOW
@given(
    st.integers(min_value=-2 * 96, max_value=4 * 96),
    st.sampled_from(("shadow", "confirm", "auto", "AUTO", "signal", "live", "x")),
)
@example(quarter=40, word="auto")  # 10:00 IST on the trade day
@example(quarter=80, word="confirm")  # 20:00 IST
def test_mode_outside_hours(quarter: int, word: str) -> None:
    """HAT-TG-032: `/mode` is refused inside market hours and for any value but shadow,
    confirm and auto, writing no `hat_mode_change`; an accepted one (auto only after its
    confirmation) queues the change for the next session open of `hat_trading_calendar`.
    Harm: switching to auto mid-session, or a mode that trades around the risk policy."""
    now = ist(TRADE_DAY, 0) + timedelta(minutes=15 * quarter)
    rig = TgRig().at(now)
    handled = rig.service.handle(text_update(1, CHAT, f"/mode {BOT} {word}"))
    assert handled.reply is not None
    if handled.reply.accepted and handled.reply.buttons:
        handled = rig.service.handle(button_update(2, CHAT, last_button(rig)))
        assert handled.reply is not None
    with rig.factory() as session:
        rows = list(session.scalars(select(HatModeChange)))
    in_hours = CALENDAR.state(now) is not SessionState.CLOSED
    if in_hours or word.lower() not in modes.MODES:
        assert not handled.reply.accepted
        assert rows == []
        return
    assert handled.reply.accepted
    [row] = rows
    opens = sorted(d.open_at for d in CALENDAR.days.values() if d.is_trading and d.open_at > now)
    assert as_utc(row.effective_from) == as_utc(opens[0])
    assert row.to_mode == word.lower()


# --- HAT-TG-038: /mode names one bot and says which gate refuses it ----------------------------


@SLOW
@given(
    word=st.sampled_from(modes.MODES),
    ddpi=st.booleans(),
    weights=st.booleans(),
)
@example(word="confirm", ddpi=False, weights=True)
@example(word="auto", ddpi=True, weights=False)
def test_mode_names_a_bot_and_its_gate(word: str, ddpi: bool, weights: bool) -> None:
    """HAT-TG-038, HAT-GOV-085: `/mode <mode>` without a bot is refused and writes nothing;
    `/mode <bot> confirm|auto` is refused with the first gate that would hold the bot back at
    the next open (`ddpi_not_confirmed`, then `weights_not_confirmed`) and writes nothing, or is
    queued (auto after its confirmation); `/status` then shows the queued change beside the mode
    the bot runs. Harm: every bot switched at once by mistake, or a bot left in shadow with no
    way to tell why."""
    rig = TgRig().at(EVENING)
    with rig.factory() as session:
        confirm_gates(session, rig.account_id, OWNER, ddpi=ddpi, weights=weights)
        session.commit()
    bare = rig.service.handle(text_update(1, CHAT, f"/mode {word}"))
    assert bare.reply is not None
    assert (bare.reply.accepted, bare.reply.reason) == (False, "bad_arguments")
    handled = rig.service.handle(text_update(2, CHAT, f"/mode {BOT} {word}"))
    assert handled.reply is not None
    if handled.reply.accepted and handled.reply.buttons:
        handled = rig.service.handle(button_update(3, CHAT, last_button(rig)))
        assert handled.reply is not None
    with rig.factory() as session:
        rows = list(session.scalars(select(HatModeChange)))
    gate = (
        None
        if word == "shadow"
        else (
            "ddpi_not_confirmed" if not ddpi else "weights_not_confirmed" if not weights else None
        )
    )
    if gate is not None:
        assert (handled.reply.accepted, handled.reply.reason) == (False, gate)
        assert f"Refused: {gate} (" in (handled.reply.text or "")
        assert rows == []
        return
    assert handled.reply.accepted
    [row] = rows
    assert row.to_mode == word
    status = rig.service.handle(text_update(4, CHAT, "/status"))
    assert status.reply is not None
    held = (
        "DDPI not confirmed" if not ddpi else "base weights not confirmed" if not weights else None
    )
    now = "confirm" if held is None else f"shadow (asked confirm: {held})"
    assert f"{now}; {word} from " in (status.reply.text or "")


# --- HAT-TG-033: second confirmation ------------------------------------------------------------


def _count(session: Session, where: ColumnElement[bool]) -> int:
    table = where.left.table  # type: ignore[attr-defined]
    return len(session.execute(select(table).where(where)).all())


CONFIRM_ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(
            ("/flatten", "/arm", f"/mode {BOT} auto", "/kill", "press", "replay", "stranger")
        ),
        st.integers(min_value=0, max_value=400),
    ),
    max_size=10,
)


@SLOW
@given(CONFIRM_ACTIONS)
@example([("/flatten", 0), ("press", 10), ("replay", 0)])
@example([("/kill", 0), ("/arm", 0), ("press", 301)])
def test_second_confirm(actions: list[tuple[str, int]]) -> None:
    """HAT-TG-033: auto, a re-arm and a flatten happen only when the Confirm button of their
    command is pressed in the allowlisted chat before the confirmation's 5 minutes are up, and
    each confirmation acts at most once: a replayed press, a late press or a press from another
    chat does nothing. Harm: one mistyped message switches to real-money auto, re-arms after a
    kill, or sells everything."""
    rig = TgRig().at(EVENING)
    pending: dict[str, tuple[str, datetime]] = {}
    pressed: list[str] = []
    tripped = False
    want = Counter()
    for n, (action, seconds) in enumerate(actions, start=1):
        rig.at(rig.later(seconds))
        if action in ("press", "replay", "stranger"):
            data = (pressed[0] if action == "replay" and pressed else None) or (
                list(pending)[-1] if pending else "cnone"
            )
            chat = CHAT + 1 if action == "stranger" else CHAT
            rig.service.handle(button_update(n, chat, data))
            if chat != CHAT or data not in pending:
                continue
            kind, asked = pending.pop(data)
            pressed.append(data)
            if rig.now >= asked + CONFIRM_WINDOW:
                continue
            if kind == "arm":
                want["arm"] += tripped
                tripped = False
            else:
                want[kind] += 1
            continue
        handled = rig.service.handle(text_update(n, CHAT, action))
        assert handled.reply is not None
        if action == "/kill":
            tripped = True
        elif handled.reply.buttons:
            kind = {"/flatten": "flatten", "/arm": "arm", f"/mode {BOT} auto": "mode_auto"}[action]
            pending[last_button(rig)] = (kind, rig.now)
    with rig.factory() as session:
        got = Counter(
            flatten=_count(session, HatEvent.event_type == "flatten.executed"),
            arm=_count(session, HatEvent.event_type == "kill.rearmed"),
            mode_auto=_count(session, HatModeChange.to_mode == "auto"),
        )
    assert +got == +want


# --- HAT-TG-035, HAT-GOV-082: flatten -----------------------------------------------------------


@SLOW
@given(
    st.sampled_from(((11, 0), (14, 0), (15, 26), (16, 0))),
    st.integers(min_value=1, max_value=3),
    st.booleans(),
)
def test_flatten_logged(when: tuple[int, int], parts: int, killed: bool) -> None:
    """HAT-TG-035, HAT-GOV-082: a confirmed `/flatten` sells only: every order it sends has
    purpose flatten, side sell and qty at most the position's open qty, even under a tripped
    kill switch; it writes exactly one `flatten.executed` with `actor = 'user'` and the chat id.
    After 15:30 the confirmation is refused (`market_closed`) and nothing is sold or recorded.
    Harm: flatten opens or grows a position, or the breach is invisible in rule adherence."""
    engine = memory_engine()
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        rig = ManageRig(session)
        outcome = rig.enter(manage_at(10, 30))
        row = rig.entry_row(outcome.signal_id)
        assert row is not None
        rig.fill(row, max(1, row.qty // parts), D(row.price))
        rig.sync(manage_at(10, 40))
        position = rig.position(outcome.signal_id)
        assert position is not None
        open_qty = position.qty_open
        session.commit()
        transport = FakeTransport()
        now = manage_at(*when)
        service = TelegramService(
            BotClient(transport, CHAT),
            factory,
            signer=ApprovalSigner(bytes(32)),
            flattener=ManagerFlattener(lambda: [(rig.account_id, rig.manager)]),
            clock=lambda: now,
        )
        if killed:
            service.handle(text_update(1, CHAT, "/kill"))
        before = {o.id for o in session.scalars(select(HatOrder))}
        places = len(rig.fake.calls_named("place_order"))
        service.handle(text_update(2, CHAT, "/flatten"))
        confirm = [p for p in transport.named("sendMessage") if "reply_markup" in p][-1]
        data = confirm["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        confirmed = service.handle(button_update(3, CHAT, data))
        session.expire_all()
        new = [o for o in session.scalars(select(HatOrder)) if o.id not in before]
        for order in new:
            assert (order.purpose, order.side) == ("flatten", Side.SELL.value)
            assert order.qty <= open_qty
            assert not order.is_virtual
        assert len(new) <= 1
        for call in rig.fake.calls_named("place_order")[places:]:
            assert str(call["side"]).upper() == "SELL"
        events = session.scalars(
            select(HatEvent).where(HatEvent.event_type == "flatten.executed")
        ).all()
        if when >= (15, 30):  # nothing can be sold: refused, no sale and no breach recorded
            assert confirmed.reply is not None
            assert confirmed.reply.reason == "market_closed"
            assert new == []
            assert events == []
            return
        [event] = events
        assert event.actor == "user"
        assert event.payload["chat_id"] == CHAT
        if not new:
            pytest.fail("a flatten in market hours sent no sale")


# --- HAT-TG-037: read commands ------------------------------------------------------------------


@SLOW
@given(
    st.sampled_from(
        (
            "/status",
            "/positions",
            "/watchlist",
            "/watchlist nobody",
            "/login",
            "/login fyers",
            "/login zerodha",
        )
    ),
    st.sampled_from((AT, EVENING)),
    st.booleans(),
)
def test_read_commands_side_effect_free(command: str, now: datetime, killed: bool) -> None:
    """HAT-TG-037: `/status`, `/positions`, `/watchlist` and `/login` change no table but the
    chat's own log and make no broker call. Harm: a status check changes trading state."""
    rig = TgRig(mode=modes.SHADOW)
    rig.route(0)  # a virtual position to list
    rig.at(now)
    if killed:
        rig.service.handle(text_update(1, CHAT, "/kill"))
    before, broker = state(rig), len(rig.fake.calls)
    handled = rig.service.handle(text_update(2, CHAT, command))
    assert handled.reply is not None
    assert state(rig) == before
    assert rig.fake.calls[broker:] == []


# --- HAT-TG-040: forged tokens ------------------------------------------------------------------

FORGERIES = st.one_of(
    st.tuples(st.just("flip"), st.integers(min_value=0, max_value=54), st.integers(1, 63)),
    st.tuples(st.just("expiry"), st.integers(-600, 600).filter(bool), st.just(0)),
    st.tuples(st.just("other_secret"), st.just(0), st.just(0)),
    st.tuples(st.just("other_signal"), st.just(0), st.just(0)),
    st.tuples(st.just("swap"), st.just(0), st.just(0)),
    st.tuples(st.just("text"), st.just(0), st.just(0)),
)
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


@SLOW
@given(FORGERIES, st.text(max_size=60))
@example(forgery=("flip", 0, 1), noise="")  # Take turned into Skip
def test_token_forgery_rejected(forgery: tuple[str, int, int], noise: str) -> None:
    """HAT-TG-040: a callback that is not a button the service issued for the card (any
    character changed, the expiry moved, signed under another secret, another signal's id,
    the two cards' buttons swapped, or arbitrary text) is refused with no state change and no
    broker call. Harm: a forged callback approves a real-money entry."""
    rig = TgRig()
    relay = wall_relay(rig)
    signal_id, take, _skip = card_of(rig, relay)
    other_id, other_take, _ = card_of(rig, relay, instrument=1)
    with rig.factory() as session:
        approval = session.get(HatApproval, signal_id)
        assert approval is not None
        expires_at = as_utc(approval.expires_at)
    kind, a, b = forgery
    if kind == "flip":
        alphabet = "ts" + _B64
        current = alphabet.index(take[a]) if take[a] in alphabet else 0
        data = take[:a] + alphabet[(current + b) % len(alphabet)] + take[a + 1 :]
    elif kind == "expiry":
        data = rig.signer.button(TAKE, signal_id, expires_at + timedelta(seconds=a))
    elif kind == "other_secret":
        data = ApprovalSigner(bytes(range(100, 132))).button(TAKE, signal_id, expires_at)
    elif kind == "other_signal":
        data = rig.signer.button(TAKE, uuid.uuid4(), expires_at)
    elif kind == "swap":  # the other card's Take, its id swapped in under its own MAC
        data = other_take[0] + take[1:23] + other_take[23:]
    else:
        data = noise
    if data == take:
        return
    before, broker = state(rig), len(rig.fake.calls)
    handled = rig.service.handle(button_update(1, CHAT, data))
    assert handled.reply is not None
    assert not handled.reply.accepted
    assert state(rig) == before
    assert rig.fake.calls[broker:] == []
    with rig.factory() as session:
        assert session.get(HatApproval, signal_id).decision is None
        assert session.get(HatApproval, other_id).decision is None


# --- HAT-TG-042: the 5-minute window ------------------------------------------------------------


@SLOW
@given(st.integers(min_value=0, max_value=900))
@example(299)
@example(300)
def test_expiry_window(after: int) -> None:
    """HAT-TG-042: the card expires 5 minutes after it is sent; a Take from then on is refused
    and places nothing, the approval ends expired, and a Take before then places one order
    (the price is at the trigger). Harm: the owner approves a setup whose price and context
    are stale."""
    rig = TgRig()
    signal_id, take, _skip = card_of(rig, wall_relay(rig))
    with rig.factory() as session:
        approval = session.get(HatApproval, signal_id)
        assert approval is not None
        assert as_utc(approval.expires_at) - as_utc(approval.sent_at) == timedelta(minutes=5)
    rig.at(AT + timedelta(seconds=after))
    handled = rig.service.handle(button_update(1, CHAT, take))
    assert handled.reply is not None
    with rig.factory() as session:
        approval = session.get(HatApproval, signal_id)
    if after >= 300:
        assert not handled.reply.accepted
        assert rig.places() == []
        assert approval.decision == approvals.EXPIRED
    else:
        assert handled.reply.accepted
        assert len(rig.places()) == 1


# --- HAT-TG-043: Take re-checks max-chase -------------------------------------------------------


@SLOW
@given(st.integers(min_value=-100, max_value=1500), st.sampled_from((False, True, None)))
@example(chase_bp=0, locked=False)
@example(chase_bp=1000, locked=False)
def test_take_chase_recheck(chase_bp: int, locked: bool | None) -> None:
    """HAT-TG-043: a Take from the chat places the entry iff the live price is within the
    trigger's max-chase and the stock is known not to be locked at its upper circuit (unknown
    fails closed); otherwise the Take is recorded with the price at the tap and refused. Harm:
    the owner's tap buys far above the planned entry, at a risk larger than sized."""
    rig = TgRig()
    signal_id, take, _skip = card_of(rig, wall_relay(rig))
    with rig.factory() as session:
        signal = session.get(HatSignal, signal_id)
        level = D(signal.trigger_price)
        pct = D(
            session.scalar(
                select(HatEvent.payload).where(
                    HatEvent.correlation_id == signal_id,
                    HatEvent.event_type == "signal.triggered",
                )
            )["max_chase_pct"]
        )
    rig.price = level * (10_000 + chase_bp) / 10_000
    rig.locked = locked
    handled = rig.service.handle(button_update(1, CHAT, take))
    with rig.factory() as session:
        approval = session.get(HatApproval, signal_id)
    assert approval.decision == "take"
    assert D(approval.price_at_decision) == rig.price
    if rig.price * 100 <= level * (100 + pct) and locked is False:
        assert handled.reply.accepted
        assert len(rig.places()) == 1
    else:
        assert not handled.reply.accepted
        assert rig.places() == []


# --- HAT-SCH-056, HAT-TG-044: the raw token is never kept ---------------------------------------

PRESSES = st.lists(st.tuples(st.integers(0, 1), st.sampled_from(("take", "skip"))), max_size=3)


def _tokens_after(presses: list[tuple[int, str]]) -> tuple[TgRig, list[str], list[str]]:
    """Two cards, the presses on them; (rig, raw card tokens and buttons, log lines)."""
    with CapturedLogs() as logs:
        rig = TgRig()
        relay = wall_relay(rig)
        cards = [card_of(rig, relay, 0), card_of(rig, relay, 1)]
        for n, (card, action) in enumerate(presses, start=1):
            _id, take, skip = cards[card]
            rig.service.handle(button_update(n, CHAT, take if action == "take" else skip))
        relay.run_once()
    raw = []
    with rig.factory() as session:
        for signal_id, take, skip in cards:
            approval = session.get(HatApproval, signal_id)
            token = rig.signer.issue(signal_id, approval.expires_at)
            assert approval.token_hash == token_hash(token)
            assert approval.token_hash == hashlib.sha256(token.encode()).hexdigest()
            raw += [token, take[1:], skip[1:]]
    return rig, raw, list(logs)


@SLOW
@given(PRESSES)
def test_token_never_stored(presses: list[tuple[int, str]]) -> None:
    """HAT-SCH-056: whatever is pressed, no column of any HAT table holds an issued card token
    or button token; `hat_approval.token_hash` is its sha256. Harm: anyone with database
    access can approve trades."""
    rig, raw, _logs = _tokens_after(presses)
    stored = all_text(rig)
    for token in raw:
        assert token not in stored


@SLOW
@given(PRESSES)
def test_token_never_persisted(presses: list[tuple[int, str]]) -> None:
    """HAT-TG-044: the raw tokens exist only in the buttons: no table and no log record written
    by HAT holds one, and the stored hash is the hash of the card's token. Harm: anyone with
    database or log access can approve trades."""
    rig, raw, logs = _tokens_after(presses)
    stored = all_text(rig)
    for token in raw:
        assert token not in stored
        assert all(token not in line for line in logs)

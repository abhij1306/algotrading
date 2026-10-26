"""The text commands (HAT-TG-030..037, HAT-DEC-I11, HAT-GOV-080).

The command set is closed: exactly the ten in `COMMANDS`, each with a fixed argument shape;
anything else is refused with no state change (HAT-TG-030). No command takes a price, a size or
a stop, none acts on a single position, and none touches the regime gate (HAT-GOV-080,
HAT-DEC-B12).

This is the text path, and it holds no route to an order (HAT-TG-031, CONF
`telegram.no_order_import_in_text_path`): it imports nothing that places, sends or approves one.
`/flatten`, `/arm` and `/mode … auto` only ask for a second confirmation here; the confirmed
action runs from its button (`callbacks.py`). Every handler works in the caller's transaction
and does not commit; the service logs the command in the same transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..configs.records import Bot, BrokerAccount
from ..configs.unit import configs_of
from ..data.calendar import session_calendar
from ..errors import HatError
from ..execution.mode_changes import request_mode_change
from ..execution.modes import NO_GTT_DELIVERY, account_gates, gate_refusal, sleeve_refusal
from ..governance import MODE_VALUES, Mode
from ..read import bots as read_bots
from ..read import kill as read_kill
from ..read.sessions import account_label
from ..risk import kill, kill_ops
from ..risk.bots import pause_bot, resume_bot
from ..risk.modes import ModeChangeError, plan_mode_change
from ..risk.policy import effective_version
from ..risk.policy_service import load_versions
from ..risk.sessions import SessionCalendar, SessionState
from ..settings import load_settings, process_env
from ..time import as_utc, trade_date_of
from . import reads, render
from .client import MAX_TEXT, Button
from .confirm import CONFIRM_WINDOW, Confirmation, PendingConfirmations

__all__ = [
    "COMMANDS",
    "CommandContext",
    "Reply",
    "KILL_CHANGED",
    "addressee",
    "arm_fits",
    "arm_refusal",
    "for_another_bot",
    "parse",
    "mode_refused",
    "queue_modes",
    "rearm",
    "run_command",
    "tripped_kills",
]

# (fewest, most) arguments of each command; its keys are the command set, in spec order.
_ARITY = {
    "/status": (0, 0),
    "/positions": (0, 0),
    "/watchlist": (0, 1),
    "/pause": (0, 1),
    "/resume": (0, 1),
    "/mode": (2, 2),
    "/kill": (0, 0),
    "/flatten": (0, 0),
    "/arm": (0, 1),
    "/login": (0, 1),
}
COMMANDS = tuple(_ARITY)
_MAX_ARG = 64
ACCOUNT = "account"
NOTHING_TRIPPED = "nothing_tripped"
KILL_CHANGED = "kill_changed"
TOO_MANY_KILLS = "too_many_kills"


@dataclass(frozen=True)
class Reply:
    """A handler's answer: whether the command was accepted, the reason when it was not, and
    the message (and buttons) to send back."""

    accepted: bool
    reason: str | None = None
    text: str | None = None
    buttons: tuple[tuple[Button, ...], ...] = field(default=())


def refused(reason: str, text: str | None = None) -> Reply:
    return Reply(False, reason, text or f"Refused: {reason}")


@dataclass(frozen=True)
class CommandContext:
    chat_id: int
    now: datetime
    confirmations: PendingConfirmations
    links: reads.LoginLinks | None = None
    price: reads.PriceSource = field(default=lambda _instrument: None)
    feed: Callable[[], str] = field(default=lambda: "unknown")
    bot_username: str | None = None  # the bot's own `getMe` username; None when unknown


def addressee(text: str) -> str | None:
    """The `@bot` suffix of a command, the message's first word (`/kill@hat_bot` -> `hat_bot`),
    else None: a text that is not a command (`@`, `hi@there`) names no bot."""
    parts = text.strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/") or "@" not in parts[0]:
        return None
    return parts[0].split("@", 1)[1]


def for_another_bot(text: str, bot_username: str | None) -> bool:
    """A command whose `@bot` suffix names another bot, compared without case. While this bot's
    own username is unknown every suffixed command counts as another bot's (fail closed)."""
    suffix = addressee(text)
    if suffix is None:
        return False
    return bot_username is None or suffix.lower() != bot_username.lower()


def parse(text: str) -> tuple[str, list[str]] | None:
    """(command, args) of a message that starts with one of the ten commands (an `@bot` suffix
    is allowed; `run_command` checks that it names this bot) and has the command's number of
    arguments; else None."""
    parts = text.strip().split()
    if not parts:
        return None
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]
    if command not in _ARITY:
        return None
    fewest, most = _ARITY[command]
    if not fewest <= len(args) <= most or any(len(a) > _MAX_ARG for a in args):
        return None
    return command, args


# --- read-only commands (HAT-TG-037) -----------------------------------------------------------


def _status(ctx: CommandContext, session: Session, _args: Sequence[str]) -> Reply:
    return Reply(True, text=render.status_text(reads.status_view(session, ctx.now, ctx.feed())))


def _positions(ctx: CommandContext, session: Session, _args: Sequence[str]) -> Reply:
    return Reply(True, text=render.positions_text(reads.position_rows(session, ctx.now, ctx.price)))


def _watchlist(ctx: CommandContext, session: Session, args: Sequence[str]) -> Reply:
    bots = reads.bots_named(session, args[0] if args else None)
    if not bots:
        return refused("unknown_bot")
    texts = reads.watchlist_texts(session, bots, trade_date_of(ctx.now))
    return Reply(True, text="\n\n".join(texts))


def _login(ctx: CommandContext, session: Session, args: Sequence[str]) -> Reply:
    accounts = reads.accounts(session, args[0] if args else None)
    if not accounts:
        return refused("unknown_broker")
    links = [
        (account_label(a), ctx.links.link(a) if ctx.links is not None else None) for a in accounts
    ]
    return Reply(True, text=render.login_text(links, repeat=False))


# --- controls ----------------------------------------------------------------------------------


def _pause(ctx: CommandContext, session: Session, args: Sequence[str]) -> Reply:
    bots = reads.bots_named(session, args[0] if args else None)
    if not bots:
        return refused("unknown_bot")
    done = [
        read_bots.label_of(session, b)
        for b in bots
        if pause_bot(session, b.id, ctx.now, actor="user", chat_id=ctx.chat_id)
    ]
    if not done:
        return refused("not_active", "Nothing to pause")
    return Reply(True, text=f"Paused (manage-only): {', '.join(done)}")


def _resume(ctx: CommandContext, session: Session, args: Sequence[str]) -> Reply:
    bots = reads.bots_named(session, args[0] if args else None)
    if not bots:
        return refused("unknown_bot")
    done = [
        read_bots.label_of(session, b)
        for b in bots
        if resume_bot(session, b.id, ctx.now, actor="user", chat_id=ctx.chat_id)
    ]
    if not done:
        return refused("not_paused", "Nothing to resume")
    return Reply(True, text=f"Resumed: {', '.join(done)} (a tripped kill switch still holds)")


def _kill(ctx: CommandContext, session: Session, _args: Sequence[str]) -> Reply:
    """HAT-TG-034: trip the account kill switch; `trip_kill` ends every pending approval in the
    same transaction. Exits and stops keep running."""
    row = kill_ops.trip_kill(
        session,
        scope="account",
        bot_id=None,
        reason="manual",
        now=ctx.now,
        actor="user",
        chat_id=ctx.chat_id,
    )
    if row is None:
        return refused("already_tripped", "The account kill switch is already tripped")
    return Reply(True, text="Kill switch tripped: every bot is manage-only; approvals cancelled")


def _ask(
    ctx: CommandContext,
    action: str,
    bots: tuple[str, ...] = (),
    kills: tuple[kill.KillRow, ...] = (),
    lines: Sequence[str] = (),
) -> Reply:
    data, pending = ctx.confirmations.ask(action, ctx.chat_id, ctx.now, bots, kills)
    return Reply(
        True,
        text=render.confirm_text(action, pending.expires_at, lines),
        buttons=((("Confirm", data),),),
    )


def _flatten(ctx: CommandContext, _session: Session, _args: Sequence[str]) -> Reply:
    return _ask(ctx, "flatten")


def tripped_kills(rows: Iterable[kill.KillRow]) -> list[kill.KillRow]:
    """The trip row of each key whose drawdown or manual kill is tripped in `rows` (the latest
    trip, the one a re-arm ends), the account's first, then by bot id."""
    risk = [r for r in rows if kill.channel_of(r.reason) == "risk"]
    keys = sorted({(r.scope, r.bot_id) for r in risk}, key=lambda k: (k[1] is not None, str(k[1])))
    out: list[kill.KillRow] = []
    for scope, bot_id in keys:
        if kill.current_state(risk, scope, bot_id, "risk") != kill.TRIPPED:
            continue
        trips = [
            r for r in risk if (r.scope, r.bot_id) == (scope, bot_id) and r.state == kill.TRIPPED
        ]
        out.append(max(trips, key=lambda r: as_utc(r.tripped_at)))
    return out


def arm_refusal(asked: Sequence[kill.KillRow], tripped: Sequence[kill.KillRow]) -> str | None:
    """Whether the confirmed `/arm` may re-arm the kills its question listed (`asked`), given
    those tripped now among the keys it named (`tripped`): None when they are the same set (same
    key, reason and trip time); `nothing_tripped` when none is tripped now; `kill_changed` when
    one was re-armed, tripped again or newly tripped since the question. Either refusal re-arms
    nothing."""
    if not tripped:
        return NOTHING_TRIPPED
    return None if set(tripped) == set(asked) else KILL_CHANGED


def arm_fits(lines: Sequence[str], expires_at: datetime) -> bool:
    """Whether the `/arm` question listing every kill in `lines` fits one Telegram message. A
    longer one is cut on sending while its Confirm stays, which would re-arm kills the owner
    never saw, so it is refused instead (`too_many_kills`)."""
    return len(render.confirm_text("arm", expires_at, lines)) <= MAX_TEXT


def _key_of(row: kill.KillRow) -> str:
    return ACCOUNT if row.bot_id is None else str(row.bot_id)


def _tripped(session: Session, keys: Sequence[str]) -> list[kill.KillRow]:
    """The tripped drawdown or manual kills of `keys` (`account` or bot ids; none: every key)."""
    rows = tripped_kills(read_kill.kill_rows(session))
    return [r for r in rows if not keys or _key_of(r) in keys]


def _arm_keys(session: Session, name: str | None) -> tuple[str, ...] | None:
    """The keys `/arm` names: none for every key, `account`, or the ids of the bots `name`
    names; None when it names no bot."""
    if name is None:
        return ()
    if name.strip().lower() == ACCOUNT:
        return (ACCOUNT,)
    return tuple(str(b.id) for b in reads.bots_named(session, name)) or None


def _arm(ctx: CommandContext, session: Session, args: Sequence[str]) -> Reply:
    """HAT-RISK-064: `/arm` asks to re-arm the tripped drawdown or manual kills it names (every
    one, `account`, or a bot), listing each; the Confirm re-arms exactly those (`rearm`)."""
    keys = _arm_keys(session, args[0] if args else None)
    if keys is None:
        return refused("unknown_bot")
    trips = _tripped(session, keys)
    if not trips:
        return refused(NOTHING_TRIPPED, "No kill switch to re-arm")
    names = read_bots.labels(session, (r.bot_id for r in trips if r.bot_id is not None))
    policy = effective_version(load_versions(session, load_settings().user_id), ctx.now)
    lines = [
        render.drawdown_line(
            None if policy is None else policy.policy.account_drawdown_pct,
            None if policy is None else policy.policy.bot_drawdown_pct,
        ),
        *(
            render.kill_line(
                ACCOUNT if r.bot_id is None else names.get(r.bot_id, str(r.bot_id)[:8]),
                r.reason,
                r.tripped_at,
            )
            for r in trips
        ),
    ]
    if not arm_fits(lines, as_utc(ctx.now) + CONFIRM_WINDOW):
        return refused(
            TOO_MANY_KILLS, "Too many kill switches to list: name one, /arm account or /arm <bot>"
        )
    return _ask(ctx, "arm", keys, tuple(trips), lines)


def rearm(
    session: Session, pending: Confirmation, now: datetime, chat_id: int
) -> tuple[int, str | None]:
    """The confirmed `/arm`: re-arm the kills its question listed, only while they are exactly
    the ones tripped among the keys it named (`arm_refusal`); (how many, the refusal)."""
    refusal = arm_refusal(pending.kills, _tripped(session, pending.bots))
    if refusal is not None:
        return 0, refusal
    count = 0
    for trip in pending.kills:
        row = kill_ops.rearm_kill(
            session,
            scope=trip.scope,
            bot_id=trip.bot_id,
            confirmed=True,
            now=now,
            chat_id=chat_id,
        )
        count += row is not None
    return count, None


def _account(session: Session, bot: Bot) -> BrokerAccount | None:
    return configs_of(session).get(BrokerAccount, bot.broker_account_id)


AUTO_NOT_OWNER = "auto_not_owner"
AUTO_UNCONFIRMED = "auto_unconfirmed"


def auto_refusal(
    owner: uuid.UUID, account_users: Sequence[uuid.UUID | None], *, confirmed: bool
) -> str | None:
    """HAT-OVR-061, D210: who may set auto mode. Only the owner (`HAT_USER_ID`): every bot's
    account must belong to `owner` (an account not found has no user), checked before and after
    the second confirmation; and only once that confirmation is given (`AUTO_UNCONFIRMED`: ask
    for it)."""
    if not account_users or any(user != owner for user in account_users):
        return AUTO_NOT_OWNER
    return None if confirmed else AUTO_UNCONFIRMED


def _account_users(session: Session, bots: Sequence[Bot]) -> list[uuid.UUID | None]:
    accounts = (_account(session, bot) for bot in bots)
    return [None if account is None else account.user_id for account in accounts]


# A gate's code as `/mode` reports it (`mode_needs_gtt` predates the other gates).
_GATE_REASONS = {NO_GTT_DELIVERY: "mode_needs_gtt"}
_REFUSAL_TEXT = {
    "mode_needs_gtt": "the account is not active or has no GTT for delivery",
    "ddpi_not_confirmed": "DDPI is not confirmed on the account (admin confirm-ddpi)",
    "weights_not_confirmed": "the owner has not confirmed the base weights and cluster cap",
    "sleeve_not_live": "the bot's sleeve is not Live; ask again once it is promoted",
}


def mode_refused(reason: str) -> Reply:
    """A `/mode` refusal; a gate's refusal says which gate."""
    detail = _REFUSAL_TEXT.get(reason)
    return refused(reason, f"Refused: {reason} ({detail})" if detail else None)


def _mode_refusal(
    session: Session,
    bots: Sequence[Bot],
    to_mode: str,
    now: datetime,
    calendar: SessionCalendar,
    *,
    confirmed: bool,
) -> str | None:
    """Why the change may not be queued now (HAT-TG-032, HAT-GOV-085), checked without writing:
    the gates are read as they will stand at the open the change takes effect at. Auto before its
    second confirmation is `AUTO_UNCONFIRMED` once every other check passes."""
    if to_mode not in MODE_VALUES:
        return "unknown_mode"
    state = calendar.state(now)
    if state is SessionState.UNKNOWN:
        return "no_calendar"  # no row for today: whether the market is open is not known
    if state is not SessionState.CLOSED:
        return "market_hours"
    auto = None
    if to_mode == Mode.AUTO:
        owner = load_settings(process_env()).user_id
        auto = auto_refusal(owner, _account_users(session, bots), confirmed=confirmed)
        if auto == AUTO_NOT_OWNER:
            return auto
    try:
        opens = plan_mode_change(to_mode, now, calendar).effective_from
    except ModeChangeError:
        return "no_next_session"
    for bot in bots:
        refusal = gate_refusal(
            to_mode, account_gates(session, _account(session, bot), opens)
        ) or sleeve_refusal(to_mode, bot.lifecycle_state)
        if refusal is not None:
            return _GATE_REASONS.get(refusal, refusal)
    return auto


def queue_modes(
    session: Session,
    bots: Sequence[Bot],
    to_mode: str,
    now: datetime,
    chat_id: int,
    *,
    confirmed: bool = False,
) -> Reply:
    """Queue `to_mode` for every bot from the next session open, all or none. Refused, with
    nothing written, inside market hours, for an unknown mode, for an account without GTT, or
    for auto unless the owner's second confirmation is given (`confirmed`)."""
    calendar = session_calendar(session)
    if calendar is None:
        return refused("no_calendar")
    refusal = _mode_refusal(session, bots, to_mode, now, calendar, confirmed=confirmed)
    if refusal is not None:
        return mode_refused(refusal)
    effective = None
    for bot in bots:
        try:
            row = request_mode_change(
                session,
                bot_id=bot.id,
                to_mode=to_mode,
                requested_at=now,
                calendar=calendar,
                via="telegram",
                actor="user",
                chat_id=chat_id,
            )
        except HatError:
            session.rollback()
            return refused("mode_refused")
        effective = row.effective_from
    names = ", ".join(read_bots.label_of(session, b) for b in bots)
    when = effective.isoformat() if effective is not None else "the next session"
    return Reply(True, text=f"Mode {to_mode} for {names} from {when}")


def _mode(ctx: CommandContext, session: Session, args: Sequence[str]) -> Reply:
    """`/mode <bot> <shadow|confirm|auto>`: one bot by name, never all at once (D130)."""
    to_mode = args[-1].lower()
    bots = reads.bots_named(session, args[0])
    if not bots:
        return refused("unknown_bot")
    if len(bots) > 1:  # one name, several bots (one strategy on two accounts): name it by id
        return refused("ambiguous_bot", "Refused: ambiguous_bot (name the bot by its id)")
    if to_mode != Mode.AUTO:
        return queue_modes(session, bots, to_mode, ctx.now, ctx.chat_id)
    calendar = session_calendar(session)
    if calendar is None:
        return refused("no_calendar")
    refusal = _mode_refusal(session, bots, to_mode, ctx.now, calendar, confirmed=False)
    if refusal is not None and refusal != AUTO_UNCONFIRMED:
        return mode_refused(refusal)
    return _ask(ctx, "mode_auto", tuple(str(b.id) for b in bots))


Handler = Callable[[CommandContext, Session, Sequence[str]], Reply]
HANDLERS: dict[str, Handler] = {
    "/status": _status,
    "/positions": _positions,
    "/watchlist": _watchlist,
    "/pause": _pause,
    "/resume": _resume,
    "/mode": _mode,
    "/kill": _kill,
    "/flatten": _flatten,
    "/arm": _arm,
    "/login": _login,
}


def run_command(ctx: CommandContext, session: Session, text: str) -> tuple[str, str | None, Reply]:
    """Run one text message: (logged command, logged args, reply). Text that is not one of the
    ten commands with valid arguments is refused and changes nothing. A command addressed to
    another bot in a group (`/kill@other_bot`) is ignored: refused (`other_bot`) with no reply."""
    parts = text.strip().split()
    head = parts[0].split("@", 1)[0].lower() if parts else ""
    logged = head if head.startswith("/") else "text"
    if for_another_bot(text, ctx.bot_username):
        return logged, None, Reply(False, "other_bot")
    parsed = parse(text)
    if parsed is None:
        reason = "unknown_command" if head not in _ARITY else "bad_arguments"
        return logged, None, refused(reason)
    command, args = parsed
    return command, " ".join(args) or None, HANDLERS[command](ctx, session, args)

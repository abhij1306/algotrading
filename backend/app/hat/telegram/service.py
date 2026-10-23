"""The Telegram service: long polling, the allowlist and the command log (HAT-TG-005,
HAT-TG-010..014, HAT-DEC-I13).

`poll_once` reads the updates after the stored offset (`hat_telegram_cursor` `updates`) and
handles each in id order. The offset past an update commits in the same transaction as its
`hat_command_log` row (HAT-TG-011), on the error and unknown-chat paths too, so no update is
dropped: a crash before that commit leaves the offset where it was and Telegram delivers the
update again; a crash after it means the update was done. A handler that commits on its own
before that point (a Take's decision, a flatten's orders) is not applied twice when its update
comes again: the Take finds the decision recorded (`already_decided`), the `/flatten` and other
second confirmations find their single-use nonce gone with the process
(`no_pending_confirmation`), and `/kill` finds the switches tripped.

An update is never retried forever and never skipped for a passing fault: a database error that
may pass (`OperationalError`: a lost connection, a lock timeout, a serialization failure) leaves
the offset where it was, so the update comes again after the back-off; any other failure, inside
a handler or before one runs (a malformed update, a refusal that cannot be written), is logged
as an `error` row and the offset moves past it, so one bad update cannot hold back a `/kill`.

Under `run`, a button that may wait on the broker (a Take, a Skip, `/flatten`'s confirmation) is
handed to the button thread and the poll goes on, so it never holds back a `/kill`; text commands
and the other confirmations (`/arm`, auto mode) stay on the poll thread, in order. The poll after
a handed button asks Telegram for the updates past it, so a crash, a stop or a database error
while it runs drops the press (and its reply) rather than bringing it again; it is never applied
twice. A Take that committed before that is placed by `resume_entries`; one that did not leaves
the card's buttons.

`handle` turns one update into exactly one `hat_command_log` row (HAT-TG-012):

- from any chat but the allowlisted one: refused (`unknown_chat`), nothing else written, no reply
  and no answer to the button (HAT-TG-005);
- a text message: one of the ten commands (`commands.py`), else refused; the text path cannot
  reach an order (HAT-TG-031). A command with an `@bot` suffix runs only when the suffix is this
  bot's own username (`getMe`, asked once and kept); one naming another bot, or any suffix
  while the username is unknown, is refused (`other_bot`) with no reply;
- a button: an approval Take or Skip, or a second confirmation (`callbacks.py`).

The row, the handler's state change and its events commit together (the order path commits on
its own as it sends); the reply goes out after the commit. A handler that fails leaves only a
refused row (`error`). Logs carry codes and ids, never message text, tokens or broker text
(HAT-TG-014).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ..clock import SYSTEM_CLOCK, Clock
from ..time import utc_now
from . import store
from .callbacks import ApprovalDesk, ButtonContext, Flattener, run_callback
from .client import POLL_SECONDS, BotClient, TelegramError
from .commands import CommandContext, Reply, addressee, refused, run_command
from .confirm import PendingConfirmations
from .reads import LoginLinks, PriceSource
from .relay import EventRelay
from .tokens import ACTIONS, CONFIRM, ApprovalSigner

__all__ = ["Handled", "TelegramService", "run_relay"]

logger = logging.getLogger(__name__)

UNKNOWN_CHAT = "unknown_chat"
_BACKOFF_SECONDS = 5.0
_RELAY_SECONDS = 2.0


@dataclass(frozen=True)
class Handled:
    """What one update did: the logged command and the reply (None when nothing is sent)."""

    chat_id: int
    command: str
    reply: Reply | None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chat_of(update: dict[str, Any]) -> tuple[int | None, str | None, str | None, int | None]:
    """(chat id, text, button data, the button's message id) of an update."""
    message = update.get("message")
    if isinstance(message, dict):
        chat = message.get("chat") or {}
        text = message.get("text")
        return _int(chat.get("id")), text if isinstance(text, str) else "", None, None
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        carrier = callback.get("message") or {}
        chat = carrier.get("chat") or {}
        data = callback.get("data")
        return (
            _int(chat.get("id")),
            None,
            data if isinstance(data, str) else "",
            _int(carrier.get("message_id")),
        )
    return None, None, None, None


def _advance(session: Session, update_id: int | None, now: datetime) -> None:
    """Move the offset past `update_id` in the caller's transaction (HAT-TG-011); it never
    moves back, so an older update handled directly cannot make a newer one come again."""
    if update_id is None:
        return
    if update_id + 1 > store.cursor(session, store.UPDATES):
        store.set_cursor(session, store.UPDATES, update_id + 1, now)


class TelegramService:
    def __init__(
        self,
        client: BotClient,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        *,
        signer: ApprovalSigner,
        confirmations: PendingConfirmations | None = None,
        desk: ApprovalDesk | None = None,
        flattener: Flattener | None = None,
        links: LoginLinks | None = None,
        price: PriceSource | None = None,
        feed: Callable[[], str] | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._client = client
        self._factory = session_factory
        self._signer = signer
        self._confirmations = confirmations or PendingConfirmations()
        self._desk = desk
        self._flattener = flattener
        self._links = links
        self._price = price or (lambda _instrument: None)
        self._feed = feed or (lambda: "unknown")
        self._clock = clock
        self._username: str | None = None
        # the button thread (`run` starts it): a Take or a confirmed /flatten waits on the
        # broker there, so the /kill after it is not queued behind it
        self._buttons: ThreadPoolExecutor | None = None
        self._handed = 0  # the offset past the last button handed to that thread

    @property
    def chat_id(self) -> int:
        return self._client.chat_id

    # --- polling (HAT-TG-011) ------------------------------------------------------------------

    def poll_once(self, timeout: int = POLL_SECONDS) -> int:
        """One long poll: handle each new update in id order; `handle` commits the offset past
        it with its command-log row. Returns how many updates were handled. A transport failure
        raises `TelegramError`."""
        with self._factory() as session:
            offset = store.cursor(session, store.UPDATES)
        offset = max(offset, self._handed)  # a button still running is not asked for again
        updates = self._client.updates(offset, timeout)
        handled = 0
        for update in sorted(updates, key=lambda u: u["update_id"]):
            update_id = update["update_id"]
            if update_id < offset:
                continue
            offset = update_id + 1
            if self._buttons is not None and self._slow(update):
                self._buttons.submit(self._button, update_id, update)
                self._handed = offset
            else:
                self._handle_or_give_up(update_id, update)
            handled += 1
        return handled

    def _slow(self, update: dict[str, Any]) -> bool:
        """A button that may wait on the broker: a Take or Skip, or the confirmation of a
        /flatten. The other confirmations (`/arm`, auto mode) stay on the poll thread, in order
        with `/kill`, so an `/arm` confirmed before a `/kill` can never land after it."""
        callback = update.get("callback_query")
        data = callback.get("data") if isinstance(callback, dict) else None
        if not isinstance(data, str):
            return False
        if data.startswith(CONFIRM):
            return self._confirmations.action_of(data) == "flatten"
        return data[:1] in ACTIONS

    def _handle_or_give_up(self, update_id: int, update: dict[str, Any]) -> None:
        try:
            self.handle(update)
        except Exception as exc:
            if isinstance(exc, OperationalError):
                raise  # may pass: the offset stays and the update comes again
            self._give_up(update_id, exc)

    def _button(self, update_id: int, update: dict[str, Any]) -> None:
        """One button on the button thread. Its offset has gone to Telegram with the next poll,
        so a database error that may pass cannot bring it again: the press is answered with a
        refusal (the card keeps its buttons; a Take that committed is placed by
        `resume_entries`). Nothing here raises."""
        try:
            self._handle_or_give_up(update_id, update)
        except Exception as exc:
            logger.error("telegram button lost: %s", type(exc).__name__)
            callback_id = update["callback_query"].get("id")
            if isinstance(callback_id, str):
                text = _error_text(update["callback_query"].get("data"))
                self._attempt("answer", lambda: self._client.answer(callback_id, text))

    def _give_up(self, update_id: int, exc: Exception) -> None:
        """An update `handle` could not get through (a malformed update, a refusal that cannot be
        written): one `error` row (chat 0, since its chat may be what is malformed) and the
        offset past it, so the next update runs. A failure here raises: the update stays."""
        logger.error("telegram update skipped: %s", type(exc).__name__)
        now = utc_now(self._clock)
        with self._factory() as session:
            store.log_command(
                session,
                chat_id=0,
                command="update",
                args=None,
                accepted=False,
                reason="error",
                received_at=now,
            )
            _advance(session, update_id, now)
            session.commit()

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set, in a worker thread (the client blocks); buttons run on
        the button thread, text commands (the /kill path) on the poll thread. A button still
        queued at the stop is dropped, as a crash would drop it."""
        self._buttons = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hat-tg-buttons")
        try:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self.poll_once)
                except TelegramError as exc:
                    logger.warning("telegram poll failed: %s", exc.code)
                    await _sleep_or_stop(stop, _BACKOFF_SECONDS)
                except Exception as exc:  # a database outage must not end polling (/kill)
                    logger.error("telegram poll failed: %s", type(exc).__name__)
                    await _sleep_or_stop(stop, _BACKOFF_SECONDS)
        finally:
            buttons, self._buttons = self._buttons, None
            buttons.shutdown(wait=False, cancel_futures=True)

    # --- one update (HAT-TG-005, HAT-TG-012) ---------------------------------------------------

    def handle(self, update: dict[str, Any]) -> Handled:
        now = utc_now(self._clock)
        update_id = _int(update.get("update_id"))
        chat_id, text, data, card_message = _chat_of(update)
        callback = update.get("callback_query")
        callback_id = callback.get("id") if isinstance(callback, dict) else None
        if chat_id != self._client.chat_id:
            return self._refuse_stranger(update_id, chat_id, text, data, now)
        if data is None and text and addressee(text) is not None:
            self._learn_username()
        with self._factory() as session:
            try:
                command, args, reply = self._run(session, chat_id, text, data, now)
                store.log_command(
                    session,
                    chat_id=chat_id,
                    command=command,
                    args=args,
                    accepted=reply.accepted,
                    reason=None if reply.accepted else reply.reason,
                    received_at=now,
                )
                _advance(session, update_id, now)
                session.commit()
            except Exception as exc:
                session.rollback()
                if isinstance(exc, OperationalError):
                    raise  # may pass: nothing is logged and the offset stays (HAT-TG-011)
                logger.error("telegram update failed: %s", type(exc).__name__)
                command, reply = "error", refused("error", _error_text(data))
                store.log_command(
                    session,
                    chat_id=chat_id,
                    command="callback" if data is not None else "text",
                    args=None,
                    accepted=False,
                    reason="error",
                    received_at=now,
                )
                _advance(session, update_id, now)
                session.commit()
        self._respond(reply, data is not None, callback_id, card_message)
        return Handled(chat_id, command, reply)

    def _run(
        self, session: Session, chat_id: int, text: str | None, data: str | None, now: datetime
    ) -> tuple[str, str | None, Reply]:
        if data is not None:
            ctx = ButtonContext(
                chat_id, now, self._signer, self._confirmations, self._desk, self._flattener
            )
            return run_callback(ctx, session, data)
        if not text:
            return "text", None, refused("not_a_command")
        ctx_text = CommandContext(
            chat_id, now, self._confirmations, self._links, self._price, self._feed, self._username
        )
        return run_command(ctx_text, session, text)

    def _learn_username(self) -> None:
        """Ask `getMe` for the bot's own username until it is known; a failure leaves it unknown,
        so suffixed commands stay refused until a later message asks again."""
        if self._username is not None:
            return
        try:
            self._username = self._client.username()
        except TelegramError as exc:
            logger.warning("telegram getMe failed: %s", exc.code)

    def _refuse_stranger(
        self,
        update_id: int | None,
        chat_id: int | None,
        text: str | None,
        data: str | None,
        now: datetime,
    ) -> Handled:
        """HAT-TG-005: one refused row, nothing else, and no reply."""
        head = (text or "").strip().split(maxsplit=1)
        if data is not None:
            command = "callback"
        else:
            command = head[0][:64] if head else "text"
        with self._factory() as session:
            store.log_command(
                session,
                chat_id=chat_id if chat_id is not None else 0,
                command=command,
                args=None,
                accepted=False,
                reason=UNKNOWN_CHAT,
                received_at=now,
            )
            _advance(session, update_id, now)
            session.commit()
        return Handled(chat_id or 0, command, None)

    def _respond(
        self, reply: Reply, is_button: bool, callback_id: object, card_message: int | None
    ) -> None:
        """Answer the button, clear an accepted card's buttons and send the reply, each on its
        own: a button answer refused as too old must not lose the reply. Nothing here raises."""
        if is_button and isinstance(callback_id, str):
            self._attempt(
                "answer",
                lambda: self._client.answer(
                    callback_id, reply.text or ("OK" if reply.accepted else "Refused")
                ),
            )
            if card_message is not None and reply.accepted:
                self._attempt("clear", lambda: self._client.clear_buttons(card_message))
        if reply.text:
            self._attempt("reply", lambda: self._send_reply(reply))

    def _send_reply(self, reply: Reply) -> None:
        message_id = self._client.send(reply.text or "", reply.buttons)
        with self._factory() as session:
            store.record_message(
                session,
                chat_id=self._client.chat_id,
                kind="reply",
                ref_id=None,
                message_id=message_id,
                sent_at=utc_now(self._clock),
            )
            session.commit()

    @staticmethod
    def _attempt(what: str, step: Callable[[], object]) -> None:
        try:
            step()
        except TelegramError as exc:
            logger.warning("telegram %s not sent: %s", what, exc.code)
        except Exception as exc:  # the update is handled and logged; a lost link row is not fatal
            logger.error("telegram %s failed: %s", what, type(exc).__name__)


def _error_text(data: object) -> str:
    """The reply to an update that failed. A button's work (a Take's order, a flatten's sales)
    may have gone through before the failure, so its reply does not say it was refused."""
    if data is None:
        return "Refused: error"
    return "Error: the outcome is not known, see the alerts and /positions before tapping again"


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def run_relay(relay: EventRelay, stop: asyncio.Event) -> None:
    """Relay events every couple of seconds until `stop` is set."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(relay.run_once)
        except Exception as exc:  # a failed run is retried on the next tick
            logger.warning("telegram relay failed: %s", type(exc).__name__)
        await _sleep_or_stop(stop, _RELAY_SECONDS)

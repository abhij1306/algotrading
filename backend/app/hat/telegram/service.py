"""The Telegram service: long polling, the allowlist and the command log (08 §1; HAT-TG-005,
HAT-TG-010..014, HAT-DEC-I13).

`poll_once` reads the updates after the stored offset (`hat_telegram_cursor` `updates`) and, for
each in id order, first commits the offset past it and then handles it. An update is therefore
handled at most once across any crash or restart, and every update after the stored offset is
still handled (HAT-TG-011): a crash between the two steps drops that one update, never replays
it, because a replayed Take, `/flatten` confirmation or `/kill` must not act twice.

`handle` turns one update into exactly one `hat_command_log` row (HAT-TG-012):

- from any chat but the allowlisted one: refused (`unknown_chat`), nothing else written, no reply
  and no answer to the button (HAT-TG-005);
- a text message: one of the ten commands (`commands.py`), else refused; the text path cannot
  reach an order (HAT-TG-031);
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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ..time import utc_now
from . import store
from .callbacks import ApprovalDesk, ButtonContext, Flattener, run_callback
from .client import POLL_SECONDS, BotClient, TelegramError
from .commands import CommandContext, Reply, refused, run_command
from .confirm import PendingConfirmations
from .reads import LoginLinks, PriceSource
from .relay import EventRelay
from .tokens import ApprovalSigner

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
        clock: Callable[[], datetime] = utc_now,
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

    @property
    def chat_id(self) -> int:
        return self._client.chat_id

    # --- polling (HAT-TG-011) ------------------------------------------------------------------

    def poll_once(self, timeout: int = POLL_SECONDS) -> int:
        """One long poll: handle each new update once, committing the offset past it first.
        Returns how many updates were handled. A transport failure raises `TelegramError`."""
        with self._factory() as session:
            offset = store.cursor(session, store.UPDATES)
        updates = self._client.updates(offset, timeout)
        handled = 0
        for update in sorted(updates, key=lambda u: u["update_id"]):
            update_id = update["update_id"]
            if update_id < offset:
                continue
            offset = update_id + 1
            with self._factory() as session:
                store.set_cursor(session, store.UPDATES, offset, self._clock())
                session.commit()
            self.handle(update)
            handled += 1
        return handled

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set, in a worker thread (the client blocks)."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.poll_once)
            except TelegramError as exc:
                logger.warning("telegram poll failed: %s", exc.code)
                await _sleep_or_stop(stop, _BACKOFF_SECONDS)
            except Exception as exc:  # a database outage must not end polling (/kill must work)
                logger.error("telegram poll failed: %s", type(exc).__name__)
                await _sleep_or_stop(stop, _BACKOFF_SECONDS)

    # --- one update (HAT-TG-005, HAT-TG-012) ---------------------------------------------------

    def handle(self, update: dict[str, Any]) -> Handled:
        now = self._clock()
        chat_id, text, data, card_message = _chat_of(update)
        callback = update.get("callback_query")
        callback_id = callback.get("id") if isinstance(callback, dict) else None
        if chat_id != self._client.chat_id:
            return self._refuse_stranger(chat_id, text, data, now)
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
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.error("telegram update failed: %s", type(exc).__name__)
                command, reply = "error", refused("error")
                store.log_command(
                    session,
                    chat_id=chat_id,
                    command="callback" if data is not None else "text",
                    args=None,
                    accepted=False,
                    reason="error",
                    received_at=now,
                )
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
            chat_id, now, self._confirmations, self._links, self._price, self._feed
        )
        return run_command(ctx_text, session, text)

    def _refuse_stranger(
        self, chat_id: int | None, text: str | None, data: str | None, now: datetime
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
                sent_at=self._clock(),
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

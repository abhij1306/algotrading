"""The Telegram Bot API client (HAT-TG-010, HAT-TG-014, HAT-TG-020).

Outbound HTTPS only: `getMe` (the bot's own username), `getUpdates` long polling, `sendMessage`,
`answerCallbackQuery` and `editMessageReplyMarkup`. No webhook is ever registered and no socket
is bound: the transport refuses any other method. The client holds the one allowlisted chat id
and every message goes to it; no method takes a chat id (HAT-TG-020).

`HttpsTransport` posts with `requests`, imported only when a real call is made. (The plan named
aiohttp; HAT's import rule keeps socket libraries to the one live-data module, CONF
`imports.no_second_websocket`, so the client is synchronous and the poll loop runs it in a
worker thread.) A failure becomes `TelegramError` with a fixed code: the request URL carries the
bot token, so no exception text or chain from the HTTP layer is kept (HAT-TG-014). Tests fake
the `TelegramTransport` protocol; nothing in the test suite reaches the network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from ..errors import HatError

__all__ = [
    "ALLOWED_METHODS",
    "BotClient",
    "Button",
    "HttpsTransport",
    "TelegramError",
    "TelegramTransport",
    "refusal_retryable",
]

API_ROOT = "https://api.telegram.org"
ALLOWED_METHODS = frozenset(
    {"getMe", "getUpdates", "sendMessage", "answerCallbackQuery", "editMessageReplyMarkup"}
)
CODE_TRANSPORT = "telegram_transport"
CODE_TIMEOUT = "telegram_timeout"
CODE_REFUSED = "telegram_refused"
CODE_BAD_RESPONSE = "telegram_bad_response"
CODE_METHOD = "telegram_method_not_allowed"
# Long polling holds the request open this long; the HTTP timeout allows for it.
POLL_SECONDS = 25
_HTTP_SLACK = 10
_SEND_TIMEOUT = 15
MAX_TEXT = 4096

Button = tuple[str, str]  # (label, callback data)


# The refusals that will not pass on a retry (a bad request, a forbidden or missing chat or
# message); a 429, a 5xx, a timeout or a transport failure may.
PERMANENT_STATUSES = frozenset({400, 403, 404})


def refusal_retryable(status: int) -> bool:
    """A refusal with this HTTP status may succeed when sent again."""
    return status not in PERMANENT_STATUSES


class TelegramError(HatError):
    """A Bot API call failed; the message is a fixed code, never server or HTTP text.
    `retryable` is False only for a refusal that will not pass (`refusal_retryable`)."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class TelegramTransport(Protocol):
    def call(self, method: str, payload: Mapping[str, Any], timeout: float) -> object:
        """Call one Bot API method and return its `result`; raise `TelegramError`."""
        ...


class HttpsTransport:
    """The Bot API over HTTPS POST. Never constructed by tests."""

    def __init__(self, token: str, post: Callable[..., Any] | None = None) -> None:
        self._token = token
        self._post = post

    def __repr__(self) -> str:
        return "HttpsTransport(<token>)"

    def call(self, method: str, payload: Mapping[str, Any], timeout: float) -> object:
        if method not in ALLOWED_METHODS:
            raise TelegramError(CODE_METHOD)
        post = self._post
        if post is None:
            import requests  # deferred: only a real call needs the HTTP stack

            post = requests.post
        try:
            response = post(
                f"{API_ROOT}/bot{self._token}/{method}", json=dict(payload), timeout=timeout
            )
            status = int(response.status_code)
            body = response.json()
        except Exception as exc:  # the text and the chain may carry the URL, so neither is kept
            timed_out = isinstance(exc, TimeoutError) or type(exc).__name__.endswith("Timeout")
            raise TelegramError(CODE_TIMEOUT if timed_out else CODE_TRANSPORT) from None
        if not isinstance(body, dict):
            raise TelegramError(CODE_BAD_RESPONSE)
        if status >= 400 or body.get("ok") is not True:
            raise TelegramError(CODE_REFUSED, retryable=refusal_retryable(status))
        return body.get("result")


def _markup(buttons: Sequence[Sequence[Button]]) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons
        ]
    }


class BotClient:
    """The bot, bound to its one allowlisted chat."""

    def __init__(self, transport: TelegramTransport, chat_id: int) -> None:
        self._transport = transport
        self.chat_id = chat_id

    def username(self) -> str | None:
        """The bot's own username (`getMe`), None when the answer has none."""
        result = self._transport.call("getMe", {}, _SEND_TIMEOUT)
        name = result.get("username") if isinstance(result, dict) else None
        return name if isinstance(name, str) and name else None

    def updates(self, offset: int, timeout: int = POLL_SECONDS) -> list[dict[str, Any]]:
        """The updates from `offset` on (long polling for up to `timeout` seconds)."""
        result = self._transport.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout + _HTTP_SLACK,
        )
        if not isinstance(result, list):
            raise TelegramError(CODE_BAD_RESPONSE)
        return [u for u in result if isinstance(u, dict) and isinstance(u.get("update_id"), int)]

    def send(self, text: str, buttons: Sequence[Sequence[Button]] = ()) -> int:
        """Send `text` (plain text, cut to Telegram's limit) to the allowlisted chat and return
        the message id."""
        payload: dict[str, object] = {"chat_id": self.chat_id, "text": text[:MAX_TEXT]}
        if buttons:
            payload["reply_markup"] = _markup(buttons)
        result = self._transport.call("sendMessage", payload, _SEND_TIMEOUT)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise TelegramError(CODE_BAD_RESPONSE)
        return message_id

    def answer(self, callback_id: str, text: str) -> None:
        """Acknowledge a button press with a short notice."""
        self._transport.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:200]},
            _SEND_TIMEOUT,
        )

    def clear_buttons(self, message_id: int) -> None:
        """Remove the buttons of one of the bot's messages in the allowlisted chat."""
        self._transport.call(
            "editMessageReplyMarkup",
            {"chat_id": self.chat_id, "message_id": message_id, "reply_markup": _markup(())},
            _SEND_TIMEOUT,
        )

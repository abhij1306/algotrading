"""The Fyers SDK session: credentials from the token file, the SDK model built from them, and
token validation (K2; cut down from the legacy `FyersClient`, HAT-OPS-051).

The session fails closed. A token that is missing, unreadable, without a client id or past its
expiry is never installed, and a token that expires while the process runs stops being used on the
next access to `fyers`, even by a caller that never validates. A re-login (a new token file) is
picked up by `validate_token`, and `reset_fyers_session` drops the process-wide session so the next
use reloads it.

This module and `fyers_auth.py` / `fyers_tokens.py` are the only HAT code that touches Fyers
credentials or the token file; every other module asks the session for a ready SDK model.
Logs name the token's expiry and the file, never the token.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from .fyers_tokens import TokenStore, TokenStoreError, token_store

__all__ = ["FyersSession", "get_fyers_session", "reset_fyers_session", "sdk_model"]

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
ModelFactory = Callable[..., Any]


def sdk_model(**kwargs: str) -> Any:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
    """The SDK's `FyersModel`; the SDK is imported on first use to keep startup fast."""
    from fyers_apiv3 import fyersModel

    module: ModuleType = fyersModel
    return module.FyersModel(**kwargs)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FyersSession:
    """One SDK model for the token on disk. `store`, `now` and `model_factory` are injectable so a
    test runs it on a temporary file, a frozen clock and a recording SDK stand-in; production passes
    none."""

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        now: Clock = _utc_now,
        model_factory: ModelFactory = sdk_model,
        load: bool = True,
    ) -> None:
        self._store = store if store is not None else token_store
        self._now = now
        self._model_factory = model_factory
        self._fyers: Any = None
        self.client_id: str | None = None
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None
        self._token_file_mtime_ns: int | None = None
        self._expired_token_mtime_ns: int | None = None
        self._connecting = False
        self._validating_token = False
        self._missing_token_file_logged = False
        self._missing_credentials_logged = False
        if load:
            self._load_credentials()
            # Connect without a network call; validation happens in `validate_token`.
            self._connect(skip_validation=True)

    @property
    def fyers(self) -> Any:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
        """The SDK model, or None once the loaded token has passed its expiry."""
        if self._fyers is not None and self.token_expires_at is not None:
            if self._now() > self.token_expires_at:
                logger.warning("Fyers token expired on %s; dropping it", self.token_expires_at)
                self._drop_credentials()
        return self._fyers

    @fyers.setter
    def fyers(self, model: Any) -> None:  # noqa: ANN401 - the untyped fyers_apiv3 FyersModel
        self._fyers = model

    def _load_credentials(self) -> None:
        """Load the token file. Fails closed: when the file is gone, unusable or unreadable, the
        cached credentials and the SDK model are dropped so no call goes out with an old token."""
        store = self._store
        try:
            mtime_ns = store.mtime_ns()
            if mtime_ns is None:
                if not self._missing_token_file_logged:
                    logger.warning("Fyers token file not found at %s", store.path)
                    self._missing_token_file_logged = True
                self._drop_credentials()
                return
            self._token_file_mtime_ns = mtime_ns
            token = store.load()
        except TokenStoreError:
            logger.exception("Failed to load Fyers credentials")
            self._drop_credentials()
            return
        if token is None:
            self._drop_credentials()  # the store logged why the file is not a usable token
            return

        if not token.is_usable(self._now()):
            # An expired token (or one without a client id) is never installed. Warn once per file.
            if self._expired_token_mtime_ns != mtime_ns:
                if token.client_id:
                    logger.warning("Fyers token expired on %s", token.expires_at)
                else:
                    logger.warning("Fyers token file has no client id; ignoring it")
                self._expired_token_mtime_ns = mtime_ns
            self._drop_credentials()
            return

        self.client_id = token.client_id
        self.access_token = token.access_token
        self.token_expires_at = token.expires_at
        self._missing_credentials_logged = False

    def _drop_credentials(self) -> None:
        """Forget the token and the SDK model built from it (logout or unusable file)."""
        self.client_id = None
        self.access_token = None
        self.token_expires_at = None
        self._token_file_mtime_ns = None
        self.fyers = None

    def _refresh_credentials_if_changed(self) -> bool:
        """Reload the token when the file changed on disk. True when a different token was
        loaded. A removed file drops the cached credentials (logout) and returns False."""
        try:
            current_mtime_ns = self._store.mtime_ns()
            if current_mtime_ns is None:
                if self.access_token or self.fyers is not None:
                    logger.info("Fyers token file removed; dropping cached credentials")
                    self._drop_credentials()
                return False
            if (
                self._token_file_mtime_ns == current_mtime_ns
                and self.client_id
                and self.access_token
            ):
                return False

            old_token = self.access_token
            self._load_credentials()
            return bool(self.access_token and self.access_token != old_token)
        except TokenStoreError:
            logger.exception("Failed checking token file changes")
            self._drop_credentials()
            return False

    def _connect(self, *, skip_validation: bool = False, quiet: bool = False) -> None:
        """Build the SDK model from the loaded credentials."""
        if self._connecting:
            return
        if not self.client_id or not self.access_token:
            if not quiet and not self._missing_credentials_logged:
                logger.warning("Fyers credentials not loaded.")
                self._missing_credentials_logged = True
            return

        self._connecting = True
        try:
            self.fyers = self._model_factory(
                client_id=self.client_id, token=self.access_token, log_path=""
            )
            if skip_validation:
                logger.info("Fyers client connected (validation skipped)")
            elif self.validate_token():
                logger.info("Fyers client connected")
            else:
                logger.warning("Fyers token validation failed")
        except Exception:
            logger.exception("Error connecting to Fyers")
        finally:
            self._connecting = False

    def is_token_expired(self) -> bool:
        return self.token_expires_at is not None and self._now() > self.token_expires_at

    def validate_token(self) -> bool:
        """Whether the token works: a lightweight profile call, after reloading a token that
        changed on disk. False when there is no usable token, it expired, or Fyers refused it."""
        if self._validating_token:
            return bool(self.fyers)

        self._validating_token = True
        try:
            if self._refresh_credentials_if_changed():
                logger.info("Detected updated Fyers token on disk, reconnecting.")
                self._connect(skip_validation=True)

            if not self.fyers:
                # One extra attempt in case the model was not built at startup.
                self._connect(skip_validation=True, quiet=True)
                if not self.fyers:
                    return False

            if self.is_token_expired():  # no network call for a token known to be expired
                return False
            return self._validate_with_profile()
        finally:
            self._validating_token = False

    def _profile_ok(self) -> bool:
        response = self.fyers.get_profile()
        if response.get("s") == "ok":
            return True
        logger.warning("Fyers token invalid (code %s)", response.get("code"))
        return False

    def _validate_with_profile(self) -> bool:
        """Profile call, retried once after reloading a token that changed on disk (a fresh
        login while the app is running)."""
        try:
            if self._profile_ok():
                return True
            if self._refresh_credentials_if_changed():
                self._connect(skip_validation=True)
                return self._profile_ok()
            return False
        except Exception:
            logger.exception("Fyers token validation failed")
            if not self._refresh_credentials_if_changed():
                return False
            try:
                self._connect(skip_validation=True)
                return self._profile_ok()
            except Exception:
                logger.exception("Fyers token validation retry failed")
                return False


_session: FyersSession | None = None
_generation = 0
_session_lock = threading.Lock()


def get_fyers_session() -> FyersSession:
    """The process-wide session over the default token file, built on first use.

    A reset that happens while a session is being built (a logout or a re-login) discards that
    session: it may hold the token read before the reset, so it is never installed."""
    global _session
    for _ in range(3):
        with _session_lock:
            if _session is not None:
                return _session
            generation = _generation
        built = FyersSession()
        with _session_lock:
            if generation == _generation:
                _session = built
                return built
    return FyersSession()


def reset_fyers_session() -> None:
    """Drop the process-wide session so fresh credentials load on the next `get_fyers_session`."""
    global _session, _generation
    with _session_lock:
        _session = None
        _generation += 1

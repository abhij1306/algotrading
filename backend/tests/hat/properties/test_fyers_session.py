"""PBT fyers_session.* (HAT-OPS-051, K2): the session builds an SDK model only from a token that is
usable now, drops it when the token expires, is removed or turns unreadable, picks up a re-login,
and validates with at most one retry. Runs on a temporary token file, a controlled clock and a
recording model; nothing reaches Fyers."""

import os
from datetime import timedelta
from typing import Any

from hat_fyers_support import T0, Clock, token_store, write_token
from hypothesis import given
from hypothesis import strategies as st

from app.hat.execution.fyers_session import FyersSession
from app.hat.execution.fyers_tokens import TokenStore


class Model:
    """A fake SDK model for one token: counts profile calls, answers from a shared script."""

    def __init__(self, token: str, script: list[Any]) -> None:
        self.token = token
        self.script = script
        self.profile_calls = 0

    def get_profile(self) -> Any:  # noqa: ANN401
        self.profile_calls += 1
        if not self.script:
            return {"s": "ok"}
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Factory:
    """A `model_factory` that records every model it builds."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = script if script is not None else []
        self.models: list[Model] = []

    def __call__(self, *, client_id: str, token: str, log_path: str) -> Model:
        assert client_id
        assert token
        model = Model(token, self.script)
        self.models.append(model)
        return model


def touch_new(store: TokenStore) -> None:
    """Make the file's mtime differ from any earlier write (coarse filesystem clocks)."""
    stat = store.path.stat()
    os.utime(store.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


FILE_STATES = st.sampled_from(["valid", "expired", "no_client", "corrupt", "absent", "naive"])


def make_file(store: TokenStore, state: str, now_offset: timedelta = timedelta(0)) -> None:
    now = T0 + now_offset
    if state == "valid":
        write_token(store, expires_at=now + timedelta(hours=5))
    elif state == "expired":
        write_token(store, expires_at=now - timedelta(seconds=1))
    elif state == "no_client":
        write_token(store, client_id=None, expires_at=now + timedelta(hours=5))
    elif state == "naive":
        write_token(store, expires_at=(now + timedelta(hours=5)).replace(tzinfo=None))
    elif state == "corrupt":
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text('{"access_token": "tok-A", "expires_', encoding="utf-8")


@given(state=FILE_STATES)
def test_a_model_exists_only_for_a_usable_token(state: str) -> None:
    """At startup the session holds an SDK model exactly when the file holds a token that has a
    client id and has not expired; a missing, corrupt, expired or client-less file gives none."""
    with token_store() as store:
        make_file(store, state)
        factory = Factory()
        session = FyersSession(store, now=Clock(), model_factory=factory)
        usable = state in {"valid", "naive"}  # a naive stored expiry is read as UTC, still ahead
        assert (session.fyers is not None) == usable
        assert len(factory.models) == (1 if usable else 0)
        assert (session.access_token is not None) == usable
        assert all(m.profile_calls == 0 for m in factory.models)  # startup makes no network call


@given(run_for=st.integers(1, 8 * 3600), grace=st.integers(0, 3600))
def test_a_token_that_expires_while_running_stops_being_used(run_for: int, grace: int) -> None:
    """A caller that never validates still gets None once the clock passes the expiry, and the
    credentials are gone with it; before the expiry it keeps the same model."""
    with token_store() as store:
        write_token(store, expires_at=T0 + timedelta(hours=4))
        clock = Clock()
        session = FyersSession(store, now=clock, model_factory=Factory())
        model = session.fyers
        assert model is not None
        clock.advance(seconds=run_for)
        expired = clock.now > T0 + timedelta(hours=4)
        if expired:
            assert session.fyers is None
            assert session.access_token is None
            assert session.client_id is None
        else:
            assert session.fyers is model
        clock.advance(seconds=grace)
        if expired:
            assert session.fyers is None


@given(after=st.sampled_from(["removed", "corrupt", "expired", "no_client"]))
def test_validation_never_keeps_a_model_after_the_file_goes_bad(after: str) -> None:
    """After the token file is removed (logout), corrupted, or replaced by an expired or
    client-less token, `validate_token` is False, makes no profile call and leaves no model."""
    with token_store() as store:
        write_token(store, expires_at=T0 + timedelta(hours=4))
        factory = Factory()
        session = FyersSession(store, now=Clock(), model_factory=factory)
        assert session.fyers is not None
        if after == "removed":
            store.path.unlink()
        else:
            make_file(store, after)
            touch_new(store)
        assert session.validate_token() is False
        assert session.fyers is None
        assert session.access_token is None
        assert sum(m.profile_calls for m in factory.models) == 0


@given(
    old=st.just("tok-A"),
    new=st.just("tok-B"),
    script=st.lists(st.sampled_from(["ok", "bad"]), max_size=3),
)
def test_a_relogin_is_picked_up(old: str, new: str, script: list[str]) -> None:
    """A new token written while the app runs is used by the next validation: the model is rebuilt
    with the new token, and the old token is never used again."""
    with token_store() as store:
        write_token(store, access_token=old, expires_at=T0 + timedelta(hours=4))
        factory = Factory(
            [{"s": "ok"} if s == "ok" else {"s": "error", "code": -16} for s in script]
        )
        session = FyersSession(store, now=Clock(), model_factory=factory)
        write_token(store, access_token=new, expires_at=T0 + timedelta(hours=9))
        touch_new(store)
        session.validate_token()
        assert session.access_token == new
        assert factory.models[-1].token == new
        used_after = [m for m in factory.models if m.token == old and m.profile_calls]
        assert used_after == []


@given(refused=st.booleans(), relogin=st.booleans(), raises=st.booleans())
def test_validation_makes_at_most_two_profile_calls(
    refused: bool, relogin: bool, raises: bool
) -> None:
    """A profile call is retried only after a re-login is found on disk, and only once: with no
    new token one call is made; with one, at most two, and the result is the last answer."""
    with token_store() as store:
        write_token(store, access_token="tok-A", expires_at=T0 + timedelta(hours=4))
        first: Any = (
            RuntimeError("boom")
            if raises
            else ({"s": "error", "code": -16} if refused else {"s": "ok"})
        )
        factory = Factory([first, {"s": "ok"}])
        session = FyersSession(store, now=Clock(), model_factory=factory)
        if relogin:
            write_token(store, access_token="tok-B", expires_at=T0 + timedelta(hours=9))
            touch_new(store)
        result = session.validate_token()
        calls = sum(m.profile_calls for m in factory.models)
        assert calls <= 2
        if relogin:
            # the reload happens before the profile call, so the new token's first answer decides
            assert factory.models[-1].token == "tok-B"
        if not relogin and (refused or raises):
            assert result is False
            assert calls == 1
        if not relogin and not (refused or raises):
            assert result is True
            assert calls == 1


@given(offset=st.integers(1, 100_000))
def test_validation_of_an_expired_token_makes_no_call(offset: int) -> None:
    """Once the stored expiry has passed, `validate_token` is False without any profile call."""
    with token_store() as store:
        write_token(store, expires_at=T0 + timedelta(hours=1))
        clock = Clock()
        factory = Factory()
        session = FyersSession(store, now=clock, model_factory=factory)
        clock.advance(hours=1, seconds=offset)
        assert session.validate_token() is False
        assert sum(m.profile_calls for m in factory.models) == 0


@given(state=st.sampled_from(["valid", "expired"]))
def test_nested_validation_does_not_recurse(state: str) -> None:
    """A validation started from inside a validation returns at once with whether a model exists,
    and makes no profile call."""
    with token_store() as store:
        make_file(store, state)
        factory = Factory()
        session = FyersSession(store, now=Clock(), model_factory=factory)
        session._validating_token = True  # as if inside `validate_token`
        assert session.validate_token() is (state == "valid")
        assert sum(m.profile_calls for m in factory.models) == 0


@given(resets=st.integers(0, 4))
def test_a_reset_during_construction_never_installs_the_old_session(resets: int) -> None:
    """A logout or re-login (a reset) while the process-wide session is being built discards that
    session: whatever is installed afterwards was built after the last reset, and a session built
    before it is never returned from a later `get_fyers_session()`."""
    from unittest import mock

    from app.hat.execution import fyers_session as module

    built: list[object] = []
    pending = [resets]

    class Slow:
        def __init__(self) -> None:
            self.generation = module._generation  # the reset count this build began under
            built.append(self)
            if pending[0] > 0:
                pending[0] -= 1
                module.reset_fyers_session()  # a logout lands while this one is being built

    module.reset_fyers_session()
    with mock.patch.object(module, "FyersSession", Slow):
        got = module.get_fyers_session()
        assert got is built[-1]
        assert module._session in (None, got)
        if module._session is got:
            assert got.generation == module._generation  # never one built before a reset
        if resets < 3:
            assert module._session is got  # installed once no reset interrupted the build
    module.reset_fyers_session()

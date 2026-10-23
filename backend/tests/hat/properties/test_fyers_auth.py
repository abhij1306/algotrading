"""PBT fyers_auth.* (HAT-OPS-051, K2): the login flow trades an auth code for a token through one
store, fails closed on anything Fyers or the disk gets wrong, never logs or raises the token, the
secret or the code, lets a logout win over a login in flight, and keeps its status cache honest.
Runs on a temporary token file and a recording SDK session."""

import os
import threading
from collections.abc import Callable, Mapping
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, NoReturn
from unittest import mock

import pytest
from hat.fakes.broker import FakeBroker
from hat.fakes.fyers_sdk import RecordingSdk, RecordingSession
from hat_fyers_support import (
    LIVE_REPLIES,
    T0,
    Ticker,
    captured_logs,
    profile_ok,
    token_reply,
    token_store,
    write_token,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.exc import OperationalError

from app.hat.execution.broker import BrokerLoginError, SessionStatus
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.fyers_auth import (
    ACCOUNT_UNCONFIRMED,
    STATUS_CACHE_SECONDS,
    WRONG_ACCOUNT,
    FyersAuthError,
    FyersAuthService,
    FyersCredentials,
    LoginCancelledError,
    MissingCredentialsError,
    TokenRejectedError,
    WrongAccountError,
    credentials_from_env,
)
from app.hat.execution.fyers_tokens import TokenStore, TokenStoreError

SECRET = "SECRETKEY-9f8e7d6c"
CREDS = FyersCredentials("APP-100", SECRET, "https://app.example.test/cb")
codes = st.from_regex(r"AUTHCODE[0-9a-f]{10,24}", fullmatch=True)
tokens = st.from_regex(r"TKN[0-9a-f]{16,32}", fullmatch=True)
MARKER = "echoed-by-fyers-9a1b"

GOOD = st.builds(token_reply, tokens)
# what the profile read after a login answers: the recorded ok reply (with its user id), the
# recorded -16 refusal, or an SDK failure; only the first gives a user id
PROFILES = st.one_of(
    st.sampled_from(["XA1234", "XB9999"]).map(profile_ok),
    st.just(LIVE_REPLIES["profile_invalid_token"]),
    st.just(RuntimeError(f"profile failed {MARKER}")),
)
BAD_REPLIES = st.one_of(
    st.just({"s": "error", "code": -413, "message": f"invalid code {MARKER}"}),
    st.just({"s": "ok", "code": 200}),  # no token
    st.just({"s": "ok", "code": 200, "access_token": ""}),
    st.just({"s": "ok", "access_token": 12345}),
    st.just({"access_token": f"TKNnoStatus{MARKER}"}),  # token without s=ok
    st.just({"s": "no_data"}),
    st.just({}),
    st.just(None),
    st.just(["s", "ok"]),
    st.just(f"not a mapping {MARKER}"),
    st.just(RuntimeError(f"connection reset: {MARKER}")),
    st.just(TimeoutError(MARKER)),
    st.just(ValueError(f"bad auth_code {MARKER}")),
)


class Harness:
    def __init__(
        self,
        store: TokenStore,
        reply: object,
        *,
        ticker: Ticker | None = None,
        profile: object = None,
    ) -> None:
        self.sessions: list[RecordingSession] = []
        self.reply = reply
        self.events: list[str] = []
        self.ticker = ticker or Ticker()

        def factory(**kwargs: str) -> RecordingSession:
            session = RecordingSession(self.reply, **kwargs)
            self.sessions.append(session)
            return session

        self.service = FyersAuthService(
            store,
            account_refs=frozenset,  # no broker account yet: any user logs in
            session_factory=factory,
            credentials=lambda: CREDS,
            on_token_change=lambda: self.events.append("token_change"),
            clock=self.ticker,
            model_factory=lambda **_kwargs: RecordingSdk(
                profile if profile is not None else profile_ok("XA0001")
            ),
        )
        self.service.add_hook("login", lambda: self.events.append("login"))
        self.service.add_hook("disconnect", lambda: self.events.append("disconnect"))


def listing(store: TokenStore) -> list[str]:
    return sorted(p.name for p in store.path.parent.iterdir()) if store.path.parent.exists() else []


@given(reply=BAD_REPLIES, code=codes)
def test_a_failed_exchange_fails_closed(reply: object, code: str) -> None:
    """Whatever Fyers answers or raises (an error, a missing or empty token, a non-mapping, an SDK
    exception), the exchange raises a fixed-message error, writes no file, runs no hook and
    leaves no auth code, secret or echoed Fyers text in the error or the log."""
    with captured_logs() as logs, token_store() as store:
        harness = Harness(store, reply)
        with pytest.raises(FyersAuthError) as caught:
            harness.service.exchange(code)
        shown = [str(caught.value), repr(caught.value), caught.value.message, logs.text]
        assert harness.events == []
        assert listing(store) == []
        assert harness.service.status().connected is False
    for text in shown:
        assert code not in text
        assert SECRET not in text
        assert MARKER not in text


@given(reply=GOOD, code=codes, profile=PROFILES)
def test_a_good_exchange_saves_once_and_reports_in_order(
    reply: dict[str, Any], code: str, profile: object
) -> None:
    """A good reply saves the token for the app's client id, reports connected with the user id
    of the account's profile straight away and after the status cache expires (none, never an
    empty one, when the profile cannot be read: the login stands), runs `token_change` before
    `login`, and never logs the token, the secret, the code or the profile's error text."""
    user_id = profile["data"]["fy_id"] if isinstance(profile, dict) and "data" in profile else None
    with captured_logs() as logs, token_store() as store:
        harness = Harness(store, reply, profile=profile)
        assert harness.service.status().connected is False  # primes the cache
        status = harness.service.exchange(code)
        assert status.connected
        assert status.user_id == user_id
        harness.ticker.now += STATUS_CACHE_SECONDS + 1
        assert harness.service.status().user_id == user_id
        saved = store.load()
        assert saved is not None
        assert (saved.access_token, saved.client_id) == (reply["access_token"], CREDS.client_id)
        assert harness.events == ["token_change", "login"]
        assert harness.sessions[-1].auth_code == code
        assert harness.service.status().connected is True
        assert listing(store) == [store.path.name]
        text = logs.text
    for secret in (reply["access_token"], SECRET, code, MARKER):
        assert secret not in text


@given(reply=GOOD, code=codes)
def test_a_save_failure_writes_nothing_and_runs_no_hook(reply: dict[str, Any], code: str) -> None:
    """When the token cannot be saved the error is a `TokenStoreError`, the old file stays, and no
    hook runs, so nothing is told a token changed."""
    with token_store() as store:
        write_token(store, access_token="tok-OLD", expires_at=T0 + timedelta(days=30))
        before = store.path.read_bytes()
        harness = Harness(store, reply)
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(TokenStoreError):
                harness.service.exchange(code)
        assert store.path.read_bytes() == before
        assert harness.events == []
        assert listing(store) == [store.path.name]


@given(reply=GOOD, code=codes)
def test_a_logout_during_a_login_wins(reply: dict[str, Any], code: str) -> None:
    """A logout that runs while Fyers is still answering cancels that login: the token is not
    written, the error says so, and no `login` hook fires."""
    with token_store() as store:
        harness = Harness(store, reply)
        original = harness.service._generate_token

        def slow_generate(creds: FyersCredentials, auth_code: str) -> Mapping[str, Any]:
            result = original(creds, auth_code)
            harness.service.disconnect()  # the user logs out while the exchange is in flight
            return result

        harness.service._generate_token = slow_generate  # type: ignore[method-assign]
        with pytest.raises(LoginCancelledError):
            harness.service.exchange(code)
        assert listing(store) == []
        assert "login" not in harness.events
        assert harness.service.status().connected is False


@given(reply=GOOD, code=codes)
@settings(max_examples=25, deadline=None)
def test_a_logout_that_is_still_running_cancels_a_login(reply: dict[str, Any], code: str) -> None:
    """A logout counts from its start, not its end: a login whose reply arrives while a slow
    disconnect hook is still running is cancelled, writes no token and fires no `login` hook."""
    with token_store() as store:
        harness = Harness(store, reply)
        service = harness.service
        in_flight, may_reply = threading.Event(), threading.Event()
        original = service._generate_token

        def slow_generate(creds: FyersCredentials, auth_code: str) -> Mapping[str, Any]:
            in_flight.set()
            may_reply.wait(5)
            return original(creds, auth_code)

        service._generate_token = slow_generate  # type: ignore[method-assign]
        outcome: list[BaseException | None] = []

        def login() -> None:
            try:
                service.exchange(code)
                outcome.append(None)
            except FyersAuthError as error:
                outcome.append(error)

        thread = threading.Thread(target=login)
        thread.start()
        assert in_flight.wait(5)

        def slow_hook() -> None:
            may_reply.set()  # the reply arrives while the logout is still running
            thread.join(5)

        service._hooks["disconnect"][:] = [slow_hook]
        service.disconnect()
        thread.join(5)
        assert isinstance(outcome[0], LoginCancelledError)
        assert listing(store) == []
        assert "login" not in harness.events


@given(
    reply=GOOD, code=codes, failing=st.sets(st.sampled_from(["token_change", "login"]), min_size=1)
)
def test_hooks_are_isolated(reply: dict[str, Any], code: str, failing: set[str]) -> None:
    """A hook that raises never fails the login or the logout, never stops the other hooks, and its
    message (not just its type) stays out of the log."""
    with captured_logs() as logs, token_store() as store:
        harness = Harness(store, reply)
        service = harness.service

        def boom(event: str) -> Callable[[], None]:
            def hook() -> None:
                harness.events.append(f"{event}!")
                raise RuntimeError(f"hook secret {MARKER}")

            return hook

        service._hooks["token_change"][:] = [
            boom("token_change")
            if "token_change" in failing
            else (lambda: harness.events.append("token_change"))
        ]
        service._hooks["login"][:] = (
            [boom("login")] if "login" in failing else [lambda: harness.events.append("login")]
        )
        service._hooks["disconnect"][:] = [
            boom("disconnect"),
            lambda: harness.events.append("disconnect"),
        ]
        service.add_hook("login", lambda: harness.events.append("login-last"))
        assert service.exchange(code).connected is True
        assert "login-last" in harness.events
        service.disconnect()
        assert "disconnect" in harness.events  # the hook after the failing one still ran
        assert listing(store) == []
        assert MARKER not in logs.text


@given(reply=GOOD, code=codes)
def test_disconnect_runs_its_hooks_before_the_file_goes(reply: dict[str, Any], code: str) -> None:
    """Disconnect hooks (the live feed stopping) run while the token is still on disk, then the file
    is removed, then `token_change` fires; a second disconnect is harmless."""
    with token_store() as store:
        harness = Harness(store, reply)
        harness.service.exchange(code)
        harness.events.clear()
        present: list[bool] = []
        harness.service._hooks["disconnect"][:] = [lambda: present.append(store.path.exists())]
        harness.service.disconnect()
        assert present == [True]
        assert not store.path.exists()
        assert harness.events == ["token_change"]
        harness.service.disconnect()
        assert harness.service.status().connected is False


@settings(max_examples=300)
@given(
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("tick"), st.floats(0, 2 * STATUS_CACHE_SECONDS)),
            st.tuples(st.just("login"), st.just(0.0)),
            st.tuples(st.just("logout"), st.just(0.0)),
            st.tuples(st.just("status"), st.just(0.0)),
            st.tuples(st.just("file_removed"), st.just(0.0)),
        ),
        max_size=14,
    )
)
def test_status_cache_model(steps: list[tuple[str, float]]) -> None:
    """The status equals a model of the cache: it is recomputed from disk once the cache is older
    than the window, and always right after a login or a logout, whatever the file did meanwhile."""
    with token_store() as store:
        ticker = Ticker()
        harness = Harness(store, token_reply("TKN" + "a" * 16), ticker=ticker)
        service = harness.service
        cached: tuple[bool, float] | None = None  # (connected, taken_at)
        for kind, amount in steps:
            if kind == "tick":
                ticker.now += amount
            elif kind == "login":
                service.exchange("AUTHCODE0123456789")
                cached = None
            elif kind == "logout":
                service.disconnect()
                cached = None
            elif kind == "file_removed":
                if store.path.exists():
                    store.path.unlink()
            else:
                on_disk = store.load() is not None and store.load().is_usable()  # type: ignore[union-attr]
                if cached is not None and ticker.now - cached[1] < STATUS_CACHE_SECONDS:
                    expected = cached[0]
                else:
                    expected = on_disk
                    cached = (on_disk, ticker.now)
                assert service.status().connected is expected


ENV_NAMES = [
    "CLIENT_ID",
    "FYERS_APP_ID",
    "FYERS_CLIENT_ID",
    "SECRET_KEY",
    "FYERS_SECRET_KEY",
    "REDIRECT_URI",
]


@given(
    env=st.fixed_dictionaries(
        {
            name: st.one_of(
                st.none(), st.just(""), st.from_regex(f"{name}-[0-9a-z]{{5}}", fullmatch=True)
            )
            for name in ENV_NAMES
        }
    )
)
def test_credentials_from_env_precedence(env: dict[str, str | None]) -> None:
    """Each credential comes from the first non-empty name in its list (the legacy name first);
    when any is missing or empty the error names the primary variable and carries no value."""
    wanted = {
        "client_id": ["CLIENT_ID", "FYERS_APP_ID", "FYERS_CLIENT_ID"],
        "secret_key": ["SECRET_KEY", "FYERS_SECRET_KEY"],
        "redirect_uri": ["REDIRECT_URI"],
    }
    clean = {k: v for k, v in env.items() if v}
    with mock.patch.dict(os.environ, clean, clear=False):
        for name in ENV_NAMES:
            if name not in clean:
                os.environ.pop(name, None)
        expected: dict[str, str] = {}
        missing: str | None = None
        for field, names in wanted.items():
            first = next((clean[n] for n in names if n in clean), None)
            if first is None:
                missing = missing or names[0]
            else:
                expected[field] = first
        if missing is None:
            creds = credentials_from_env()
            assert (creds.client_id, creds.secret_key, creds.redirect_uri) == tuple(
                expected.values()
            )
            assert creds.secret_key not in repr(creds)
        else:
            with pytest.raises(MissingCredentialsError) as caught:
                credentials_from_env()
            assert missing in str(caught.value)
            assert all(v not in str(caught.value) for v in clean.values())


@given(code=codes)
def test_unset_credentials_fail_before_any_sdk_call(code: str) -> None:
    """With the credentials missing, neither the login link nor the exchange builds an SDK session
    or touches the token file."""
    with token_store() as store:
        sessions: list[Any] = []

        def factory(**kwargs: str) -> NoReturn:
            sessions.append(kwargs)
            raise AssertionError("no SDK session without credentials")

        def no_creds() -> FyersCredentials:
            raise MissingCredentialsError("CLIENT_ID not found")

        service = FyersAuthService(
            store,
            account_refs=frozenset,
            session_factory=factory,
            credentials=no_creds,
            on_token_change=lambda: None,
        )
        for call in (service.login_url, lambda: service.exchange(code)):
            with pytest.raises(MissingCredentialsError):
                call()
        assert sessions == []
        assert listing(store) == []


USERS = ("XA1234", "XB9999")
maybe_user = st.one_of(st.none(), st.sampled_from(USERS))


def _profile(user: str | None) -> dict[str, Any]:
    """The profile reply of a token for `user`; None: the recorded -16 refusal (no user id)."""
    return profile_ok(user) if user is not None else LIVE_REPLIES["profile_invalid_token"]


@given(
    refs=st.frozensets(st.sampled_from(USERS)),
    onboarded=maybe_user,
    user=maybe_user,
    good=st.booleans(),
    token=tokens,
    code=codes,
)
def test_accounts_read_in_order(
    refs: frozenset[str],
    onboarded: str | None,
    user: str | None,
    good: bool,
    token: str,
    code: str,
) -> None:
    """PBT fyers_auth.accounts_read_in_order (HAT-DEC-BD5; PR 73, PR 38 follow-ups 1 and 2): every
    exchange checks the token's Fyers user against the active Fyers broker accounts, which the
    service reads itself, once, after Fyers has answered and the profile was read and just before
    the save: an account onboarded while Fyers answers counts, a rejected code reads no account,
    and a refused user saves nothing. Harm: a login for another Fyers account plants its token
    (HAT then trades and reconciles against the wrong account), or the read moves before the
    exchange and holds a connection while Fyers answers."""
    trace: list[str] = []
    accounts = set(refs)

    def factory(**kwargs: str) -> RecordingSession:
        session = RecordingSession(token_reply(token) if good else {"s": "error"}, **kwargs)
        answer = session.generate_token

        def generate() -> Mapping[str, Any]:
            trace.append("exchange")
            return answer()

        session.generate_token = generate  # type: ignore[method-assign]
        return session

    class Profile:
        def get_profile(self) -> dict[str, Any]:
            trace.append("profile")
            if onboarded is not None:
                accounts.add(onboarded)  # onboarded while Fyers was answering
            return _profile(user)

    def account_refs() -> frozenset[str]:
        trace.append("accounts")
        return frozenset(accounts)

    final = refs | ({onboarded} if onboarded is not None else set())
    accepted = good and (not final or user in final)
    with token_store() as store:
        save = store.save

        def traced_save(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - TokenStore.save
            trace.append("save")
            return save(*args, **kwargs)

        store.save = traced_save  # type: ignore[method-assign]
        service = FyersAuthService(
            store,
            account_refs=account_refs,
            session_factory=factory,
            credentials=lambda: CREDS,
            on_token_change=lambda: None,
            model_factory=lambda **_kwargs: Profile(),
        )
        try:
            service.exchange(code)
            saved = True
        except WrongAccountError as error:
            assert good
            assert error.message == (ACCOUNT_UNCONFIRMED if user is None else WRONG_ACCOUNT)
            saved = False
        except TokenRejectedError:
            assert not good
            saved = False
        assert saved == accepted
        assert store.path.exists() == accepted
    read = ["exchange", "profile", "accounts"] if good else ["exchange"]
    assert trace == read + (["save"] if accepted else [])


@given(
    refs=st.frozensets(st.sampled_from(USERS)),
    user=maybe_user,
    down=st.booleans(),
    token=tokens,
    code=codes,
)
def test_complete_login_checks_the_account(
    refs: frozenset[str], user: str | None, down: bool, token: str, code: str
) -> None:
    """PBT fyers_auth.complete_login_checks_the_account (HAT-DEC-BD5; PR 73, PR 38 follow-up 1):
    the adapter's `complete_login` goes through the same check as the HTTP route. It saves the
    token exactly when no Fyers account is active or the user is one; another user is
    `wrong_account`, unreadable accounts are `accounts_unreadable`, and neither writes a token.
    Harm: a Telegram or job login saves a token for any Fyers user."""

    def account_refs() -> frozenset[str]:
        if down:
            raise OperationalError("SELECT account_ref", {}, RuntimeError("database down"))
        return refs

    with token_store() as store:
        service = FyersAuthService(
            store,
            account_refs=account_refs,
            session_factory=lambda **kwargs: RecordingSession(token_reply(token), **kwargs),
            credentials=lambda: CREDS,
            on_token_change=lambda: None,
            model_factory=lambda **_kwargs: RecordingSdk(_profile(user)),
        )
        broker = FyersHatBroker(
            FakeBroker(),  # type: ignore[arg-type]
            client=lambda: SimpleNamespace(fyers=None),  # type: ignore[arg-type,return-value]
            auth=lambda: service,
            instruments=list,
        )
        accepted = not down and (not refs or user in refs)
        if accepted:
            assert broker.complete_login(code) is SessionStatus.EXPIRED  # saved; no SDK client
        else:
            with pytest.raises(BrokerLoginError) as refused:
                broker.complete_login(code)
            assert refused.value.code == ("accounts_unreadable" if down else "wrong_account")
        assert store.path.exists() == accepted

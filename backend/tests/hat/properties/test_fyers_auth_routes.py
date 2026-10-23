"""PBT fyers_auth_routes.*: the four `/api/hat/auth/fyers`
routes answer with fixed messages for every failure, never echo the token, the secret, the auth
code or exception text, write no token on a failure, and report connected right after a good
exchange. The two POSTs refuse a request that did not come from the HAT page, and `token` refuses
a login for a Fyers user other than the active Fyers broker account. Mini app with only the HAT
router; the login service runs on a temporary token file and a recording SDK, and the broker
accounts live in SQLite in memory."""

import uuid
from collections.abc import Callable, Sequence
from typing import Any
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hat.fakes.fyers_sdk import RecordingSdk, RecordingSession
from hat_fyers_support import captured_logs, profile_ok, token_reply, token_store, write_token
from hat_support import memory_engine
from hypothesis import example, given
from hypothesis import strategies as st
from sqlalchemy.orm import sessionmaker

from app.hat.api import fyers_auth as routes
from app.hat.api import guard
from app.hat.execution.fyers_auth import (
    ACCOUNT_UNCONFIRMED,
    ACCOUNTS_UNREADABLE,
    WRONG_ACCOUNT,
    FyersAuthService,
    FyersCredentials,
    MissingCredentialsError,
    fyers_account_refs,
    get_fyers_auth,
)
from app.hat.execution.fyers_tokens import TokenStore
from app.hat.models import HatBrokerAccount

SECRET = "SECRETKEY-1a2b3c"
MARKER = "echoed-text-77ee"
codes = st.from_regex(r"AUTHCODE[0-9a-f]{10,24}", fullmatch=True)
tokens = st.from_regex(r"TKN[0-9a-f]{16,32}", fullmatch=True)

FIXED = {
    routes.MISSING_CREDENTIALS_ERROR,
    routes.URL_ERROR,
    routes.EXCHANGE_ERROR,
    routes.STORE_ERROR,
    routes.DISCONNECT_ERROR,
    routes.STATUS_ERROR,
    ACCOUNTS_UNREADABLE,
    "Fyers rejected the auth code. Please log in again.",
    "Fyers login failed. Please try again.",
    "Login was cancelled by a logout. Please log in again.",
    WRONG_ACCOUNT,
    ACCOUNT_UNCONFIRMED,
}
# The frontend origins of the test app's rule; every other origin is a page on another site.
FRONTEND = ("http://localhost:3000", "http://127.0.0.1:5173")
FOREIGN = (
    "https://evil.example",
    "http://localhost.evil.example",
    "null",
    "http://localhost:8000.evil",
)
WRITE = {guard.HEADER: guard.HEADER_VALUE}
USERS = ("XA1234", "XB5678", "XC9012")
# (broker, account_ref, status) of a broker account row
Account = tuple[str, str, str]
ACCOUNTS = st.lists(
    st.tuples(
        st.sampled_from(("fyers", "zerodha")),
        st.sampled_from(USERS),
        st.sampled_from(("active", "failed", "pending")),
    ),
    max_size=3,
)

REPLIES = st.one_of(
    st.just({"s": "error", "code": -413, "message": f"bad code {MARKER}"}),
    st.just({"s": "ok"}),
    st.just(None),
    st.just(f"plain {MARKER}"),
    st.just(RuntimeError(f"reset {MARKER}")),
    st.just(KeyError(MARKER)),
)


def _account_refs(accounts: Sequence[Account], down: bool = False) -> Callable[[], frozenset[str]]:
    """`active_fyers_refs` over the given broker accounts in SQLite; `down`: a database without
    the table, so every read raises (`OperationalError`)."""
    engine = memory_engine(HatBrokerAccount)
    if down:
        HatBrokerAccount.__table__.drop(engine)  # type: ignore[attr-defined]
        accounts = ()
    factory = sessionmaker(bind=engine)
    with factory() as session:
        for broker, ref, status in accounts:
            session.add(
                HatBrokerAccount(
                    user_id=uuid.uuid4(),
                    broker=broker,
                    account_ref=ref,
                    capabilities={},
                    status=status,
                )
            )
        session.commit()

    def refs() -> frozenset[str]:
        with factory() as session:
            return fyers_account_refs(session)

    return refs


def client_for(
    store: TokenStore,
    reply: object,
    creds: bool = True,
    *,
    profile: object = None,
    accounts: Sequence[Account] = (),
    wired: bool = True,
    db_down: bool = False,
    headers: dict[str, str] | None = None,
    hooks: list[str] | None = None,
) -> TestClient:
    """A client of the login routes. By default it sends the write header, the profile answers
    user XA1234, there is no broker account and the app has the test frontend-origin rule."""

    def factory(**kwargs: str) -> RecordingSession:
        return RecordingSession(reply, **kwargs)

    def credentials() -> FyersCredentials:
        if not creds:
            raise MissingCredentialsError(f"CLIENT_ID missing {MARKER}")
        return FyersCredentials("APP-100", SECRET, "https://app.example.test/cb")

    service = FyersAuthService(
        store,
        account_refs=_account_refs(accounts, db_down),
        session_factory=factory,
        credentials=credentials,
        on_token_change=lambda: None,
        model_factory=lambda **_kwargs: RecordingSdk(
            profile_ok("XA1234") if profile is None else profile
        ),
    )
    if hooks is not None:
        for event in ("token_change", "login", "disconnect"):
            service.add_hook(event, lambda event=event: hooks.append(event))  # type: ignore[misc]
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_fyers_auth] = lambda: service
    if wired:
        setattr(app.state, guard.ORIGIN_RULE, lambda origin: origin in FRONTEND)
    return TestClient(
        app, raise_server_exceptions=False, headers=WRITE if headers is None else headers
    )


@given(reply=REPLIES, code=codes)
def test_a_failed_token_exchange_is_a_fixed_message_and_writes_nothing(
    reply: object, code: str
) -> None:
    """For every failure of the Fyers side the route answers 400 or 500 with one of the fixed
    messages, the body and the log carry no code, secret or echoed text, and no token file
    exists."""
    with captured_logs() as logs, token_store() as store:
        client = client_for(store, reply)
        response = client.post(f"{routes.router.prefix}/token", json={"auth_code": code})
        assert response.status_code in {400, 500}
        assert response.json()["detail"] in FIXED
        assert not store.path.exists()
        assert client.get(f"{routes.router.prefix}/status").json()["connected"] is False
        shown = response.text + logs.text
    for secret in (code, SECRET, MARKER):
        assert secret not in shown


@given(code=codes)
def test_missing_credentials_are_a_fixed_500_on_both_routes(code: str) -> None:
    """Without app credentials the login link and the exchange both answer 500 with the fixed
    credentials message, never the variable text."""
    with captured_logs() as logs, token_store() as store:
        client = client_for(store, None, creds=False)
        for response in (
            client.get(f"{routes.router.prefix}/url"),
            client.post(f"{routes.router.prefix}/token", json={"auth_code": code}),
        ):
            assert response.status_code == 500
            assert response.json()["detail"] == routes.MISSING_CREDENTIALS_ERROR
            assert MARKER not in response.text + logs.text
            assert code not in response.text


@given(token=tokens, code=codes)
def test_a_good_exchange_is_connected_at_once_and_never_shows_the_token(
    token: str, code: str
) -> None:
    """After a good exchange `status` is connected with the user id, `disconnect` flips it back, and
    the token never appears in a response or a log line."""
    reply: dict[str, Any] = token_reply(token)  # the user id comes from the profile
    with captured_logs() as logs, token_store() as store:
        client = client_for(store, reply)
        base = routes.router.prefix
        assert client.get(f"{base}/status").json()["connected"] is False
        exchanged = client.post(f"{base}/token", json={"auth_code": code})
        status = client.get(f"{base}/status")
        assert exchanged.status_code == 200
        assert status.json() == {"connected": True, "user_id": "XA1234", "error": None}
        login_url = client.get(f"{base}/url")
        gone = client.post(f"{base}/disconnect")
        after = client.get(f"{base}/status")
        assert gone.status_code == 200
        assert after.json()["connected"] is False
        assert not store.path.exists()
        shown = "".join(r.text for r in (exchanged, status, login_url, gone, after)) + logs.text
    for secret in (token, SECRET, code):
        assert secret not in shown


@given(content=st.binary(min_size=1, max_size=40).filter(lambda b: b not in (b"{}",)))
def test_an_unreadable_token_file_is_a_status_error_not_a_crash(content: bytes) -> None:
    """A garbage token file reads as not connected, never a 500 and never its content."""
    with token_store() as store:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_bytes(content)
        client = client_for(store, None)
        response = client.get(f"{routes.router.prefix}/status")
        assert response.status_code == 200
        assert response.json()["connected"] is False


@given(token=tokens, code=codes)
def test_a_token_file_that_cannot_be_read_or_saved_is_a_fixed_error(token: str, code: str) -> None:
    """A token path that cannot be read gives `status` the fixed error text, and a save that fails
    gives the fixed store message; neither shows a path, the token or the code, and nothing is
    written."""
    reply = token_reply(token)
    with captured_logs() as logs, token_store() as store:
        client = client_for(store, reply)
        base = routes.router.prefix
        store.path.mkdir(parents=True)  # a directory where the file should be
        status = client.get(f"{base}/status")
        assert status.status_code == 200
        assert status.json() == {"connected": False, "user_id": None, "error": routes.STATUS_ERROR}
        store.path.rmdir()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            saved = client.post(f"{base}/token", json={"auth_code": code})
        assert saved.status_code == 500
        assert saved.json()["detail"] == routes.STORE_ERROR
        assert not store.path.exists()
        shown = status.text + saved.text + logs.text
    for secret in (token, code, str(store.path.parent)):
        assert secret not in status.text + saved.text
    assert token not in shown
    assert code not in shown


TOKEN = "TKN0123456789abcdef"
CODE = "AUTHCODE0123456789ab"
HEADER_VALUES = st.sampled_from((None, guard.HEADER_VALUE, "0", "true", ""))
ORIGINS = st.sampled_from((None, *FRONTEND, *FOREIGN))
FETCH_SITES = st.sampled_from((None, "same-origin", "same-site", "cross-site", "none"))


@given(
    header=HEADER_VALUES,
    origin=ORIGINS,
    fetch_site=FETCH_SITES,
    wired=st.booleans(),
    token=tokens,
    code=codes,
)
# the harmful shapes, each pinned: a cross-site or same-site request that names no origin, a page
# on another site that got the header past CORS, and the HAT page with no rule wired
@example(header="1", origin=None, fetch_site="cross-site", wired=True, token=TOKEN, code=CODE)
@example(header="1", origin=None, fetch_site="same-site", wired=True, token=TOKEN, code=CODE)
@example(header="1", origin=FOREIGN[0], fetch_site="cross-site", wired=True, token=TOKEN, code=CODE)
@example(
    header=None, origin=FRONTEND[0], fetch_site="same-origin", wired=True, token=TOKEN, code=CODE
)
@example(
    header="1", origin=FRONTEND[0], fetch_site="same-origin", wired=False, token=TOKEN, code=CODE
)
def test_cross_site_write_refused(
    header: str | None,
    origin: str | None,
    fetch_site: str | None,
    wired: bool,
    token: str,
    code: str,
) -> None:
    """For every combination of the write header, `Origin` and `Sec-Fetch-Site`, both POSTs run
    only when the header is `1` and the request names a frontend origin (with the rule wired), or
    names none and is not marked cross-site or same-site. A refused request is a 403 with the fixed
    message: no token is written or removed and no login hook runs. Harm: any page the owner's
    browser opens logs HAT out of Fyers mid-session or plants its own token."""
    sent = {
        name: value
        for name, value in (
            (guard.HEADER, header),
            ("Origin", origin),
            ("Sec-Fetch-Site", fetch_site),
        )
        if value is not None
    }
    from_page = header == guard.HEADER_VALUE and (
        (wired and origin in FRONTEND)
        if origin is not None
        else fetch_site in (None, "same-origin", "none")
    )
    base = routes.router.prefix
    with token_store() as store:
        hooks: list[str] = []
        client = client_for(store, token_reply(token), wired=wired, headers=sent, hooks=hooks)
        exchanged = client.post(f"{base}/token", json={"auth_code": code})
        if from_page:
            assert exchanged.status_code == 200
            assert store.path.exists()
        else:
            assert exchanged.status_code == 403
            assert exchanged.json()["detail"] == guard.WRITE_REFUSED
            assert not store.path.exists()
            assert hooks == []
            write_token(store)
        hooks.clear()
        gone = client.post(f"{base}/disconnect")
        if from_page:
            assert gone.status_code == 200
            assert not store.path.exists()
        else:
            assert gone.status_code == 403
            assert gone.json()["detail"] == guard.WRITE_REFUSED
            assert store.path.exists()
            assert hooks == []


@given(
    accounts=ACCOUNTS,
    user=st.one_of(st.none(), st.sampled_from(USERS)),
    db_down=st.booleans(),
    token=tokens,
    code=codes,
)
def test_token_for_another_account_refused(
    accounts: list[Account], user: str | None, db_down: bool, token: str, code: str
) -> None:
    """For every set of broker accounts and every Fyers user a login token belongs to (None: the
    profile could not be read), `token` saves the token exactly when there is no active Fyers
    account or the user is the `account_ref` of one. Otherwise it answers 403 with the fixed
    message (`ACCOUNT_UNCONFIRMED` for an unknown user, else `WRONG_ACCOUNT`) and writes nothing.
    When the accounts cannot be read it answers 503 with `ACCOUNTS_UNREADABLE` and writes
    nothing. Harm: a page or a wrong login plants a token for another Fyers account, and HAT
    trades or reconciles against it."""
    refs = {ref for broker, ref, status in accounts if broker == "fyers" and status == "active"}
    accepted = not refs or user in refs
    profile = profile_ok(user) if user is not None else {"s": "error", "code": -16}
    with captured_logs() as logs, token_store() as store:
        client = client_for(
            store, token_reply(token), profile=profile, accounts=accounts, db_down=db_down
        )
        exchanged = client.post(f"{routes.router.prefix}/token", json={"auth_code": code})
        if db_down:
            assert exchanged.status_code == 503
            assert exchanged.json()["detail"] == ACCOUNTS_UNREADABLE
            assert not store.path.exists()
        elif accepted:
            assert exchanged.status_code == 200
            assert store.path.exists()
        else:
            assert exchanged.status_code == 403
            assert exchanged.json()["detail"] == (
                ACCOUNT_UNCONFIRMED if user is None else WRONG_ACCOUNT
            )
            assert not store.path.exists()
        shown = exchanged.text + logs.text
    for secret in (token, code):
        assert secret not in shown

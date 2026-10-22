"""PBT fyers_auth_routes.* (HAT-OPS-051, K2): the four `/api/hat/auth/fyers` routes answer with
fixed messages for every failure, never echo the token, the secret, the auth code or exception
text, write no token on a failure, and report connected right after a good exchange. Mini app with
only the HAT router; the login service runs on a temporary token file and a recording SDK."""

from typing import Any
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hat.fakes.fyers_sdk import RecordingSdk, RecordingSession
from hat_fyers_support import captured_logs, profile_ok, token_reply, token_store
from hypothesis import given
from hypothesis import strategies as st

from app.hat.api import fyers_auth as routes
from app.hat.execution.fyers_auth import (
    FyersAuthService,
    FyersCredentials,
    MissingCredentialsError,
    get_fyers_auth,
)
from app.hat.execution.fyers_tokens import TokenStore

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
    "Fyers rejected the auth code. Please log in again.",
    "Fyers login failed. Please try again.",
    "Login was cancelled by a logout. Please log in again.",
}

REPLIES = st.one_of(
    st.just({"s": "error", "code": -413, "message": f"bad code {MARKER}"}),
    st.just({"s": "ok"}),
    st.just(None),
    st.just(f"plain {MARKER}"),
    st.just(RuntimeError(f"reset {MARKER}")),
    st.just(KeyError(MARKER)),
)


def client_for(store: TokenStore, reply: object, creds: bool = True) -> TestClient:
    def factory(**kwargs: str) -> RecordingSession:
        return RecordingSession(reply, **kwargs)

    def credentials() -> FyersCredentials:
        if not creds:
            raise MissingCredentialsError(f"CLIENT_ID missing {MARKER}")
        return FyersCredentials("APP-100", SECRET, "https://app.example.test/cb")

    service = FyersAuthService(
        store,
        session_factory=factory,
        credentials=credentials,
        on_token_change=lambda: None,
        model_factory=lambda **_kwargs: RecordingSdk(profile_ok("XA1234")),
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_fyers_auth] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


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

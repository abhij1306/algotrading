"""PBT fyers_auth.*: the login flow trades an auth code for a token through one
store, fails closed on anything Fyers or the disk gets wrong, never logs or raises the token, the
secret or the code, lets a logout win over a login in flight, and keeps its status cache honest.
Runs on a temporary token file and a recording SDK session."""

import os
from unittest import mock

import pytest
from hat_fyers_support import (
    token_reply,
    token_store,
)
from hypothesis import given
from hypothesis import strategies as st

from app.hat.execution.fyers_auth import (
    FyersAuthService,
    FyersCredentials,
    MissingCredentialsError,
    credentials_from_env,
)
from app.hat.execution.fyers_tokens import TokenStore

SECRET = "SECRETKEY-9f8e7d6c"
CREDS = FyersCredentials("APP-100", SECRET, "https://app.example.test/cb")
codes = st.from_regex(r"AUTHCODE[0-9a-f]{10,24}", fullmatch=True)
tokens = st.from_regex(r"TKN[0-9a-f]{16,32}", fullmatch=True)
GOOD = st.builds(token_reply, tokens)


def listing(store: TokenStore) -> list[str]:
    return sorted(p.name for p in store.path.parent.iterdir()) if store.path.parent.exists() else []


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


def test_a_refused_token_reads_not_connected_until_a_new_login() -> None:
    """C22 A5: once Fyers refuses the saved token (-16 on a profile read) the login status reads
    not connected though the file is in date, until a login saves another token; a refusal read
    for an earlier token (a check in flight across a login) changes nothing."""
    with token_store() as store:
        service = FyersAuthService(store, account_refs=frozenset, on_token_change=lambda: None)
        service._save_login(token_reply("TKN0123456789abcdef"), CREDS.client_id, 0)
        assert service.status().connected
        refused = service.token_generation()
        service.refuse_token(refused)
        assert service.token_refused()
        assert not service.status().connected
        service._save_login(token_reply("TKNfedcba9876543210"), CREDS.client_id, 0)
        assert not service.token_refused()
        assert service.status().connected
        service.refuse_token(refused)  # the old token's refusal arrives late
        assert service.status().connected

"""PBT fyers_tokens.*: the Fyers token file is read and written by one store,
expires at the next IST midnight, fails closed on anything it cannot trust, is written atomically
and never shows the token. Property tests on the real `TokenStore` over a temporary directory."""

import json
import os
import stat
from datetime import UTC, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from hat_fyers_support import captured_logs, token_store
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data.sessions import IST
from app.hat.execution.fyers_tokens import StoredToken, TokenStoreError, token_expiry

ZONES = [UTC, IST, ZoneInfo("America/New_York"), ZoneInfo("Pacific/Auckland")]
instants = st.datetimes(
    min_value=datetime(2001, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.sampled_from(ZONES)
)
token_texts = st.from_regex(r"TKN[0-9a-f]{12,30}", fullmatch=True)
client_ids = st.from_regex(r"[A-Z0-9]{4,10}-100", fullmatch=True)


@given(now=instants)
def test_expiry_is_the_next_ist_midnight(now: datetime) -> None:
    """For every instant, the token expires at the first IST midnight after it: strictly later,
    at most a day later, and exactly at 00:00 IST. A naive time is refused."""
    expiry = token_expiry(now)
    assert expiry > now
    assert expiry - now <= timedelta(days=1)
    assert expiry.astimezone(IST).time() == time(0)
    assert expiry - timedelta(days=1) <= now  # no earlier midnight was skipped
    naive = now.replace(tzinfo=None)
    with pytest.raises(ValueError):
        token_expiry(naive)


@given(
    token=token_texts,
    client=client_ids,
    saved_at=instants,
    offset=st.integers(-3 * 86400, 3 * 86400),
)
def test_save_load_roundtrip_and_usable(
    token: str, client: str, saved_at: datetime, offset: int
) -> None:
    """A saved token reads back with its client id, user id and the IST-midnight expiry, and is
    usable exactly until that expiry (never after, never without a client id)."""
    with token_store() as store:
        saved = store.save(
            {"s": "ok", "access_token": token, "fy_id": "XA1234"}, client_id=client, now=saved_at
        )
        loaded = store.load()
    assert loaded == saved
    assert loaded is not None
    assert (loaded.access_token, loaded.client_id, loaded.user_id) == (token, client, "XA1234")
    assert loaded.expires_at == token_expiry(saved_at).astimezone(UTC)
    probe = loaded.expires_at + timedelta(seconds=offset)
    assert loaded.is_usable(probe) == (offset <= 0)
    assert StoredToken(token, None, None, loaded.expires_at).is_usable(loaded.expires_at) is False


EXPIRIES = st.one_of(
    st.just(None),
    st.just(""),
    st.just("not a date"),
    st.just("9999-12-31T23:59:59-23:59"),  # overflows on conversion to UTC
    st.just("0001-01-01T00:00:00+23:59"),
    st.integers(),
    st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2099, 1, 1)).map(
        lambda d: d.isoformat()
    ),
)
FILES = st.one_of(
    st.binary(max_size=60),
    st.dictionaries(
        st.sampled_from(["access_token", "client_id", "fy_id", "expires_at"]),
        st.one_of(st.none(), st.integers(), st.text(max_size=20), EXPIRIES),
        max_size=4,
    ).map(lambda d: json.dumps(d).encode()),
)


@given(content=FILES, now=instants)
def test_unreadable_or_untrusted_files_fail_closed(content: bytes, now: datetime) -> None:
    """Whatever the file holds, `load` never raises and never invents a token: it returns None or
    a token with a non-empty access token, and the token is usable only with a client id and a
    parseable expiry that is still ahead."""
    with token_store() as store:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_bytes(content)
        token = store.load()
    if token is None:
        return
    assert isinstance(token.access_token, str)
    assert token.access_token
    if token.is_usable(now):
        assert token.client_id
        assert token.expires_at is not None
        assert now <= token.expires_at
    if token.expires_at is None:
        assert not token.is_usable(now)


@settings(max_examples=60)
@given(
    old=token_texts,
    new=token_texts,
    fail_at=st.sampled_from(["fsync", "replace", "write"]),
    first=st.booleans(),
)
def test_failed_write_leaves_the_old_file_and_no_partial_file(
    old: str, new: str, fail_at: str, first: bool
) -> None:
    """A write that fails at any step leaves the previous token file byte-for-byte (or no file when
    there was none), no temporary file, and raises `TokenStoreError`; a write that succeeds is
    owner-only on POSIX and replaces the whole file."""
    with token_store() as store:
        if not first:
            store.save({"access_token": old}, client_id="C-100")
        store.path.parent.mkdir(parents=True, exist_ok=True)
        before = store.path.read_bytes() if store.path.exists() else None
        target = {"fsync": "os.fsync", "replace": "os.replace", "write": "json.dumps"}[fail_at]
        with mock.patch(target, side_effect=OSError("disk full")):
            with pytest.raises(TokenStoreError) as caught:
                store.save({"access_token": new}, client_id="C-100")
        assert "disk full" not in str(caught.value)
        after = store.path.read_bytes() if store.path.exists() else None
        assert after == before
        assert [p.name for p in store.path.parent.iterdir()] == (
            [] if before is None else [store.path.name]
        )

        store.save({"access_token": new}, client_id="C-100")
        assert store.load().access_token == new  # type: ignore[union-attr]
        assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]
        if os.name == "posix":
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


@given(token=token_texts, secret=token_texts)
def test_the_token_is_never_logged_or_shown(token: str, secret: str) -> None:
    """Saving, loading, clearing, a corrupt file and a failed write never put the token in a log
    line, a `repr` or an error message."""
    with captured_logs() as logs, token_store() as store:
        saved = store.save(
            {"s": "ok", "access_token": token, "refresh_token": secret}, client_id="C-1"
        )
        store.load()
        shown = [repr(saved), repr(store.load()), str(saved.expires_at)]
        store.path.write_text(f'{{"access_token": "{token}", ', encoding="utf-8")  # truncated JSON
        store.load()
        with mock.patch("os.replace", side_effect=OSError(token)):
            with pytest.raises(TokenStoreError) as caught:
                store.save({"access_token": token}, client_id="C-1")
        shown.append(str(caught.value))
        store.clear()
    for text in [logs.text, *shown]:
        assert token not in text
        assert secret not in text

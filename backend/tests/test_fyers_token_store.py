"""TokenStore: the token file round-trips, writes atomically and expires at IST midnight."""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import config as config_module
from app.services import fyers_client as fyers_client_module
from app.services.fyers import token_store as token_store_module
from app.services.fyers.token_store import StoredToken, TokenStore, TokenStoreError, token_expiry
from app.services.fyers_client import FyersClient

REPLY = {"s": "ok", "access_token": "tok-secret-abc", "fy_id": "XA1234", "refresh_token": "r"}


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "config" / "access_token.json")


def test_save_then_load_round_trips(store: TokenStore) -> None:
    now = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)  # 14:30 IST

    store.save(REPLY, client_id="CLIENT-100", now=now)

    assert store.load() == StoredToken(
        access_token="tok-secret-abc",
        client_id="CLIENT-100",
        user_id="XA1234",
        expires_at=datetime(2026, 3, 10, 18, 30, tzinfo=UTC),  # 00:00 IST on the 11th
    )
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk["refresh_token"] == "r"
    assert on_disk["expires_at"] == "2026-03-10T18:30:00+00:00"
    assert on_disk["expires_at_ist"] == "2026-03-11T00:00:00+05:30"


def test_save_replaces_an_existing_token(store: TokenStore) -> None:
    store.save(REPLY, client_id="CLIENT-100")
    store.save({**REPLY, "access_token": "tok-new"}, client_id="CLIENT-100")

    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "tok-new"


def test_failed_write_leaves_old_file_and_no_partial_file(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(REPLY, client_id="CLIENT-100")
    before = store.path.read_bytes()

    def failing_replace(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_module.os, "replace", failing_replace)

    with pytest.raises(TokenStoreError):
        store.save({**REPLY, "access_token": "tok-new"}, client_id="CLIENT-100")

    assert store.path.read_bytes() == before
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["access_token.json"]


def test_failed_first_write_leaves_no_file(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_replace(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_module.os, "replace", failing_replace)

    with pytest.raises(TokenStoreError):
        store.save(REPLY, client_id="CLIENT-100")

    assert list(store.path.parent.iterdir()) == []
    assert store.load() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes; Windows ACLs are not changed")
def test_saved_file_is_owner_only_on_posix(store: TokenStore) -> None:
    store.save(REPLY, client_id="CLIENT-100")

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="fchmod runs on POSIX only")
def test_failed_chmod_leaves_no_partial_file(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_fchmod(_fd: int, _mode: int) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(token_store_module.os, "fchmod", failing_fchmod)

    with pytest.raises(TokenStoreError):
        store.save(REPLY, client_id="CLIENT-100")

    assert list(store.path.parent.iterdir()) == []


def test_save_without_access_token_is_refused(store: TokenStore) -> None:
    with pytest.raises(ValueError):
        store.save({"s": "ok"}, client_id="CLIENT-100")

    assert not store.path.exists()


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 23:59 IST on the 10th -> midnight starting the 11th
        (datetime(2026, 3, 10, 18, 29, tzinfo=UTC), datetime(2026, 3, 10, 18, 30, tzinfo=UTC)),
        # 00:00 IST exactly on the 11th -> midnight starting the 12th
        (datetime(2026, 3, 10, 18, 30, tzinfo=UTC), datetime(2026, 3, 11, 18, 30, tzinfo=UTC)),
        # 00:01 IST on the 11th, still the 10th in UTC -> midnight starting the 12th
        (datetime(2026, 3, 10, 18, 31, tzinfo=UTC), datetime(2026, 3, 11, 18, 30, tzinfo=UTC)),
        # 05:00 IST on Jan 1, still Dec 31 in UTC -> midnight starting Jan 2
        (datetime(2025, 12, 31, 23, 30, tzinfo=UTC), datetime(2026, 1, 1, 18, 30, tzinfo=UTC)),
    ],
)
def test_expiry_is_the_next_ist_midnight(now: datetime, expected: datetime) -> None:
    expiry = token_expiry(now)

    assert expiry == expected
    assert expiry.utcoffset() is not None
    assert expiry.isoformat().endswith("T00:00:00+05:30")


def test_expiry_needs_an_aware_time() -> None:
    with pytest.raises(ValueError):
        token_expiry(datetime(2026, 3, 10, 12, 0))


def test_is_expired_compares_against_utc_now() -> None:
    token = StoredToken("t", "c", "u", datetime(2026, 3, 10, 18, 30, tzinfo=UTC))

    assert token.is_expired(datetime(2026, 3, 10, 18, 29, tzinfo=UTC)) is False
    assert token.is_expired(datetime(2026, 3, 10, 18, 31, tzinfo=UTC)) is True
    assert StoredToken("t", "c", "u", None).is_expired() is True  # no expiry: fail closed


def test_usable_needs_a_client_id_and_a_live_expiry() -> None:
    before = datetime(2026, 3, 10, 18, 0, tzinfo=UTC)
    expiry = datetime(2026, 3, 10, 18, 30, tzinfo=UTC)

    assert StoredToken("t", "c", "u", expiry).is_usable(before) is True
    assert StoredToken("t", None, "u", expiry).is_usable(before) is False
    assert StoredToken("t", "c", "u", None).is_usable(before) is False
    assert StoredToken("t", "c", "u", expiry).is_usable(expiry.replace(minute=31)) is False


@pytest.mark.parametrize(
    "document",
    [
        {"access_token": "t", "client_id": "c"},  # no expires_at
        {"access_token": "t", "client_id": "c", "expires_at": "soon"},  # unparseable
        # parses, but overflows when converted to UTC
        {"access_token": "t", "client_id": "c", "expires_at": "0001-01-01T00:00:00+05:30"},
        {"access_token": "t", "client_id": "c", "expires_at": "9999-12-31T23:59:59-05:00"},
        {"access_token": "t", "expires_at": "2999-01-01T00:00:00+00:00"},  # no client_id
    ],
)
def test_unusable_token_file_is_unavailable(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch, document: dict[str, str]
) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(config_module, "token_store", store)
    client = _isolated_client(store, monkeypatch)

    client._load_credentials()

    assert (client.client_id, client.access_token, client.fyers) == (None, None, None)
    assert config_module.fyers_available() is False


def test_missing_file_is_no_token(store: TokenStore) -> None:
    assert store.load() is None
    assert store.mtime_ns() is None


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '{"client_id": "c"}', '{"access_token": ""}', ""],
)
def test_corrupt_file_is_no_token(store: TokenStore, content: str) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")

    assert store.load() is None


def test_malformed_expiry_reads_as_expired(store: TokenStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"access_token": "t", "client_id": "c", "expires_at": "tomorrow"}),
        encoding="utf-8",
    )

    token = store.load()

    assert token is not None
    assert token.is_expired() is True


def test_naive_expiry_is_read_as_utc(store: TokenStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"access_token": "t", "expires_at": "2026-03-10T18:30:00"}), encoding="utf-8"
    )

    token = store.load()

    assert token is not None
    assert token.expires_at == datetime(2026, 3, 10, 18, 30, tzinfo=UTC)


def test_clear_removes_the_file(store: TokenStore) -> None:
    store.save(REPLY, client_id="CLIENT-100")

    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is False


def test_token_text_is_not_in_repr(store: TokenStore) -> None:
    saved = store.save(REPLY, client_id="CLIENT-100")

    assert "tok-secret-abc" not in repr(saved)


def test_fyers_client_reads_credentials_through_the_store(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _isolated_client(store, monkeypatch)

    assert client._refresh_credentials_if_changed() is False  # no file yet

    store.save(REPLY, client_id="CLIENT-100")
    assert client._refresh_credentials_if_changed() is True
    assert (client.client_id, client.access_token) == ("CLIENT-100", "tok-secret-abc")
    assert client.token_expires_at is not None
    assert client.is_token_expired() is False


def test_client_never_installs_an_expired_token(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _isolated_client(store, monkeypatch)
    store.save(REPLY, client_id="CLIENT-100")
    client._load_credentials()
    client.fyers = "sdk-model-with-old-token"

    store.save(REPLY, client_id="CLIENT-100", now=datetime(2020, 3, 10, 9, 0, tzinfo=UTC))
    os.utime(store.path, ns=(1, 1))  # a distinct mtime, however fast the two saves were
    with caplog.at_level("WARNING"):
        assert client._refresh_credentials_if_changed() is False
        assert client._refresh_credentials_if_changed() is False

    assert (client.client_id, client.access_token, client.fyers) == (None, None, None)
    assert caplog.text.count("Fyers token expired") == 1  # warned once, not on every check


def _isolated_client(store: TokenStore, monkeypatch: pytest.MonkeyPatch) -> FyersClient:
    """A FyersClient over ``store`` that is not the process singleton and builds no SDK model."""

    class IsolatedClient(fyers_client_module.FyersClient):
        _instance = None

        def __init__(self) -> None:  # no SDK, no default token file
            self.fyers = None
            self.client_id = None
            self.access_token = None
            self.token_expires_at = None
            self._token_file_mtime_ns = None
            self._expired_token_mtime_ns = None
            self._missing_token_file_logged = False
            self._missing_credentials_logged = False

    monkeypatch.setattr(fyers_client_module, "token_store", store)
    return IsolatedClient()


def test_undecodable_file_is_no_token(store: TokenStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b'{"access_token": "\xff\xfe"}')

    assert store.load() is None


def test_uninspectable_file_raises_store_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "access_token.json")
    real_stat = Path.stat

    def denied(self: Path, **kwargs: bool) -> os.stat_result:
        if self == store.path:
            raise PermissionError("denied")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(TokenStoreError):
        store.mtime_ns()


def test_client_fails_closed_when_the_token_file_cannot_be_inspected(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _isolated_client(store, monkeypatch)
    store.save(REPLY, client_id="CLIENT-100")
    client._load_credentials()
    client.fyers = "sdk-model-with-old-token"

    def denied() -> int:
        raise TokenStoreError("Fyers token file could not be inspected")

    monkeypatch.setattr(store, "mtime_ns", denied)

    client._load_credentials()  # must not raise
    assert (client.access_token, client.fyers) == (None, None)


def test_client_drops_the_old_token_after_the_file_is_removed(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _isolated_client(store, monkeypatch)
    store.save(REPLY, client_id="CLIENT-100")
    assert client._refresh_credentials_if_changed() is True
    client.fyers = "sdk-model-with-old-token"

    store.clear()  # logout

    assert client._refresh_credentials_if_changed() is False
    assert (client.client_id, client.access_token, client.fyers) == (None, None, None)


def test_client_drops_the_old_token_when_the_file_turns_corrupt(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _isolated_client(store, monkeypatch)
    store.save(REPLY, client_id="CLIENT-100")
    client._load_credentials()
    client.fyers = "sdk-model-with-old-token"

    store.path.write_bytes(b"\xff\xfe not json")
    client._load_credentials()

    assert (client.access_token, client.fyers) == (None, None)


def test_fyers_available_reads_the_store_at_call_time(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "token_store", store)
    monkeypatch.delenv("FYERS_CLIENT_ID", raising=False)
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    assert config_module.fyers_available() is False

    store.save(REPLY, client_id="CLIENT-100")  # login after import
    assert config_module.fyers_available() is True

    store.save(REPLY, client_id="CLIENT-100", now=datetime(2020, 1, 1, tzinfo=UTC))
    assert config_module.fyers_available() is False  # expired

    store.path.write_bytes(b"\xff\xfe")
    assert config_module.fyers_available() is False  # undecodable


def test_fyers_available_ignores_env_credentials_the_client_cannot_use(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "token_store", store)
    monkeypatch.setenv("FYERS_CLIENT_ID", "env-client")
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "env-token")

    assert config_module.fyers_available() is False


def test_fyers_available_is_false_when_the_token_file_is_unreadable(
    store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied() -> None:
        raise TokenStoreError("Fyers token file could not be read")

    monkeypatch.setattr(store, "load", denied)
    monkeypatch.setattr(config_module, "token_store", store)

    assert config_module.fyers_available() is False

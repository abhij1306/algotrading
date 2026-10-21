"""Database URL building and boolean env parsing in app.database."""

import pytest
from sqlalchemy.engine import make_url

from app import database
from app.utils.env_loader import env_flag

PASSWORD = "p@ss:w/rd%41"


@pytest.fixture
def db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "db.example")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_NAME", "trading")
    monkeypatch.setenv("DB_USER", "trader")
    monkeypatch.setenv("DB_PASSWORD", PASSWORD)


@pytest.mark.usefixtures("db_env")
def test_password_with_url_special_characters_reaches_the_driver_intact() -> None:
    engine = database.create_engine_from_env()
    try:
        _, connect_kwargs = engine.dialect.create_connect_args(engine.url)
    finally:
        engine.dispose()

    assert engine.url.drivername == "postgresql+psycopg2"
    assert {k: connect_kwargs[k] for k in ("host", "port", "dbname", "user", "password")} == {
        "host": "db.example",
        "port": 6543,
        "dbname": "trading",
        "user": "trader",
        "password": PASSWORD,
    }


@pytest.mark.usefixtures("db_env")
def test_rendered_url_round_trips_the_password() -> None:
    url = database.postgres_url_from_env()

    rendered = url.render_as_string(hide_password=False)
    parsed = make_url(rendered)

    assert (parsed.host, parsed.port, parsed.database, parsed.password) == (
        "db.example",
        6543,
        "trading",
        PASSWORD,
    )


@pytest.mark.usefixtures("db_env")
def test_database_target_description_has_no_password() -> None:
    url = database.postgres_url_from_env()

    assert database.describe_database_target(url) == "db.example:6543/trading"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("Yes", True),
        ("on", True),
        ("ON", True),
        (" true ", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        ("enabled", False),
    ],
)
def test_env_flag_values(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("PLAT_TEST_FLAG", raw)

    assert env_flag("PLAT_TEST_FLAG") is expected


@pytest.mark.parametrize("raw", [None, "", "   "])
@pytest.mark.parametrize("default", [True, False])
def test_env_flag_unset_or_blank_gives_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, default: bool
) -> None:
    if raw is None:
        monkeypatch.delenv("PLAT_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("PLAT_TEST_FLAG", raw)

    assert env_flag("PLAT_TEST_FLAG", default=default) is default

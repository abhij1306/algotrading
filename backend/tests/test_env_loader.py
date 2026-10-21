"""Characterization tests for the .env loader."""

import os
from pathlib import Path

import pytest

from app.utils.env_loader import load_dotenv


def test_load_dotenv_parses_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "test.env"
    env.write_text(
        "\n".join(
            [
                "# comment",
                "",
                "CHAR_PLAIN = value one ",
                'CHAR_DQ="double quoted"',
                "CHAR_SQ='single quoted'",
                "export CHAR_EXPORTED=yes",
                "CHAR_MIXED=\"mismatch'",
                'CHAR_LONE="',
                "CHAR_EQ=a=b",
                "=no key",
                "no equals sign",
                "CHAR_KEEP=from file",
            ]
        ),
        encoding="utf-8",
    )
    names = [
        "CHAR_PLAIN",
        "CHAR_DQ",
        "CHAR_SQ",
        "CHAR_EXPORTED",
        "CHAR_MIXED",
        "CHAR_LONE",
        "CHAR_EQ",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHAR_KEEP", "from env")

    load_dotenv(str(env))

    assert {name: os.environ[name] for name in names} == {
        "CHAR_PLAIN": "value one",
        "CHAR_DQ": "double quoted",
        "CHAR_SQ": "single quoted",
        "CHAR_EXPORTED": "yes",
        "CHAR_MIXED": "\"mismatch'",
        "CHAR_LONE": "",
        "CHAR_EQ": "a=b",
    }
    assert os.environ["CHAR_KEEP"] == "from env"


def test_load_dotenv_falls_back_to_cwd_and_ignores_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "char_cwd.env").write_text("CHAR_FROM_CWD=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHAR_FROM_CWD", raising=False)

    load_dotenv("char_cwd.env")
    load_dotenv("definitely_missing_char.env")

    assert os.environ["CHAR_FROM_CWD"] == "1"

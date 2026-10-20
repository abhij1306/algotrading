"""App-wide guards: every app module imports, and app.* loggers reach the console."""

import importlib
import io
import logging
import pkgutil

import pytest

import app
from app.utils.logger import setup_logging


def _app_modules() -> list[str]:
    return sorted(
        info.name for info in pkgutil.walk_packages(app.__path__, prefix=f"{app.__name__}.")
    )


@pytest.mark.parametrize("module_name", _app_modules())
def test_every_app_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_app_module_info_logs_reach_the_console_handler() -> None:
    from app.routers import auth

    base = setup_logging()
    console = next(
        h
        for h in base.handlers
        if type(h) is logging.StreamHandler  # the console handler, not the rotating file one
    )
    buffer = io.StringIO()
    original_stream = console.setStream(buffer)
    try:
        auth.logger.info("[Fyers Auth] Access token saved to %s", "token.json")
    finally:
        console.setStream(original_stream)

    assert " - app.routers.auth - INFO - [Fyers Auth] Access token saved to token.json" in (
        buffer.getvalue()
    )


def test_safe_log_value_strips_line_breaks_and_control_characters() -> None:
    from app.utils.logger import safe_log_value

    assert safe_log_value("NIFTY\r\nINFO forged\x1b[31m") == "NIFTY??INFO forged?[31m"


def test_safe_log_value_truncates_long_input() -> None:
    from app.utils.logger import safe_log_value

    assert safe_log_value("A" * 500) == "A" * 200

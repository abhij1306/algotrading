"""PBT minute_history_cli.root_and_report: the minute-history
command line, driven through `main` with only its boundaries replaced (the Fyers history call,
the database session and the universe query).

Unlike the loader's properties in test_bars.py, `main` reads the wall clock (the period ends
yesterday, the report file is named by the day), so these checks match the report by pattern
and assert nothing that depends on the date."""

import io
import tempfile
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from hat_support import ROOT_SOURCES, no_network, operator_env
from hypothesis import given, settings
from hypothesis import strategies as st

from app.hat.data import minute_history
from app.hat.data.bars_basis import RawBasis


@given(
    source=st.sampled_from(ROOT_SOURCES),
    count=st.integers(0, 3),
    refused=st.booleans(),
    unmapped=st.integers(0, 2),
)
@settings(max_examples=25, deadline=None)
def test_root_and_report(source: str, count: int, refused: bool, unmapped: int) -> None:
    """The command line resolves the data root after `.env` is loaded (a root set only in
    `.env` counts, one in the environment wins, a blank one means the default), keeps its lock
    and progress under it, and appends the coverage report it prints to
    `minute_history/report-YYYY-MM-DD.txt` there; it exits 1 exactly when the run stopped or a
    symbol failed (the budget never runs out here, so only refused symbols do either)."""
    symbols = {f"NSE:S{i}-EQ": uuid4() for i in range(count)}

    def history(request: dict[str, str]) -> dict[str, Any]:
        if refused:
            return {"s": "error", "code": -16, "message": "token expired for client XY1234"}
        return {"s": "no_data", "candles": []}

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        with (
            operator_env(scratch, source) as root,
            patch("app.hat.data.provider.fyers_websocket.fyers_history", return_value=history),
            patch("app.database.db_session_scope", return_value=nullcontext()),
            patch.object(minute_history, "history_universe", return_value=(symbols, unmapped)),
            patch.object(
                minute_history, "_basis", return_value=RawBasis([], lambda *_: {}, Decimal(0))
            ),
            patch("logging.basicConfig"),  # main's own logging setup must not leak into the run
            patch("sys.stdout", new_callable=io.StringIO) as out,
            no_network() as reached,
        ):
            code = minute_history.main(["--days", "3"])
        assert reached == []
        assert code == (1 if refused and symbols else 0)
        written = sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*.*"))
        assert {p.name for p in scratch.iterdir()} == {".env", root.name}, written
        folder = root / "minute_history"
        assert (folder / "progress.json").is_file(), written
        saved = list(folder.glob("report-*.txt"))
        assert len(saved) == 1, written
        assert saved[0].read_text(encoding="utf-8").endswith(out.getvalue())
        assert f"active members without a Fyers symbol: {unmapped}" in out.getvalue()

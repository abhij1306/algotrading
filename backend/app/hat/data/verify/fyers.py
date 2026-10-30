"""python -m app.hat.data.verify.fyers: download D2's second-source sample (`adjustments`'
`second_source_sample`, each stock as its chain's current instrument) from Fyers' daily history
into `verify_reference/fyers/`, one file per stock and calendar year of `SECOND_SOURCE_YEARS`
(and the December before, for the first year's base), for a verify run to read.

It uses HAT's Fyers session as the app's login left it (`fyers_history`): it never logs in or
refreshes the token, and stops (exit 2) without a valid one. The token is shared with the
data downloads and live trading, so it asks at most `PER_MINUTE` times a minute. Every answer
with candles, `no_data` or "invalid symbol" is kept once (`store.keep_once`), so a rerun asks
only what is not kept; any other answer stops the run with its code (never the broker's
text), to be rerun later. A stock with no Fyers symbol (delisted: Fyers lists only what trades
now) is not asked. The database is read once, for the sample, and the session closed before
the first request.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..pacing import Pacer
from .adjustments import SECOND_SOURCE_YEARS, fyers_answered, fyers_request

PER_MINUTE = 10  # history calls a minute: low, the token is shared
logger = logging.getLogger(__name__)
HistoryCall = Callable[[dict[str, str]], Mapping[str, Any]]


def requests_for(symbols: Sequence[str]) -> list[tuple[str, dict[str, str]]]:
    """Every (file name, request) of `symbols` (Fyers symbols) for the compared years."""
    first, last = SECOND_SOURCE_YEARS
    return [
        fyers_request(s, y, base=y < first) for s in symbols for y in range(first - 1, last + 1)
    ]


def fetch_all(
    todo: Sequence[tuple[str, dict[str, str]]],
    history: HistoryCall,
    keep: Callable[[str, bytes], None],
    pacer: Pacer,
) -> tuple[int, str | None]:  # pragma: no cover - the network loop; its rules are above
    """Each request asked in turn and its answer kept: how many were kept, and why it stopped
    early (None when every one was)."""
    kept = 0
    for name, request in todo:
        pacer.wait()
        try:
            answer = history(request)
        except Exception:  # noqa: BLE001 - the SDK's text never leaves this module
            return kept, "fyers_failed"
        if not fyers_answered(answer):
            return kept, f"fyers_refused_{answer.get('code')}"
        keep(name, json.dumps(answer, separators=(",", ":")).encode())
        kept += 1
        logger.info("fyers %s kept (%d of %d)", name, kept, len(todo))
    return kept, None


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - database and network
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ...models import HatMarket  # noqa: PLC0415
    from ...settings import operator_settings  # noqa: PLC0415
    from ..chains import chains, stored_links  # noqa: PLC0415
    from ..provider.fyers_websocket import fyers_history  # noqa: PLC0415
    from ..store import DATASET_BARS, DataStore, keep_once  # noqa: PLC0415
    from . import FIRST_DAY  # noqa: PLC0415
    from .adjustments import fyers_sample, nifty500_members, second_source_sample  # noqa: PLC0415
    from .context import Context  # noqa: PLC0415

    del argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = operator_settings()
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    days = store.days(DATASET_BARS)
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatMarket})
        ctx = Context(store, session, FIRST_DAY, days[-1])
        symbols = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
        broker = dict(
            ctx.rows(
                "SELECT CAST(instrument_id AS TEXT), broker_symbol FROM hat_broker_symbol "
                "WHERE broker = 'fyers'"
            )
        )
        heads = chains(stored_links(session))
        sample = fyers_sample(
            second_source_sample(symbols, nifty500_members(ctx), heads),
            symbols,
            heads,
            broker,
        )
    folder = ctx.reference_dir("fyers")
    listed = sorted({fyers for _, _, fyers in sample if fyers})
    todo = [(n, r) for n, r in requests_for(listed) if not (folder / n).is_file()]
    logger.info(
        "fyers: %d of %d stocks listed, %d requests to ask", len(listed), len(sample), len(todo)
    )
    if not todo:
        return 0
    history = fyers_history()
    if history is None:
        sys.stderr.write("fyers_no_session: log in to Fyers from the app first\n")
        return 2
    kept, stopped = fetch_all(
        todo,
        history,
        lambda name, data: keep_once(folder / name, data),
        Pacer(min_interval=60 / PER_MINUTE),  # evenly spaced, never a burst
    )
    sys.stdout.write(
        f"fyers: {kept} of {len(todo)} kept{f', stopped: {stopped}' if stopped else ''}\n"
    )
    return 0 if stopped is None else 1


if __name__ == "__main__":
    sys.exit(main())

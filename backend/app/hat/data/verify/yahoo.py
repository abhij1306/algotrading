"""python -m app.hat.data.verify.yahoo [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--hours H]:
download D2's second-source sample (`adjustments.second_source_sample`) from Yahoo Finance into
`verify_reference/yahoo/`, slowly, for a verify run to read.

Yahoo rate-limits a client that asks often (HTTP 429), for minutes to hours. This command asks
one symbol every `INTERVAL_S`; on a 429, a 5xx or a network error it waits (`backoff_s`: the
answer's `Retry-After`, else doubling from `BACKOFF_S` to `BACKOFF_MAX_S`) and asks the same
symbol again, until every symbol has an answer or `--hours` run out. Every answer is kept once
(`store.keep_once`): a chart, and a 404's "no such symbol" too, so a rerun asks only the symbols
still without one. The database is read once, for the sample, and the session closed before the
first request: nothing is held while it waits.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from . import FIRST_DAY
from .adjustments import nifty500_members, second_source_sample, yahoo_request

INTERVAL_S = 20.0  # between two requests: about 100 symbols in 35 minutes when Yahoo answers
BACKOFF_S = 900.0  # the first wait after a refusal
BACKOFF_MAX_S = 7200.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*;q=0.8",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    status: int  # HTTP status; 0 for a network error
    data: bytes = b""
    retry_after: float | None = None


def keepable(answer: Answer) -> bool:
    """A chart (200) or Yahoo's "no such symbol" (404 with its JSON body): both are answers."""
    return answer.status in (200, 404) and answer.data.lstrip().startswith(b"{")


def backoff_s(refusals: int, retry_after: float | None) -> float:
    """The wait after the `refusals`-th refusal in a row (1 is the first): the server's
    `Retry-After` when it gives one (never under `INTERVAL_S`), else `BACKOFF_S` doubled per
    refusal, capped at `BACKOFF_MAX_S`."""
    if retry_after is not None and retry_after > 0:
        return max(retry_after, INTERVAL_S)
    return min(BACKOFF_S * 2 ** max(refusals - 1, 0), BACKOFF_MAX_S)


def retry_after_s(value: str | None) -> float | None:
    """A `Retry-After` header given in seconds; an HTTP date or nothing is None."""
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _get(url: str) -> Answer:  # pragma: no cover - the network
    import requests  # noqa: PLC0415 - only when fetching

    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return Answer(0)
    return Answer(r.status_code, r.content, retry_after_s(r.headers.get("Retry-After")))


def fetch_all(
    todo: Sequence[tuple[str, str]],
    keep: Callable[[str, bytes], None],
    deadline: float,
    get: Callable[[str], Answer] = _get,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:  # pragma: no cover - the network loop; its rules are the functions above
    """Each (name, url) asked in turn and kept; the names left without an answer at the
    deadline."""
    left = list(todo)
    refusals = 0
    while left and clock() < deadline:
        name, url = left[0]
        answer = get(url)
        if keepable(answer):
            keep(name, answer.data)
            left.pop(0)
            refusals = 0
            logger.info("yahoo %s: %s (%d left)", name, answer.status, len(left))
            wait = INTERVAL_S
        elif answer.status in (429, 0) or answer.status >= 500:
            refusals += 1
            wait = backoff_s(refusals, answer.retry_after)
            logger.info("yahoo refused (%s); waiting %.0f s", answer.status, wait)
        else:  # another status: not an answer to keep, and asking again would not change it
            logger.info("yahoo %s: unexpected %s, skipped", name, answer.status)
            left.pop(0)
            wait = INTERVAL_S
        if left:
            sleep(min(wait, max(deadline - clock(), 0)))
    return [n for n, _ in left]


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - database and network
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ...models import HatMarket  # noqa: PLC0415
    from ...settings import operator_settings  # noqa: PLC0415
    from ..store import DATASET_BARS, DataStore, keep_once  # noqa: PLC0415
    from .context import Context  # noqa: PLC0415

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.verify.yahoo")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=FIRST_DAY)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--hours", type=float, default=6.0)
    args = parser.parse_args(argv)
    settings = operator_settings()
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    days = store.days(DATASET_BARS)
    end = args.end or (days[-1] if days else date.today())  # noqa: DTZ011 - a calendar date
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatMarket})
        ctx = Context(store, session, args.start, end)
        symbols = dict(ctx.rows("SELECT CAST(id AS TEXT), symbol FROM hat_instrument"))
        sample = second_source_sample(symbols, nifty500_members(ctx))
    folder = ctx.reference_dir("yahoo")
    todo = [
        (name, url)
        for name, url in (yahoo_request(symbols[i], args.start, end) for i in sample)
        if not (folder / name).is_file()
    ]
    logger.info("yahoo: %d of %d symbols to ask", len(todo), len(sample))
    left = fetch_all(
        todo,
        lambda name, data: keep_once(folder / name, data),
        time.monotonic() + args.hours * 3600,
    )
    sys.stdout.write(f"yahoo: {len(sample) - len(left)} of {len(sample)} kept, {len(left)} left\n")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())

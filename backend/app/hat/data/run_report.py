"""A command-line run's report kept on disk (HAT-DATA-096, HAT-DATA-119).

The data commands print their report to stdout; each also appends it to
`<root>/<command>/report-YYYY-MM-DD.txt` (the IST date the run finished), so a run's coverage
survives without `tee`, and a day's runs all stay in that day's file.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..time import IST

logger = logging.getLogger(__name__)


def save_report(directory: Path, text: str, now: datetime) -> Path | None:
    """Append `text` under a line with the time `now` (IST) to the day's report file in
    `directory`; the file's path, or None when it could not be written (the report was already
    printed, so the run's outcome does not change)."""
    local = now.astimezone(IST)
    path = directory / f"report-{local.date().isoformat()}.txt"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"--- run finished {local.isoformat(timespec='seconds')}\n{text}")
            if not text.endswith("\n"):
                handle.write("\n")
    except OSError:
        logger.warning("report_not_saved: %s", path)
        return None
    return path

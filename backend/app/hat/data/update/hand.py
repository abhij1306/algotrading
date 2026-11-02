"""The NSE files the daily data update takes from the owner's hand while `HAT_NSE_WEB` is off
(`nse_web`, docs/runbook.md, Daily data update).

The owner downloads each file NSE publishes and hands it over with
`scripts/hat_data_update.sh --only <step> --file <path>`, which copies it into the inbox
`HAT_DATA_ROOT/admin/nse/` under its own name; a step that needs a file it does not find there
reports `manual` with the file's address, the name to save it under and that command. The names
and addresses here are pure; `find`, `take` and `retire` are the only file IO.
"""

from __future__ import annotations

import shlex
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..index_history import (
    HISTORY_URL,
    NIFTY_500,
    NIFTY_500_TRI,
    TRI_URL,
    tri_raw_name,
    tri_request,
)
from ..nse_changes import NAME_CHANGES_URL, SYMBOL_CHANGES_URL
from ..nse_files import EQUITY_LIST_URL, HOLIDAYS_URL, PR_ARCHIVE_URL

INBOX = Path("admin") / "nse"  # under the data root
USED = INBOX / "used"  # a dated file's step moves it to used/<run date>/ once read
UPDATE = "scripts/hat_data_update.sh"


@dataclass(frozen=True)
class HandFile:
    """One file the owner downloads: the name the step reads from the inbox, its address, and,
    for a POST, the request body (the file is the answer)."""

    name: str
    url: str
    body: str = ""

    def how(self) -> str:
        if self.body:
            return (
                f"curl -s -X POST {shlex.quote(self.url)}"
                " -H 'Content-Type: application/json; charset=UTF-8'"
                f" --data {shlex.quote(self.body)} -o {self.name}"
            )
        return f"{self.url} saved as {self.name}"


HOLIDAYS = HandFile("holiday-master.json", HOLIDAYS_URL)
EQUITY_LIST = HandFile("EQUITY_L.csv", EQUITY_LIST_URL)
SYMBOL_CHANGES = HandFile("symbolchange.csv", SYMBOL_CHANGES_URL)
NAME_CHANGES = HandFile("namechange.csv", NAME_CHANGES_URL)
NIFTY500_LIST = HandFile(
    "ind_nifty500list.csv", "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
)
NIFTY_PRESS = "https://www.niftyindices.com/press-release"


def pr_archive(day: date) -> HandFile:
    """NSE's PR archive of `day` (its `bc` CSV is the night's corporate actions)."""
    stamp = day.strftime("%d%m%y")
    return HandFile(f"PR{stamp}.zip", PR_ARCHIVE_URL.format(stamp=stamp))


def niftyindices_series(name: str, base: str, url: str, start: date, end: date) -> HandFile:
    """niftyindices' JSON answer for `base` over [start, end], saved as `tri_raw_name`'s name
    (`NIFTY_500_TRI_20260101_20261030.json`): the TRI and history POSTs take the same body."""
    body = tri_request(base, start, end).decode()
    return HandFile(tri_raw_name(name, start, end), url, body)


def nifty500_tri(start: date, end: date) -> HandFile:
    return niftyindices_series(NIFTY_500_TRI, NIFTY_500, TRI_URL, start, end)


def history_series(name: str, start: date, end: date) -> HandFile:
    return niftyindices_series(name, name, HISTORY_URL, start, end)


def series_end(name: str, prefix: str) -> date | None:
    """The last day a `niftyindices_series` file name covers (`<prefix>_<from>_<to>.json`);
    None for another name."""
    stem = name.removesuffix(".json")
    head, _, rest = stem.partition(prefix + "_")
    parts = rest.split("_")
    if head or len(parts) != 2 or not all(p.isdigit() and len(p) == 8 for p in parts):
        return None
    try:
        return date(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:]))
    except ValueError:
        return None


def manual(step: str, files: Sequence[HandFile], note: str = "", *, dated: bool = False) -> str:
    """A step's `manual` detail: each file's address and name, then the import command (with
    `--as-of`, the download day, for a file that is a snapshot of that day: `dated`)."""
    wanted = "; ".join(f.how() for f in files)
    given = " ".join(f.name for f in files)
    lead = f"{note}: " if note else ""
    as_of = " --as-of <the day you downloaded it, YYYY-MM-DD>" if dated else ""
    return f"{lead}download {wanted}; then {UPDATE} --only {step} --file {given}{as_of}"


# --- the inbox --------------------------------------------------------------------------------


def find(root: Path, name: str) -> Path | None:
    """The inbox's file `name`, None when the owner has not handed it over."""
    path = root / INBOX / name
    return path if path.is_file() else None


def matching(root: Path, pattern: str) -> list[Path]:
    """The inbox's files matching `pattern`, by name."""
    folder = root / INBOX
    return sorted(p for p in folder.glob(pattern) if p.is_file()) if folder.is_dir() else []


AS_OF_SUFFIX = ".as-of"  # beside a handed file: the day the owner downloaded it (`--as-of`)


def take(root: Path, files: Iterable[Path], as_of: date | None = None) -> list[Path]:
    """Copy the files the owner gave (`--file`) into the inbox under their own names, each with
    its download day beside it when given (`--as-of`); the copies. A name the inbox holds is
    replaced (the owner's newer download), its old day with it."""
    folder = root / INBOX
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for path in files:
        target = folder / path.name
        if path.resolve() != target.resolve():
            shutil.copyfile(path, target)
        stamp = target.with_name(target.name + AS_OF_SUFFIX)
        if as_of is not None:
            stamp.write_text(as_of.isoformat() + "\n")
        else:
            stamp.unlink(missing_ok=True)
        out.append(target)
    return out


def as_of(path: Path) -> date | None:
    """The download day the owner gave with a handed file (`--as-of`); None without one."""
    stamp = path.with_name(path.name + AS_OF_SUFFIX)
    try:
        return date.fromisoformat(stamp.read_text().strip())
    except (OSError, ValueError):
        return None


def as_of_refused(given: date, today: date, latest_change: date | None) -> str | None:
    """Why a snapshot downloaded on `given` cannot be loaded as of that day, else None: a day
    after today, or before the latest stored change in effect by today (`latest_change`): the
    list would undo a change it predates."""
    if given > today:
        return f"as_of_in_future: {given}"
    if latest_change is not None and given < latest_change:
        return f"as_of_before_change: {given} is before the stored change of {latest_change}"
    return None


def retire(root: Path, paths: Iterable[Path], today: date) -> None:
    """Move files a step has read into `used/<today>/`, so the next run asks for a new one."""
    folder = root / USED / today.isoformat()
    for path in paths:
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), folder / path.name)
        stamp = path.with_name(path.name + AS_OF_SUFFIX)
        if stamp.exists():
            shutil.move(str(stamp), folder / stamp.name)

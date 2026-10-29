"""NSE's published text files the data jobs read (the equity, holiday, industry and price-band
lists, the PR archive's corporate-action file), each as a `TextSource` downloading on each call.
Shared by the live assembly (`daily/production.py`) and the daily data update."""

from __future__ import annotations

import io
import zipfile
from datetime import date

from .bhavcopy import Fetcher
from .reference_jobs import TextSource

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
INDUSTRY_LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
PRICE_BANDS_URL = "https://nsearchives.nseindia.com/content/equities/sec_list_{stamp}.csv"
# The daily PR archive (published about 17:30 IST); its `bc` CSV lists the corporate actions
# of the coming sessions, the dividends with their cash (CA4).
PR_ARCHIVE_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{stamp}.zip"
PR_MEMBER_MAX = 5_000_000  # the bc CSV is about 15 kB: a larger member is refused, not read


def url_source(url: str, fetch: Fetcher) -> TextSource:
    """A text source downloading `url` on each call (no response text is kept on a failure)."""

    def read() -> str:
        result = fetch(url)
        if result.status != "ok" or not result.data:
            raise OSError(result.status)
        return result.data.decode("utf-8-sig")

    return read


def pr_member(data: bytes, name: str) -> str:
    """The text of the member `name` of a PR archive; `OSError` for an archive that is not a
    zip, has no such member or holds one larger than `PR_MEMBER_MAX` (checked before reading)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            found = [i for i in archive.infolist() if i.filename.lower() == name.lower()]
            if len(found) != 1 or found[0].file_size > PR_MEMBER_MAX:
                raise OSError("pr_member_missing")
            with archive.open(found[0]) as member:
                raw = member.read(PR_MEMBER_MAX + 1)
    except zipfile.BadZipFile as exc:
        raise OSError("pr_bad_archive") from exc
    if len(raw) > PR_MEMBER_MAX:
        raise OSError("pr_member_missing")
    return raw.decode("utf-8-sig", errors="replace")


def corporate_action_file(day: date, fetch: Fetcher) -> TextSource:
    """The `bc` CSV of NSE's PR archive of `day`, downloaded on each call."""

    def read() -> str:
        result = fetch(PR_ARCHIVE_URL.format(stamp=day.strftime("%d%m%y")))
        if result.status != "ok" or not result.data:
            raise OSError(result.status)
        return pr_member(result.data, f"bc{day:%d%m%Y}.csv")

    return read

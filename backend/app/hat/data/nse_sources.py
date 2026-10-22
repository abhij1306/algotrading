"""The NSE files the reference jobs read, as text sources over a `Fetcher` (PR 17).

Production passes `bhavcopy.http_fetcher()` (browser-like headers, one request per call); checks
pass recorded fixtures through `reference_jobs.file_source` and never reach these URLs. A failed
download raises, and the job turns it into its stable error code (`reference_jobs._read`).

[A] The locations are NSE's public archive paths as of 2026; the holiday list is the JSON the
calendar parser reads (`parse_nse_holidays`). Each is validated on the first live run.
"""

from __future__ import annotations

from datetime import date

from .bhavcopy import Fetcher
from .reference_jobs import TextSource

__all__ = [
    "EQUITY_LIST_URL",
    "HOLIDAYS_URL",
    "INDUSTRY_LIST_URL",
    "price_band_url",
    "url_source",
]

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
INDUSTRY_LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
_PRICE_BANDS = "https://nsearchives.nseindia.com/content/equities/sec_list_{stamp}.csv"


class SourceUnavailableError(OSError):
    """A download that did not answer with a file (no response text is kept)."""


def price_band_url(day: date) -> str:
    """The price-band list in force on `day` (`sec_list_DDMMYYYY.csv`)."""
    return _PRICE_BANDS.format(stamp=day.strftime("%d%m%Y"))


def url_source(url: str, fetch: Fetcher) -> TextSource:
    """A text source downloading `url` on each call."""

    def read() -> str:
        result = fetch(url)
        if result.status != "ok" or not result.data:
            raise SourceUnavailableError(result.status)
        return result.data.decode("utf-8-sig")

    return read

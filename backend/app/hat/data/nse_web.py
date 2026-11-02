"""The switch that keeps NSE's websites out of reach (docs/decisions.md, NSE web off).

NSE answers this machine with an Akamai 403 on every host of its website and archives
(`www.nseindia.com`, `nsearchives.nseindia.com`) and on NSE Indices' (`www.niftyindices.com`),
and the owner ruled that HAT never asks them from here nor works around the block. `HAT_NSE_WEB`
(default off) is that rule as one setting: while it is off, every HTTP primitive an NSE URL can
pass through (`bhavcopy.http_fetcher`, `index_history.http_poster`, the results client, the
announcement feeds, the membership script's download) calls `guard` first, which refuses the URL
before a socket is opened. The NSE MCP servers (`mcp.nseindia.in`) are another host and stay
reachable. The daily data update takes its NSE files from the owner's hand downloads while it is
off (`update/hand.py`).
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from ..errors import HatError
from ..settings import process_env

NSE_WEB_ENV = "HAT_NSE_WEB"
# A host is NSE's web when it is one of these or under one (any subdomain).
NSE_WEB_DOMAINS = ("nseindia.com", "niftyindices.com")
CODE_NSE_WEB_OFF = "nse_web_off"


class NseWebOffError(HatError):
    """A request to NSE's website or archives while `HAT_NSE_WEB` is off; `code` only."""

    def __init__(self) -> None:
        super().__init__(CODE_NSE_WEB_OFF)
        self.code = CODE_NSE_WEB_OFF


def is_nse_web(url: str) -> bool:
    """Whether `url`'s host is NSE's website, archives or NSE Indices' site (any subdomain)."""
    host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in NSE_WEB_DOMAINS)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """`HAT_NSE_WEB` is on (`1`, `true`, `yes`, `on`); unset or anything else is off."""
    source = process_env() if env is None else env
    return (source.get(NSE_WEB_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def refused(url: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether `guard` refuses `url`: an NSE web URL while the switch is off."""
    return is_nse_web(url) and not enabled(env)


def guard(url: str, env: Mapping[str, str] | None = None) -> None:
    """Raise `NseWebOffError` for an NSE web URL while the switch is off; anything else passes."""
    if refused(url, env):
        raise NseWebOffError

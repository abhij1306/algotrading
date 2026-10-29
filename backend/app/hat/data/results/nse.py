"""NSE's results listings and announcements: the endpoints, their answers as plain values, and the
paced client that fetches them into the raw cache (`raw.RawCache`).

The JSON API (`www.nseindia.com/api/...`) answers only a browser-like session that first loaded
the referring page (for its cookies); the XBRL documents come from `nsearchives.nseindia.com`.
One `Pacer` paces both at one request a second (bhavcopy's `DEFAULT_INTERVAL_SECONDS`); a
401/403/429 waits `BACKOFF_SECONDS` before each retry and a 401/403 re-loads the page first.
Answers with those statuses, and network errors, are not cached, so a rerun asks again. The
fields used, and why the listing time is not the filing time before 2022, are in the A0 probe
report (docs/runbook.md, Financial results).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from ...time import IST
from ..bhavcopy import BROWSER_HEADERS, DEFAULT_INTERVAL_SECONDS
from ..pacing import Pacer
from .raw import Entry, RawCache

BASE = "https://www.nseindia.com"
RESULTS_PAGE = BASE + "/companies-listing/corporate-filings-financial-results"
ANNOUNCEMENTS_PAGE = BASE + "/companies-listing/corporate-filings-announcements"
RESULTS_API = BASE + "/api/corporates-financial-results"
INTEGRATED_API = BASE + "/api/integrated-filing-results"
ANNOUNCEMENTS_API = BASE + "/api/corporate-announcements"
INTEGRATED_FINANCIALS = "Integrated Filing- Financials"
API_HEADERS = {**BROWSER_HEADERS, "Accept": "application/json, text/plain, */*"}
RETRY_STATUSES = frozenset({401, 403, 429})
BACKOFF_SECONDS = (5.0, 20.0, 60.0)
INTEGRATED_PAGE_SIZE = 500  # the endpoint pages by 20 unless asked; no symbol has 500 rows

LISTING_REG33 = "reg33"
LISTING_INSURANCE = "insurance"
LISTING_INTEGRATED = "integrated"


# --- URLs -----------------------------------------------------------------------------------


def api_url(base: str, params: Mapping[str, str]) -> str:
    return f"{base}?{urlencode(params)}"


def results_url(symbol: str) -> str:
    return api_url(RESULTS_API, {"index": "equities", "symbol": symbol, "period": "Quarterly"})


def insurance_url(start: date, end: date) -> str:
    """Every insurer's Reg 33 filings broadcast in [start, end] (the insurance index is small)."""
    params = {"index": "insurance", "period": "Quarterly", **_dates(start, end)}
    return api_url(RESULTS_API, params)


def integrated_url(symbol: str) -> str:
    params = {
        "index": "equities",
        "symbol": symbol,
        "type": INTEGRATED_FINANCIALS,
        "size": str(INTEGRATED_PAGE_SIZE),
    }
    return api_url(INTEGRATED_API, params)


def announcements_day_url(day: date) -> str:
    """Every company's announcements broadcast on `day` (one request a day covers the market)."""
    return api_url(ANNOUNCEMENTS_API, {"index": "equities", **_dates(day, day)})


def _dates(start: date, end: date) -> dict[str, str]:
    return {"from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}


# --- listings -------------------------------------------------------------------------------

NO_XBRL_SUFFIXES = ("/-", "/null")


@dataclass(frozen=True)
class Filing:
    """One results filing of a listing, normalised across the endpoints."""

    listing: str  # reg33 | insurance | integrated
    symbol: str
    isin: str  # "" when the listing does not give it (integrated)
    seq: str
    period_end: date  # the listing's period end (the document's wins when they differ)
    basis: str  # standalone | consolidated | "" (not stated)
    broadcast_at: datetime | None  # the listing's broadcast time, to the second
    created_at: datetime | None  # integrated: the document's creation time
    xbrl_url: str | None
    revision: bool  # integrated `type_Sub` Revision
    markers: str  # the listing's own format markers, joined


def nse_time(raw: str | None) -> datetime | None:
    """`16-Jan-2025 20:20:21` (or without seconds, or a bare date) as an IST datetime."""
    if not raw or raw.strip() in {"-", ""}:
        return None
    for pattern in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw.strip().title(), pattern).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def basis_of(raw: str | None) -> str:
    text = (raw or "").strip().lower()
    if text == "consolidated":
        return "consolidated"
    if text in {"non-consolidated", "standalone"}:
        return "standalone"
    return ""


def _xbrl(raw: str | None) -> str | None:
    return None if not raw or raw.endswith(NO_XBRL_SUFFIXES) else raw


def parse_results_listing(body: bytes, listing: str = LISTING_REG33) -> list[Filing]:
    """The `corporates-financial-results` answer (a JSON list, or `{"data": [...], "msg": ...}`
    as NSE answers a symbol it has no results for; the insurance index names its period end
    `periodEnd`, the others `toDate`); rows without a period end are skipped."""
    answer = json.loads(body or b"[]")
    rows = answer.get("data") if isinstance(answer, dict) else answer
    if not isinstance(rows, list):
        raise ValueError(f"unexpected results listing: {str(answer)[:200]}")
    filings = []
    for row in rows:
        end = nse_time(row.get("toDate") or row.get("periodEnd"))
        if end is None:
            continue
        markers = [row.get(k) or "-" for k in ("format", "indAs", "bank", "insuranceType")]
        filings.append(
            Filing(
                listing=listing,
                symbol=row.get("symbol") or "",
                isin=row.get("isin") or "",
                seq=str(row.get("seqNumber") or ""),
                period_end=end.date(),
                basis=basis_of(row.get("consolidated")),
                broadcast_at=nse_time(row.get("broadCastDate")),
                created_at=None,
                xbrl_url=_xbrl(row.get("xbrl")),
                revision=False,
                markers="/".join(m for m in markers if m != "-") or "-",
            )
        )
    return filings


def parse_integrated_listing(body: bytes) -> tuple[list[Filing], int]:
    """The `integrated-filing-results` answer (`{"data": [...], "totalCount": n}`): its
    Financials rows, and the total the endpoint says it has."""
    answer = json.loads(body or b"{}")
    filings = []
    for row in answer.get("data") or []:
        if "Financial" not in (row.get("type") or ""):
            continue
        end = nse_time(row.get("qe_Date"))
        if end is None:
            continue
        sub = row.get("type_Sub") or "-"
        filings.append(
            Filing(
                listing=LISTING_INTEGRATED,
                symbol=row.get("symbol") or "",
                isin="",
                seq=str(row.get("seq_Id") or ""),
                period_end=end.date(),
                basis=basis_of(row.get("consolidated")),
                broadcast_at=nse_time(row.get("broadcast_Date")),
                created_at=nse_time(row.get("creation_Date")),
                xbrl_url=_xbrl(row.get("xbrl")),
                revision=sub.strip().lower().startswith("revis"),
                markers=sub,
            )
        )
    return filings, int(answer.get("totalCount") or 0)


# --- announcements --------------------------------------------------------------------------


@dataclass(frozen=True)
class Announcement:
    symbol: str
    at: datetime  # the company's submission time (`an_dt`)
    desc: str
    text: str
    disseminated: datetime | None = None  # the exchange's dissemination time (`exchdisstime`)

    @property
    def public(self) -> datetime:
        """When the market could read it: the later of `at` and the exchange's dissemination
        (NSE disseminates seconds to days after the submission; a missing one is `at`)."""
        return max(self.at, self.disseminated or self.at)


def parse_announcements(body: bytes) -> list[Announcement]:
    """The `corporate-announcements` answer (a JSON list): `an_dt` is the submission time,
    `exchdisstime` the dissemination time."""
    items = []
    for row in json.loads(body or b"[]"):
        at = nse_time(row.get("an_dt"))
        if at is not None:
            items.append(
                Announcement(
                    row.get("symbol") or "",
                    at,
                    row.get("desc") or "",
                    row.get("attchmntText") or "",
                    nse_time(row.get("exchdisstime")),
                )
            )
    return items


RESULT_DESCS = frozenset({"financial result updates", "financial results"})
BOARD_OUTCOME = "outcome of board meeting"
_RESULTS_WORD = re.compile(r"\bresults?\b", re.IGNORECASE)


def is_results_announcement(item: Announcement) -> str:
    """Which rule makes `item` a results announcement: `result_updates` for a 'Financial Result
    Updates' item; `board_outcome` for a board-meeting outcome whose text names the results (the
    Integrated Filing era files the results PDF under it; an outcome about anything else must
    not date the results early); "" otherwise."""
    desc = item.desc.strip().lower()
    if desc in RESULT_DESCS:
        return "result_updates"
    if desc.startswith(BOARD_OUTCOME) and _RESULTS_WORD.search(item.text):
        return "board_outcome"
    return ""


# --- the paced client -----------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    url: str
    status: int  # 0: no HTTP answer (network error), or not fetched (offline)
    body: bytes
    entry: Entry | None  # the cached record; None when not cached


Reuse = Callable[[Entry], bool]


def always(_entry: Entry) -> bool:
    return True


def transient(status: int) -> bool:
    """A failure worth asking again on the next run (no answer, or a server error): never cached
    and never reused."""
    return status == 0 or status >= 500


class Client:
    """GETs through one browser-like session, paced, every final answer kept in `cache`.

    `get` returns the cached answer when `reuse(entry)` accepts it (documents: always; listings
    and announcement days: when fresh enough, `pipeline`), else fetches. `referer` is the page
    loaded once per session for cookies; None sends no referer and loads no page (the archive)."""

    def __init__(
        self,
        cache: RawCache,
        *,
        offline: bool = False,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        headers: Mapping[str, str] = API_HEADERS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache = cache
        self.offline = offline
        self.pacer = Pacer(min_interval=interval)
        self.headers = dict(headers)
        self.sleep = sleep
        self.requests = 0
        self.statuses: dict[int, int] = {}
        self._session: Any = None
        self._warm: set[str] = set()

    def get(self, url: str, referer: str | None = RESULTS_PAGE, reuse: Reuse = always) -> Answer:
        entry = self.cache.entry(url)
        if entry is not None and (self.offline or (reuse(entry) and not transient(entry.status))):
            return Answer(url, entry.status, self.cache.body(entry), entry)
        if self.offline:
            return Answer(url, 0, b"", None)
        status, body = self._attempts(url, referer)
        if status in RETRY_STATUSES or transient(status):
            return Answer(url, status, body, None)  # not cached: the next run asks again
        entry = self.cache.put(url, status, body)
        return Answer(url, status, body, entry)

    def _attempts(self, url: str, referer: str | None) -> tuple[int, bytes]:
        """Fetch `url`, backing off and retrying on 401/403/429 or a network failure; on a
        401/403 the referer page is visited again first (fresh cookies)."""
        status, body = self._fetch(url, referer)
        for pause in BACKOFF_SECONDS:
            if status not in RETRY_STATUSES and status != 0:
                break
            self.sleep(pause)
            if status in (401, 403) and referer:
                self._warm.discard(referer)
            status, body = self._fetch(url, referer)
        return status, body

    def _fetch(self, url: str, referer: str | None) -> tuple[int, bytes]:  # pragma: no cover
        import requests  # network: never in tests

        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(self.headers)
        if referer and referer not in self._warm:
            self.pacer.wait()
            self._count(None)
            try:
                page = self._session.get(referer, timeout=30, headers={"Accept": "text/html"})
                self._count(page.status_code)
            except requests.RequestException:
                return 0, b""
            self._warm.add(referer)
        self.pacer.wait()
        self._count(None)
        try:
            headers = {"Referer": referer} if referer else {}
            response = self._session.get(url, timeout=60, headers=headers)
        except requests.RequestException:
            return 0, b""
        self._count(response.status_code)
        return response.status_code, response.content

    def _count(self, status: int | None) -> None:  # pragma: no cover - network
        if status is None:
            self.requests += 1
        else:
            self.statuses[status] = self.statuses.get(status, 0) + 1


def filings_of(answers: Iterable[tuple[str, bytes]]) -> list[Filing]:
    """The filings of a mix of listing answers, by the listing each came from."""
    out: list[Filing] = []
    for listing, body in answers:
        if listing == LISTING_INTEGRATED:
            out += parse_integrated_listing(body)[0]
        else:
            out += parse_results_listing(body, listing)
    return out

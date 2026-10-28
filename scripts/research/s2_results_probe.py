"""S2 EP plan, step A0: can NSE's financial-results filings back a point-in-time results store?

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \
        venv/bin/python scripts/research/s2_results_probe.py [--cache DIR] [--offline]

Report only: no table, no parser, no criterion. It asks NSE's JSON API the way the website's
pages do (a browser session that first loads the page, for its cookies) with bhavcopy's
browser headers and the data layer's `Pacer` at one request a second, backs off on
401/403/429 and keeps every answer under `--cache` (default `<HAT_DATA_ROOT>/raw/nse-results-probe`,
outside the repo), so a rerun asks NSE only for what is missing. `--offline` asks nothing.

For a fixed list of 20 symbols it records, per symbol x year 2016-2026, the results filings
(listing metadata, the filing-time field), each XBRL document's HTTP status, its taxonomy
(Reg 33 Ind AS / banking / NBFC / insurance, Integrated Filing) and basis
(standalone / consolidated); for 20 filings it compares the results listing's time with the
broadcast time of the same results' announcement. The database is read only to resolve the
symbols' ISINs. Writes `reports/s2-ep/a0/` at the repo root (gitignored).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.hat.data.bhavcopy import DEFAULT_INTERVAL_SECONDS  # noqa: E402
from app.hat.data.pacing import Pacer  # noqa: E402
from app.hat.data.results.nse import (  # noqa: E402
    ANNOUNCEMENTS_API,
    ANNOUNCEMENTS_PAGE,
    API_HEADERS,
    BACKOFF_SECONDS,
    BOARD_OUTCOME,
    INTEGRATED_API,
    RESULT_DESCS,
    RESULTS_API,
    RESULTS_PAGE,
    RETRY_STATUSES,
    api_url,
    nse_time,
)
from app.hat.data.results.nse import basis_of as _basis  # noqa: E402
from app.hat.time import IST, trade_date_of, utc_now  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

YEARS = range(2016, 2027)
REPORT_DIR = PROJECT_ROOT / "reports" / "s2-ep" / "a0"


# --- the cached, paced client -------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    url: str
    status: int  # 0: no HTTP answer (network error)
    body: bytes
    cached: bool
    path: Path | None  # the cached body


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


class Client:
    """GETs through one browser-like session, one request a second, every final answer kept.

    A 401/403 re-loads the referring page (NSE's cookies expire) and a 401/403/429 waits
    `BACKOFF_SECONDS` before each retry; the last answer is kept whatever it is, except a
    retryable status or a network error, which a rerun asks again."""

    def __init__(self, cache: Path, *, offline: bool = False) -> None:
        self.cache = cache
        self.offline = offline
        self.pacer = Pacer(min_interval=DEFAULT_INTERVAL_SECONDS)
        self.requests = 0
        self.statuses: Counter[int] = Counter()
        self._session: Any = None
        self._warm: set[str] = set()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = cache_key(url)
        folder = self.cache / key[:2]
        return folder / f"{key}.meta.json", folder / f"{key}.body"

    def cached(self, url: str) -> Answer | None:
        meta_path, body_path = self._paths(url)
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text())
        return Answer(url, meta["status"], body_path.read_bytes(), True, body_path)

    def get(self, url: str, referer: str = RESULTS_PAGE) -> Answer:
        hit = self.cached(url)
        if hit is not None or self.offline:
            return hit or Answer(url, 0, b"", True, None)
        status, body = 0, b""
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            if attempt:
                time.sleep(BACKOFF_SECONDS[attempt - 1])
                if status in (401, 403):
                    self._warm.discard(referer)
            status, body = self._fetch(url, referer)
            if status not in RETRY_STATUSES and status != 0:
                break
        if status in RETRY_STATUSES or status == 0:
            return Answer(url, status, body, False, None)
        meta_path, body_path = self._paths(url)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(body)
        meta = {"url": url, "status": status, "fetched_at": datetime.now().astimezone().isoformat()}
        meta_path.write_text(json.dumps(meta))
        return Answer(url, status, body, False, body_path)

    def _fetch(self, url: str, referer: str) -> tuple[int, bytes]:  # pragma: no cover - network
        import requests

        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(API_HEADERS)
        if referer not in self._warm:
            self.pacer.wait()
            self.requests += 1
            try:
                page = self._session.get(referer, timeout=30, headers={"Accept": "text/html"})
                self.statuses[page.status_code] += 1
            except requests.RequestException:
                return 0, b""
            self._warm.add(referer)
        self.pacer.wait()
        self.requests += 1
        try:
            response = self._session.get(url, timeout=60, headers={"Referer": referer})
        except requests.RequestException:
            return 0, b""
        self.statuses[response.status_code] += 1
        return response.status_code, response.content


# --- pure helpers: listings, taxonomy, announcements, timestamps ---------------------------------


SOURCE_REG33 = "reg33"
SOURCE_INSURANCE = "reg33_insurance_listing"
SOURCE_INTEGRATED = "integrated"
NO_XBRL_SUFFIX = "/-"


@dataclass(frozen=True)
class Filing:
    """One results filing of a listing, normalised across the two endpoints."""

    source: str
    symbol: str
    seq: str
    period_end: date
    basis: str  # standalone | consolidated | "" (not stated)
    filed_at: datetime | None  # the listing's broadcast time (to the second)
    filing_date_field: str  # the listing's field `filed_at` was read from
    xbrl_url: str | None
    detail_url: str | None  # the pre-XBRL HTML results page, when listed
    listing_format: str  # the listing's own format markers, joined


def _xbrl(raw: str | None) -> str | None:
    return None if not raw or raw.endswith(NO_XBRL_SUFFIX) or raw.endswith("/null") else raw


def parse_results_listing(body: bytes, source: str = SOURCE_REG33) -> list[Filing]:
    """The `corporates-financial-results` answer (a JSON list; the insurance index names its
    period end `periodEnd`, the others `toDate`); rows without a period end are skipped."""
    filings = []
    for row in json.loads(body or b"[]"):
        end = nse_time(row.get("toDate") or row.get("periodEnd"))
        if end is None:
            continue
        markers = [row.get(k) or "-" for k in ("format", "indAs", "bank", "insuranceType")]
        filings.append(
            Filing(
                source=source,
                symbol=row.get("symbol", ""),
                seq=str(row.get("seqNumber") or ""),
                period_end=end.date(),
                basis=_basis(row.get("consolidated")),
                filed_at=nse_time(row.get("broadCastDate")),
                filing_date_field="broadCastDate",
                xbrl_url=_xbrl(row.get("xbrl")),
                detail_url=row.get("resultDetailedDataLink") or None,
                listing_format="/".join(m for m in markers if m != "-") or "-",
            )
        )
    return filings


def parse_integrated_listing(body: bytes) -> tuple[list[Filing], int]:
    """The `integrated-filing-results` answer (`{"data": [...], "totalCount": n}`): its
    Financials rows, and the total the endpoint says it has (a total above the rows read means
    a further page)."""
    answer = json.loads(body or b"{}")
    filings = []
    for row in answer.get("data") or []:
        if "Financial" not in (row.get("type") or ""):
            continue
        end = nse_time(row.get("qe_Date"))
        if end is None:
            continue
        filings.append(
            Filing(
                source=SOURCE_INTEGRATED,
                symbol=row.get("symbol", ""),
                seq=str(row.get("seq_Id") or ""),
                period_end=end.date(),
                basis=_basis(row.get("consolidated")),
                filed_at=nse_time(row.get("broadcast_Date")),
                filing_date_field="broadcast_Date",
                xbrl_url=_xbrl(row.get("xbrl")),
                detail_url=None,
                listing_format=f"{row.get('type_Sub') or '-'}",
            )
        )
    return filings, int(answer.get("totalCount") or 0)


_SCHEMA_REF = re.compile(rb"schemaRef[^>]*?href=\"([^\"]+)\"")
_INTEGRATED_NS = re.compile(rb"/xbrl/IntegratedFinance_([A-Za-z]+)/")
_NATURE = re.compile(rb"NatureOfReportStandaloneConsolidated[^>]*>\s*([A-Za-z]+)")
_SCHEMA_FORMATS = (
    ("banking_entry_point", "reg33_banking"),
    ("ind-as_entry_point", "reg33_indas"),
    ("other_than_banks_entry_point", "reg33_non_indas"),
    ("in-bse-fin-", "reg33_nbfc"),
)


@dataclass(frozen=True)
class XbrlKind:
    format: str  # reg33_* | integrated_* | not_xbrl
    schema_ref: str
    basis: str  # from the document's NatureOfReportStandaloneConsolidated, "" when absent


def classify_xbrl(body: bytes) -> XbrlKind:
    """The taxonomy of an NSE results XBRL instance, from its schemaRef and namespaces: the
    Integrated Filing namespaces name their variant (`IntegratedFinance_IndAS`, `_Banking`,
    `_NBFC`, `_LI`, ...); the Reg 33 instances differ by entry point, insurance by namespace."""
    head = body[:20000]
    ref = _SCHEMA_REF.search(head)
    schema = ref.group(1).decode(errors="replace") if ref else ""
    nature = _NATURE.search(body)
    basis = _basis(nature.group(1).decode()) if nature else ""
    if not schema or b"xbrl" not in head[:2000]:
        return XbrlKind("not_xbrl", schema, basis)
    integrated = _INTEGRATED_NS.search(head)
    if integrated:
        return XbrlKind(f"integrated_{integrated.group(1).decode().lower()}", schema, basis)
    if b"/xbrl/Insurance/" in head:
        return XbrlKind("reg33_insurance", schema, basis)
    lowered = schema.lower()
    for prefix, kind in _SCHEMA_FORMATS:
        if lowered.startswith(prefix):
            return XbrlKind(kind, schema, basis)
    return XbrlKind("other", schema, basis)


_DETAIL_FIELDS = (b"Net Profit", b"Basic")


def detail_has_figures(body: bytes) -> bool:
    """Whether a pre-XBRL HTML results page carries the figures (net profit and basic EPS)."""
    return all(field in body for field in _DETAIL_FIELDS)


@dataclass(frozen=True)
class Announcement:
    at: datetime
    desc: str
    text: str
    attachment: str


def parse_announcements(body: bytes) -> list[Announcement]:
    """The `corporate-announcements` answer (a JSON list); `an_dt` is the broadcast time."""
    items = []
    for row in json.loads(body or b"[]"):
        at = nse_time(row.get("an_dt"))
        if at is not None:
            text = row.get("attchmntText") or ""
            items.append(
                Announcement(at, row.get("desc") or "", text, row.get("attchmntFile") or "")
            )
    return items


def results_announcement(items: Iterable[Announcement]) -> tuple[Announcement | None, str]:
    """The results announcement among `items` (one fetched window around a filing): the
    earliest 'Financial Result Updates' item, else the earliest board-meeting outcome (its
    text rarely names the results), with which rule matched; Integrated Filing, press-release
    and presentation items never match."""
    items = sorted(items, key=lambda i: i.at)
    for item in items:
        if item.desc.strip().lower() in RESULT_DESCS:
            return item, "result_updates"
    for item in items:
        if item.desc.strip().lower().startswith(BOARD_OUTCOME):
            return item, "board_outcome"
    return None, ""


def seconds_between(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds())


def summary(values: Sequence[int]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {
        "n": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


_NAME_STAMP = re.compile(r"_(\d{14})(?:_WEB)?\.xml$")


def xbrl_name_stamp(url: str) -> str:
    """The `ddmmyyyyhhmmss` stamp in an XBRL file name as `yyyy-mm-dd hh:mm:ss` (a 12-hour
    clock without AM/PM, so an evening filing reads 12 hours early), "" when absent."""
    match = _NAME_STAMP.search(url)
    if not match:
        return ""
    s = match.group(1)
    return f"{s[4:8]}-{s[2:4]}-{s[0:2]} {s[8:10]}:{s[10:12]}:{s[12:14]}"


def xbrl_stamp_at(url: str) -> datetime | None:
    """The XBRL file name's stamp as an IST time, read as written (the clock is 12-hour, so
    the true time is this or 12 hours later)."""
    stamp = xbrl_name_stamp(url)
    return datetime.fromisoformat(stamp).replace(tzinfo=IST) if stamp else None


def quarters_in(year: int, today: date) -> int:
    """Calendar quarter ends of `year` whose results could be out by `today` (45 days after)."""
    ends = [date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31)]
    return sum(1 for end in ends if end + timedelta(days=45) <= today)


# --- the probe ---------------------------------------------------------------------------------

# (symbol, cap band, kind): fixed, chosen from NIFTY 500 point-in-time members (hat_index_member)
SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("RELIANCE", "large", "corporate"),
    ("TCS", "large", "corporate"),
    ("INFY", "large", "corporate"),
    ("HINDUNILVR", "large", "corporate"),
    ("LT", "large", "corporate"),
    ("HDFCBANK", "large", "bank"),
    ("ICICIBANK", "large", "bank"),
    ("SBIN", "large", "bank"),
    ("FEDERALBNK", "mid", "bank"),
    ("BAJFINANCE", "large", "nbfc"),
    ("CHOLAFIN", "large", "nbfc"),
    ("MUTHOOTFIN", "mid", "nbfc"),
    ("MANAPPURAM", "small", "nbfc"),
    ("HDFCLIFE", "large", "insurer"),
    ("PERSISTENT", "mid", "corporate"),
    ("VOLTAS", "mid", "corporate"),
    ("TATAELXSI", "mid", "corporate"),
    ("CERA", "small", "corporate"),
    ("JKLAKSHMI", "small", "corporate"),
    ("SONATSOFTW", "small", "corporate"),
)
QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def results_url(symbol: str, index: str = "equities", period: str = "Quarterly") -> str:
    return api_url(RESULTS_API, {"index": index, "symbol": symbol, "period": period})


def integrated_url(symbol: str) -> str:
    params = {"index": "equities", "symbol": symbol, "type": "Integrated Filing- Financials"}
    return api_url(INTEGRATED_API, params)


def announcements_url(symbol: str, start: date, end: date) -> str:
    params = {
        "index": "equities",
        "symbol": symbol,
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
    }
    return api_url(ANNOUNCEMENTS_API, params)


def isins(symbols: Iterable[str]) -> dict[str, str]:  # pragma: no cover - the database
    from sqlalchemy import select

    from app.database import db_session_scope
    from app.hat.models import HatInstrument

    with db_session_scope() as session:
        rows = session.execute(
            select(HatInstrument.symbol, HatInstrument.isin, HatInstrument.active).where(
                HatInstrument.symbol.in_(list(symbols))
            )
        ).all()
    found: dict[str, str] = {}
    for symbol, isin, _active in sorted(rows, key=lambda r: r.active):
        found[symbol] = isin  # the active instrument's ISIN wins
    return found


@dataclass
class Fetched:
    filing: Filing
    xbrl_status: int | None = None  # None: no XBRL listed
    kind: XbrlKind | None = None
    xbrl_cache: str = ""
    detail_status: int | None = None
    detail_figures: bool | None = None


def listings(client: Client, symbol: str, kind: str) -> tuple[list[Filing], dict[str, Any]]:
    """Every results filing the two listings give for `symbol`, and what each listing said."""
    info: dict[str, Any] = {}
    answer = client.get(results_url(symbol))
    filings = parse_results_listing(answer.body) if answer.status == 200 else []
    info["reg33"] = {"url": answer.url, "status": answer.status, "rows": len(filings)}
    annual = client.get(results_url(symbol, period="Annual"))
    rows = parse_results_listing(annual.body) if annual.status == 200 else []
    info["reg33_annual"] = {"url": annual.url, "status": annual.status, "rows": len(rows)}
    filings += rows
    if kind == "insurer":
        insurance = client.get(results_url(symbol, index="insurance"))
        rows = (
            parse_results_listing(insurance.body, SOURCE_INSURANCE)
            if insurance.status == 200
            else []
        )
        info["insurance"] = {"url": insurance.url, "status": insurance.status, "rows": len(rows)}
        filings += rows
    integrated = client.get(integrated_url(symbol))
    rows, total = parse_integrated_listing(integrated.body) if integrated.status == 200 else ([], 0)
    info["integrated"] = {
        "url": integrated.url,
        "status": integrated.status,
        "rows": len(rows),
        "totalCount": total,
    }
    return filings + rows, info


def fetch_documents(client: Client, filings: Iterable[Filing]) -> list[Fetched]:
    out = []
    for filing in filings:
        item = Fetched(filing)
        if filing.period_end.year in YEARS:
            if filing.xbrl_url:
                answer = client.get(filing.xbrl_url)
                item.xbrl_status = answer.status
                item.xbrl_cache = str(answer.path or "")
                if answer.status == 200:
                    item.kind = classify_xbrl(answer.body)
            elif filing.detail_url:
                answer = client.get(filing.detail_url)
                item.detail_status = answer.status
                item.detail_figures = answer.status == 200 and detail_has_figures(answer.body)
        out.append(item)
    return out


def coverage_rows(
    symbol: str, band: str, kind: str, items: Sequence[Fetched], today: date
) -> list[dict[str, Any]]:
    rows = []
    for year in YEARS:
        mine = [i for i in items if i.filing.period_end.year == year]
        listed = [i for i in mine if i.filing.xbrl_url]
        ok = [i for i in listed if i.xbrl_status == 200]
        quarters = {i.filing.period_end for i in mine}
        xbrl_quarters = {i.filing.period_end for i in ok}
        bases = {i.filing.basis for i in mine if i.filing.basis}
        doc_bases = {i.kind.basis for i in ok if i.kind and i.kind.basis}
        details = [i for i in mine if i.detail_status is not None]
        rows.append(
            {
                "symbol": symbol,
                "band": band,
                "kind": kind,
                "year": year,
                "expected_quarters": quarters_in(year, today),
                "filings": len(mine),
                "quarters_with_meta": len(quarters),
                "time_fields": "+".join(sorted({i.filing.filing_date_field for i in mine})),
                "sources": "+".join(sorted({i.filing.source for i in mine})),
                "xbrl_listed": len(listed),
                "xbrl_200": len(ok),
                "xbrl_statuses": dict(Counter(i.xbrl_status for i in listed)),
                "quarters_with_xbrl": len(xbrl_quarters),
                "formats": dict(Counter(i.kind.format for i in ok if i.kind)),
                "basis_listing": "both" if len(bases) == 2 else "".join(bases) or "-",
                "basis_document": "both" if len(doc_bases) == 2 else "".join(doc_bases) or "-",
                "detail_pages": len(details),
                "detail_200_with_figures": sum(1 for i in details if i.detail_figures),
            }
        )
    return rows


def year_table(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Coverage by year over the symbols: symbol-quarters with metadata and with a downloaded
    XBRL; `complete` when every listed symbol has every due quarter with an XBRL."""
    table = []
    for year in YEARS:
        mine = [r for r in rows if r["year"] == year]
        with_meta = [r for r in mine if r["filings"]]
        expected = sum(r["expected_quarters"] for r in with_meta)
        formats: Counter[str] = Counter()
        for r in mine:
            formats.update(r["formats"])
        table.append(
            {
                "year": year,
                "symbols_with_meta": len(with_meta),
                "filings": sum(r["filings"] for r in mine),
                "symbol_quarters_meta": sum(r["quarters_with_meta"] for r in mine),
                "symbol_quarters_due": expected,
                "xbrl_listed": sum(r["xbrl_listed"] for r in mine),
                "xbrl_200": sum(r["xbrl_200"] for r in mine),
                "symbol_quarters_xbrl": sum(r["quarters_with_xbrl"] for r in mine),
                "symbols_full_xbrl": sum(
                    1
                    for r in with_meta
                    if r["quarters_with_xbrl"] >= r["expected_quarters"] and r["xbrl_listed"]
                ),
                "detail_pages_with_figures": sum(r["detail_200_with_figures"] for r in mine),
                "formats": dict(sorted(formats.items())),
                "complete": bool(with_meta)
                and all(r["quarters_with_xbrl"] >= r["expected_quarters"] for r in with_meta),
            }
        )
    return table


def pick_timing_filings(by_symbol: Mapping[str, Sequence[Fetched]]) -> list[Filing]:
    """20 filings, fixed: for the i-th symbol the quarter ending in year 2019 + i % 8, quarter
    i % 4 + 1 (each year 2019-2026, each quarter, each listing), its earliest-broadcast filing
    (nearest such quarter when the company has none for it)."""
    picks = []
    for index, (symbol, _band, _kind) in enumerate(SYMBOLS):
        month, day = QUARTER_ENDS[index % 4]
        wanted = date(2019 + index % 8, month, day)
        filings = [i.filing for i in by_symbol.get(symbol, ()) if i.filing.filed_at is not None]
        if not filings:
            continue
        end = min({f.period_end for f in filings}, key=lambda e: (abs((e - wanted).days), e))
        same = [f for f in filings if f.period_end == end]
        picks.append(min(same, key=lambda f: (f.filed_at, f.seq)))
    return picks


def _diff(later: datetime | None, earlier: datetime | None) -> int | None:
    return seconds_between(later, earlier) if later and earlier else None


def timing_rows(client: Client, filings: Sequence[Filing]) -> list[dict[str, Any]]:
    """Each filing against the results announcement of the same results, searched from two
    days before to a day after the earlier of the listing time and the XBRL name stamp (the
    listing time can be a later re-broadcast)."""
    rows = []
    for filing in filings:
        assert filing.filed_at is not None
        stamp = xbrl_stamp_at(filing.xbrl_url or "")
        anchor = min(filing.filed_at.date(), stamp.date() if stamp else filing.filed_at.date())
        answer = client.get(
            announcements_url(
                filing.symbol, anchor - timedelta(days=2), anchor + timedelta(days=1)
            ),
            referer=ANNOUNCEMENTS_PAGE,
        )
        items = parse_announcements(answer.body) if answer.status == 200 else []
        match, rule = results_announcement(items)
        at = match.at if match else None
        diff = _diff(filing.filed_at, at)
        rows.append(
            {
                "symbol": filing.symbol,
                "source": filing.source,
                "period_end": filing.period_end.isoformat(),
                "basis": filing.basis,
                "listing_time": filing.filed_at.isoformat(),
                "listing_field": filing.filing_date_field,
                "xbrl_name_stamp": xbrl_name_stamp(filing.xbrl_url or ""),
                "announcements_url": answer.url,
                "announcements_status": answer.status,
                "announcements": len(items),
                "announcement_time": at.isoformat() if at else "",
                "announcement_rule": rule,
                "announcement_desc": match.desc if match else "",
                "announcement_text": match.text[:120] if match else "",
                "listing_minus_announcement_s": diff,
                "stamp_as_read_minus_announcement_s": _diff(stamp, at),
                "stamp_pm_minus_announcement_s": _diff(
                    stamp + timedelta(hours=12) if stamp and stamp.hour < 12 else stamp, at
                ),
                "listing_before_announcement": diff is not None and diff < 0,
            }
        )
    return rows


def listing_vs_stamp(items: Iterable[Fetched]) -> list[dict[str, Any]]:
    """By period-end year: XBRL filings whose listing time is more than a day after the XBRL
    name stamp's date (the listing time is then not the filing time), and XBRL files listed
    under more than one sequence number."""
    by_year: dict[int, list[Fetched]] = defaultdict(list)
    for item in items:
        if item.filing.xbrl_url and item.filing.filed_at:
            by_year[item.filing.period_end.year].append(item)
    rows = []
    for year in sorted(by_year):
        lags = []
        for item in by_year[year]:
            stamp = xbrl_stamp_at(item.filing.xbrl_url or "")
            if stamp is not None and item.filing.filed_at is not None:
                lags.append((item.filing.filed_at.date() - stamp.date()).days)
        urls = Counter(i.filing.xbrl_url for i in by_year[year])
        rows.append(
            {
                "year": year,
                "xbrl_filings": len(by_year[year]),
                "with_stamp": len(lags),
                "listing_after_stamp_gt_1d": sum(1 for lag in lags if lag > 1),
                "listing_before_stamp": sum(1 for lag in lags if lag < 0),
                "max_lag_days": max(lags, default=None),
                "xbrl_listed_twice": sum(n - 1 for n in urls.values() if n > 1),
            }
        )
    return rows


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: json.dumps(v) if isinstance(v, dict) else v for k, v in row.items()}
            )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - network and database
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    data_root = Path(os.environ.get("HAT_DATA_ROOT", "data_system/hat"))
    parser.add_argument("--cache", type=Path, default=data_root / "raw" / "nse-results-probe")
    parser.add_argument("--out", type=Path, default=REPORT_DIR)
    parser.add_argument("--offline", action="store_true", help="read the cache only")
    args = parser.parse_args(argv)
    client = Client(args.cache, offline=args.offline)
    today = trade_date_of(utc_now())
    found = isins(s for s, _b, _k in SYMBOLS)
    symbols, coverage, documents, listing_info = [], [], [], {}
    by_symbol: dict[str, list[Fetched]] = {}
    for symbol, band, kind in SYMBOLS:
        filings, info = listings(client, symbol, kind)
        listing_info[symbol] = info
        items = fetch_documents(client, filings)
        by_symbol[symbol] = items
        coverage += coverage_rows(symbol, band, kind, items, today)
        first = min((i.filing.period_end for i in items), default=None)
        symbols.append(
            {
                "symbol": symbol,
                "isin": found.get(symbol, ""),
                "band": band,
                "kind": kind,
                "filings": len(filings),
                "earliest_period_end": first.isoformat() if first else "",
            }
        )
        for i in items:
            documents.append(
                {
                    **{k: v for k, v in asdict(i.filing).items() if k != "filed_at"},
                    "filed_at": i.filing.filed_at.isoformat() if i.filing.filed_at else "",
                    "xbrl_status": i.xbrl_status,
                    "format": i.kind.format if i.kind else "",
                    "schema_ref": i.kind.schema_ref if i.kind else "",
                    "basis_document": i.kind.basis if i.kind else "",
                    "xbrl_cache": i.xbrl_cache,
                    "detail_status": i.detail_status,
                    "detail_figures": i.detail_figures,
                }
            )
        print(f"{symbol}: {len(filings)} filings, {client.requests} requests so far", flush=True)
    timing = timing_rows(client, pick_timing_filings(by_symbol))
    diffs = [
        r["listing_minus_announcement_s"]
        for r in timing
        if r["listing_minus_announcement_s"] is not None
    ]
    stamp_diffs = [
        r["stamp_pm_minus_announcement_s"]
        for r in timing
        if r["stamp_pm_minus_announcement_s"] is not None
    ]
    lag = listing_vs_stamp(i for items in by_symbol.values() for i in items)
    years = year_table(coverage)
    write_csv(args.out / "listing_vs_stamp.csv", lag)
    write_csv(args.out / "symbols.csv", symbols)
    write_csv(args.out / "coverage_symbol_year.csv", coverage)
    write_csv(args.out / "coverage_year.csv", years)
    write_csv(args.out / "documents.csv", documents)
    write_csv(args.out / "timing.csv", timing)
    summary_json = {
        "requests": client.requests,
        "statuses": dict(client.statuses),
        "cache": str(args.cache),
        "listings": listing_info,
        "timing_listing": summary(diffs),
        "timing_stamp_pm": summary(stamp_diffs),
        "listing_before_announcement": sum(1 for r in timing if r["listing_before_announcement"]),
        "matched": sum(1 for r in timing if r["announcement_rule"]),
        "years": years,
    }
    (args.out / "summary.json").write_text(json.dumps(summary_json, indent=2, default=str))
    print(
        json.dumps(
            {
                k: summary_json[k]
                for k in ("requests", "statuses", "timing_listing", "timing_stamp_pm", "matched")
            },
            default=str,
        )
    )
    for row in years + lag:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

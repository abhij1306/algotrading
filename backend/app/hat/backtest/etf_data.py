"""The ETF overlay's data (`overlay.EtfData`) in the store, under `HAT_DATA_ROOT/etf/`: written
from the overlay data build (`scripts/data/overlay_etf.py store`) and read by a run with an
overlay (`load`). Prices are kept as integers of 1/10000 rupee, as the build rounds them.

- `daily.parquet`: NIFTYBEES, GOLDBEES and LIQUIDBEES daily bars, split-adjusted (the build's
  `etf_daily_adjusted.csv`), with each day's adjustment factor.
- `bars_5m.parquet`: their 5-minute bars by start instant (UTC), from the raw Kotak files: a
  bar's label is its end through `bars5m_import.END_LABELLED_UNTIL` and its start after
  (`bars5m_import.start_epoch`), each price times the day's daily factor; a bar read twice
  (overlapping files) is kept once.
- `liquid_tr.parquet`: LIQUIDBEES' total-return index, each day's basis (`start`, `proxy`: the
  rate-index proxy where the dividends are not known, `dividend`).
- `indices.parquet`: NIFTY 50 (the regime rule's) and the reference total-return indices, with
  NIFTY 50's opens from HAT's own `index_daily` (NIFTYBEES' estimated price).
- `manifest.json`: each file's digest, rows and dates, the build it came from, and the data
  check `open_gaps`: each ETF's daily opens more than 2% from the previous close
  where the price the sleeve fills at is not, the thin pre-open auction prints a fill never uses.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from ..data.bars5m_import import start_epoch
from ..data.store import DATASET_INDEX
from ..money import exact
from ..time import IST
from .overlay import ASSETS, EtfBar, EtfData, SleevePrices

__all__ = [
    "ETF_DIR",
    "REFERENCES",
    "REGIME_INDEX",
    "EtfDataError",
    "check_files",
    "e4",
    "five_min_rows",
    "load",
    "manifest",
    "open_gaps",
    "proxy_ranges",
    "write",
]

ETF_DIR = "etf"
REGIME_INDEX = "NIFTY 50"
REFERENCES = ("NIFTY 50 TRI", "NIFTY 500 TRI")
PROXY = "proxy"
NONE = "none"  # a day the series carries unchanged: neither ends a proxy range nor starts one
# the bars of the trading session (IST): a bar starting before the open is the pre-open's
_OPEN, _CLOSE = time(9, 15), time(15, 30)
_E4 = Decimal(10_000)
_DAILY = "daily.parquet"
_BARS_5M = "bars_5m.parquet"
_LIQUID_TR = "liquid_tr.parquet"
_INDICES = "indices.parquet"
_MANIFEST = "manifest.json"
_PRICES = ("open", "high", "low", "close")
GAP_PCT = Decimal("0.02")  # the data check's open gap (`open_gaps`)


class EtfDataError(ValueError):
    """The store is missing or a value in the build cannot be read."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def e4(value: object) -> int:
    """A price as an integer of 1/10000 rupee; `etf_store_bad_value` for one that is not a
    finite decimal of at most four places."""
    number = exact(str(value))
    if number is None or number < 0:
        raise EtfDataError("etf_store_bad_value")
    scaled = number * _E4
    if scaled != scaled.to_integral_value():
        raise EtfDataError("etf_store_bad_value")
    return int(scaled)


def _volume(value: object) -> int:
    number = exact(str(value))
    if number is None or number < 0:
        raise EtfDataError("etf_store_bad_value")
    return int(number)


def _price(units: int) -> Decimal:
    return Decimal(units) / _E4


def _adjusted(value: object, factor: Decimal) -> Decimal:
    number = exact(str(value))
    if number is None:
        raise EtfDataError("etf_store_bad_value")
    return (number * factor).quantize(Decimal("0.0001"))


def five_min_rows(
    symbol: str, candles: Iterable[Sequence[Any]], factors: Mapping[date, Decimal]
) -> list[tuple[str, int, int, int, int, int, int]]:
    """The 5-minute bars of `candles` (`[label "YYYY-MM-DDTHH:MM:SS+0530", o, h, l, c, v]`) as
    rows `(symbol, start epoch, open, high, low, close, volume)`, prices in 1/10000 rupee times
    the day's daily factor, in start order; a start read twice is kept once (the first), and a
    bar outside the session (the pre-open's) or of a day without a daily bar is left out."""
    out: dict[int, tuple[str, int, int, int, int, int, int]] = {}
    for label, *values in candles:
        start = start_epoch(int(datetime.fromisoformat(str(label)).timestamp()))
        moment = datetime.fromtimestamp(start, IST)
        factor = factors.get(moment.date())
        if factor is None or start in out or not _OPEN <= moment.time() < _CLOSE:
            continue
        # adjusted as the daily bars are, to the build's four places
        o, h, lo, c = (e4(_adjusted(v, factor)) for v in values[:4])
        out[start] = (symbol, start, o, h, lo, c, _volume(values[4]))
    return [out[k] for k in sorted(out)]


def proxy_ranges(rows: Iterable[tuple[date, str]]) -> list[tuple[date, date]]:
    """The first and last day of each run of consecutive days (in date order) whose basis is
    `proxy`, a `none` day inside a run not ending it."""
    out: list[tuple[date, date]] = []
    start: date | None = None
    last: date | None = None
    for day, basis in sorted(rows):
        if basis == NONE:
            continue
        if basis == PROXY:
            start = day if start is None else start
            last = day
        elif start is not None and last is not None:
            out.append((start, last))
            start = last = None
    if start is not None and last is not None:
        out.append((start, last))
    return out


def open_gaps(data: EtfData, pct: Decimal = GAP_PCT) -> dict[str, list[dict[str, str]]]:
    """Each ETF's sessions whose daily open is more than `pct` from the previous close while
    the price the sleeve fills at that open (`SleevePrices`: the first 5-minute bar's close, else
    the estimate, NIFTY 50's own open move for NIFTYBEES) is not: an auction print, not a move."""
    prices = SleevePrices(data)
    out: dict[str, list[dict[str, str]]] = {}
    for asset in ASSETS:
        found = []
        for day, bar in sorted(data.daily.get(asset, {}).items()):
            before, fill = prices.prev_close(asset, day), prices.at(asset, day)
            if before is None or fill is None or before[1] <= 0:
                continue
            prev = before[1]
            if abs(bar.open / prev - 1) > pct >= abs(fill[0] / prev - 1):
                found.append(
                    {
                        "date": day.isoformat(),
                        "open": str(bar.open),
                        "prev_close": str(prev),
                        "fill_price": str(fill[0]),
                        "estimated": str(fill[1]).lower(),
                    }
                )
        out[asset] = found
    return out


def _index_opens(root: Path) -> dict[date, Decimal]:
    """NIFTY 50's opens in HAT's own index store (`root/index_daily`); none without it."""
    path = root / DATASET_INDEX
    if not path.is_dir():
        return {}
    table = ds.dataset(path, format="parquet", partitioning="hive").to_table(
        columns=["date", "open"], filter=pc.field("index_name") == REGIME_INDEX
    )
    return {
        date.fromisoformat(str(d)[:10]): Decimal(o)
        for d, o in zip(table["date"].to_pylist(), table["open"].to_pylist(), strict=True)
        if o is not None and o > 0
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _span(days: Iterable[date]) -> list[str]:
    found = sorted(days)
    return [found[0].isoformat(), found[-1].isoformat()] if found else []


def write(root: Path, build: Path, kotak: Path) -> dict[str, Any]:
    """Write the store under `root/etf/` from the overlay build directory `build` and the raw
    Kotak directory `kotak` (`<SYMBOL>/5min/*.json`); its manifest."""
    out = root / ETF_DIR
    out.mkdir(parents=True, exist_ok=True)
    daily = [r for r in _read_csv(build / "etf_daily_adjusted.csv") if r["symbol"] in ASSETS]
    factors: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for r in daily:
        factors[r["symbol"]][date.fromisoformat(r["date"])] = exact(r["factor"]) or Decimal(0)
    pq.write_table(
        pa.table(
            {
                "symbol": [r["symbol"] for r in daily],
                "date": [date.fromisoformat(r["date"]) for r in daily],
                **{p: pa.array([e4(r[p]) for r in daily], pa.int64()) for p in _PRICES},
                "volume": pa.array([_volume(r["volume"]) for r in daily], pa.int64()),
                "factor": [r["factor"] for r in daily],
            }
        ),
        out / _DAILY,
    )
    bars: list[tuple[str, int, int, int, int, int, int]] = []
    for symbol in ASSETS:
        for path in sorted((kotak / symbol / "5min").glob("*.json")):
            candles = json.loads(path.read_text(encoding="utf-8"))["candles"]
            bars += five_min_rows(symbol, candles, factors[symbol])
    unique = {(b[0], b[1]): b for b in reversed(bars)}  # across files too: the first read wins
    bars = [unique[k] for k in sorted(unique)]
    columns = list(zip(*bars, strict=True)) if bars else [()] * 7
    pq.write_table(
        pa.table(
            {
                "symbol": list(columns[0]),
                "start": pa.array(
                    [datetime.fromtimestamp(s, UTC) for s in columns[1]], pa.timestamp("s", "UTC")
                ),
                **{p: pa.array(columns[2 + n], pa.int64()) for n, p in enumerate(_PRICES)},
                "volume": pa.array(columns[6], pa.int64()),
            }
        ),
        out / _BARS_5M,
    )
    tr = _read_csv(build / "liquidbees_tr.csv")
    pq.write_table(
        pa.table(
            {
                "date": [date.fromisoformat(r["date"]) for r in tr],
                "tr_index": [r["tr_index"] for r in tr],
                "basis": [r["basis"] for r in tr],
            }
        ),
        out / _LIQUID_TR,
    )
    names = {REGIME_INDEX, *REFERENCES}
    indices = [r for r in _read_csv(build / "index_daily.csv") if r["index"] in names]
    opens = _index_opens(root)
    pq.write_table(
        pa.table(
            {
                "index": [r["index"] for r in indices],
                "date": [date.fromisoformat(r["date"]) for r in indices],
                "close": [r["close"] for r in indices],
                "open": pa.array(
                    [
                        str(found)
                        if r["index"] == REGIME_INDEX
                        and (found := opens.get(date.fromisoformat(r["date"]))) is not None
                        else None
                        for r in indices
                    ],
                    pa.string(),
                ),
            }
        ),
        out / _INDICES,
    )
    bar_days: dict[str, set[date]] = defaultdict(set)
    for b in bars:
        bar_days[b[0]].add(datetime.fromtimestamp(b[1], IST).date())
    found = {
        "build": str(build.name if build.name != "build" else build.parent.name),
        "files": {name: _digest(out / name) for name in (_DAILY, _BARS_5M, _LIQUID_TR, _INDICES)},
        "daily": {s: _span(factors[s]) for s in ASSETS},
        "bars_5m": {s: {"days": len(bar_days[s]), "span": _span(bar_days[s])} for s in ASSETS},
        "liquid_tr": {
            "span": _span(date.fromisoformat(r["date"]) for r in tr),
            "proxy": [
                [a.isoformat(), b.isoformat()]
                for a, b in proxy_ranges((date.fromisoformat(r["date"]), r["basis"]) for r in tr)
            ],
        },
        "indices": {
            n: _span(date.fromisoformat(r["date"]) for r in indices if r["index"] == n)
            for n in sorted(names)
        },
    }
    (out / _MANIFEST).write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    gaps = open_gaps(load(root, date.min, date.max))
    found["open_gaps"] = {
        "pct": str(GAP_PCT),
        **{
            s: {
                "count": len(v),
                "estimated": sum(g["estimated"] == "true" for g in v),
                "days": [g["date"] for g in v],
            }
            for s, v in gaps.items()
        },
    }
    (out / _MANIFEST).write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    return found


def manifest(root: Path) -> dict[str, Any] | None:
    """The store's manifest; None without a store."""
    path = root / ETF_DIR / _MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def check_files(base: Path, found: Mapping[str, Any]) -> None:
    """Each data file under `base` against the sha256 its manifest `found` records:
    `etf_store_digest_mismatch` for a file missing, changed, or not recorded."""
    recorded = found.get("files")
    if not isinstance(recorded, Mapping) or set(recorded) != {
        _DAILY,
        _BARS_5M,
        _LIQUID_TR,
        _INDICES,
    }:
        raise EtfDataError("etf_store_digest_mismatch")
    for name, digest in recorded.items():
        path = base / name
        if not path.is_file() or _digest(path) != digest:
            raise EtfDataError("etf_store_digest_mismatch")


def _decimal(text: str) -> Decimal:
    number = exact(text)
    if number is None:
        raise EtfDataError("etf_store_bad_value")
    return number


def load(root: Path, start: date, end: date) -> EtfData:
    """The overlay's data from `start` to `end` (the daily series and indices from their
    beginning, the regime rule's SMA reading back past `start`); `etf_store_missing` without
    the store, `etf_store_digest_mismatch` when a file is not the one its manifest records."""
    base = root / ETF_DIR
    found = manifest(root)
    if found is None:
        raise EtfDataError("etf_store_missing")
    check_files(base, found)
    daily: dict[str, dict[date, EtfBar]] = defaultdict(dict)
    for r in pq.read_table(base / _DAILY).to_pylist():
        if r["date"] <= end:
            daily[r["symbol"]][r["date"]] = EtfBar(*(_price(r[p]) for p in _PRICES))
    bars_5m: dict[str, dict[date, list[tuple[datetime, EtfBar]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in pq.read_table(base / _BARS_5M).to_pylist():
        at: datetime = r["start"]
        day = at.astimezone(IST).date()
        if start <= day <= end:
            bars_5m[r["symbol"]][day].append((at, EtfBar(*(_price(r[p]) for p in _PRICES))))
    tr = [r for r in pq.read_table(base / _LIQUID_TR).to_pylist() if r["date"] <= end]
    indices: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    opens: dict[date, Decimal] = {}
    for r in pq.read_table(base / _INDICES).to_pylist():
        if r["date"] <= end:
            indices[r["index"]].append((r["date"], _decimal(r["close"])))
            if r["index"] == REGIME_INDEX and r.get("open"):
                opens[r["date"]] = _decimal(r["open"])
    return EtfData(
        daily={s: dict(daily[s]) for s in ASSETS},
        bars_5m={s: {d: sorted(v) for d, v in bars_5m[s].items()} for s in ASSETS},
        liquid_tr={r["date"]: _decimal(r["tr_index"]) for r in tr},
        proxy_ranges=proxy_ranges((r["date"], r["basis"]) for r in tr),
        regime_closes=sorted(indices.get(REGIME_INDEX, [])),
        references={n: sorted(indices.get(n, [])) for n in REFERENCES},
        regime_opens=opens,
    )

"""
Phase-1 data consolidation pipeline for:
1) All EQ stocks OHLCV (Jan 2025 onward)
2) NIFTY50 monthly weights and daily membership
3) Corporate-action adjusted prices (split/bonus)
4) Daily stock and universe snapshots
5) Metadata manifests/checksums and DB publish indexes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
from pypdf import PdfReader
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data_system"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import (  # noqa: E402
    DatasetArtifact,
    DatasetRun,
    SessionLocal,
    SnapshotIndexStock,
    SnapshotIndexUniverse,
)
from backend.app.services.symbol_master import symbol_master  # noqa: E402

PHASE_START_DATE = date(2025, 1, 1)
DEFAULT_INDEX_WEIGHTS_SOURCE = DATA_ROOT / "01_sources" / "nse_index_weights_pdf"
DEFAULT_UNIVERSES = ["NIFTY50", "BANKNIFTY"]
UNIVERSE_SPECS: dict[str, dict[str, Any]] = {
    "NIFTY50": {
        "slug": "nifty50",
        "csv_slug": "nifty50",
        "expected_count": 50,
        "allow_pdf_fallback": True,
        "pdf_pattern": "NIFTY_50_*.pdf",
    },
    "BANKNIFTY": {
        "slug": "banknifty",
        "csv_slug": "banknifty",
        "expected_count": None,
        "allow_pdf_fallback": False,
        "pdf_pattern": None,
    },
}
MONTH_ABBR = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
NIFTY50_LINE_RE = re.compile(r"^([A-Z0-9&\-.]+)\s+.+\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)$")


@dataclass
class Phase1Paths:
    project_root: Path
    phase1_root: Path
    sources_root: Path
    raw_root: Path
    staged_root: Path
    curated_root: Path
    metadata_root: Path
    source_bhavcopy: Path
    source_corporate_actions: Path
    source_index_weights: Path
    source_manifest: Path
    checksums: Path
    data_contract: Path
    run_log: Path
    legacy_inventory: Path
    validation_report: Path
    anomaly_report: Path
    corp_action_audit: Path
    banknifty_anomaly_report: Path


def _ensure_dirs(paths: list[Path]) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _iter_months(start: date, end: date) -> list[date]:
    out: list[date] = []
    m = _month_start(start)
    while m <= end:
        out.append(m)
        m = _next_month(m)
    return out


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _month_from_filename(filename: str) -> date | None:
    m = re.search(r"_([A-Za-z]{3})(\d{4})\.pdf$", filename)
    if not m:
        return None
    month_token = m.group(1).upper()
    year = int(m.group(2))
    month = MONTH_ABBR.get(month_token)
    if not month:
        return None
    return date(year, month, 1)


def _month_raw_dir_candidates(value: date) -> list[str]:
    mon = value.strftime("%b")
    year = value.year
    return [
        f"{year:04d}_{value.month:02d}",
        f"{mon.lower()}{year}",
        f"{mon}{year}",
        f"{mon.upper()}{year}",
    ]


def _build_paths(project_root: Path) -> Phase1Paths:
    phase1_root = project_root / "data_system"
    sources_root = phase1_root / "01_sources"
    raw_root = sources_root
    staged_root = phase1_root / "03_staging" / "phase1"
    curated_root = phase1_root / "04_curated" / "phase1"
    metadata_root = phase1_root / "05_metadata" / "phase1"
    source_bhavcopy = raw_root / "nse_bhavcopy"
    source_corporate_actions = raw_root / "nse_corporate_actions"
    source_index_weights = raw_root / "nse_index_weights_pdf"

    _ensure_dirs(
        [
            phase1_root,
            sources_root,
            raw_root,
            staged_root,
            curated_root,
            metadata_root,
            source_bhavcopy,
            source_corporate_actions,
            source_index_weights,
        ]
    )

    return Phase1Paths(
        project_root=project_root,
        phase1_root=phase1_root,
        sources_root=sources_root,
        raw_root=raw_root,
        staged_root=staged_root,
        curated_root=curated_root,
        metadata_root=metadata_root,
        source_bhavcopy=source_bhavcopy,
        source_corporate_actions=source_corporate_actions,
        source_index_weights=source_index_weights,
        source_manifest=metadata_root / "source_manifest.json",
        checksums=metadata_root / "checksums.json",
        data_contract=metadata_root / "data_contract.json",
        run_log=metadata_root / "run_log.jsonl",
        legacy_inventory=metadata_root / "legacy_inventory.json",
        validation_report=metadata_root / "validation_report.json",
        anomaly_report=metadata_root / "anomaly_report.json",
        corp_action_audit=metadata_root / "corporate_action_audit.csv",
        banknifty_anomaly_report=metadata_root / "banknifty_anomaly_report.json",
    )


class Phase1Builder:
    def __init__(self, asof: date, mode: str, start_date: date, universes: list[str]) -> None:
        self.asof = asof
        self.mode = mode
        self.start_date = start_date
        if self.start_date > self.asof:
            raise ValueError("start_date cannot be after asof date")
        self.universes = [u.upper() for u in universes]
        self.paths = _build_paths(PROJECT_ROOT)
        self.run_id = f"phase1-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.issues: list[str] = []
        self.summary: dict[str, Any] = {}
        self._write_data_contract()

    def _log(self, step: str, status: str, details: dict[str, Any] | None = None) -> None:
        payload = {
            "run_id": self.run_id,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "step": step,
            "status": status,
            "asof": self.asof.isoformat(),
            "start_date": self.start_date.isoformat(),
            "mode": self.mode,
            "details": details or {},
        }
        _append_jsonl(self.paths.run_log, payload)

    def _write_data_contract(self) -> None:
        contract = {
            "version": "phase1-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "datasets": {
                "equity_ohlcv": ["trade_date", "symbol", "open", "high", "low", "close", "volume", "turnover", "source"],
                "nifty50_membership_daily": ["date", "symbol", "in_universe", "source_file", "source_priority"],
                "nifty50_weights_monthly": ["month", "symbol", "weight", "source_file", "source_priority", "parse_confidence"],
                "snapshot_stock_daily": [
                    "date", "symbol", "open", "high", "low", "close", "volume", "turnover",
                    "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume", "corporate_action_flag",
                ],
                "snapshot_nifty50_daily": [
                    "date", "universe_id", "symbol", "in_universe", "weight", "open", "high",
                    "low", "close", "adj_close", "volume", "corporate_action_flag",
                ],
                "banknifty_weights_monthly": ["month", "symbol", "weight", "source_file", "source_priority", "parse_confidence"],
                "banknifty_membership_daily": ["date", "symbol", "in_universe", "source_file", "source_priority"],
                "snapshot_banknifty_daily": [
                    "date", "universe_id", "symbol", "in_universe", "weight", "open", "high",
                    "low", "close", "adj_close", "volume", "corporate_action_flag",
                ],
            },
        }
        _json_dump(self.paths.data_contract, contract)

    def run_all(self) -> None:
        self.ingest_sources()
        for universe_id in self.universes:
            self.build_universe(universe_id)
        self.build_equity()
        self.apply_corp_actions()
        self.build_snapshots()
        valid = self.validate()
        if not valid:
            raise RuntimeError("Validation failed. Check validation_report.json for details.")
        self.publish()

    def ingest_sources(self) -> None:
        step = "ingest-sources"
        self._log(step, "started")
        copied = {"bhavcopy": 0, "corporate_actions": 0, "index_weights": 0}

        index_weights_source = Path(
            os.getenv("PHASE1_INDEX_WEIGHTS_SRC", str(DEFAULT_INDEX_WEIGHTS_SOURCE))
        )
        if index_weights_source.exists() and index_weights_source.resolve() != self.paths.source_index_weights.resolve():
            for src in sorted(index_weights_source.rglob("NIFTY_50_*.pdf")):
                month = _month_from_filename(src.name)
                if month is None or month < _month_start(self.start_date) or month > _month_start(self.asof):
                    continue
                rel = src.relative_to(index_weights_source)
                dst = self.paths.source_index_weights / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or _sha256(dst) != _sha256(src):
                    shutil.copy2(src, dst)
                    copied["index_weights"] += 1

        manifest_entries = []
        for kind, root in [
            ("bhavcopy", self.paths.source_bhavcopy),
            ("corporate_actions", self.paths.source_corporate_actions),
            ("index_weights", self.paths.source_index_weights),
        ]:
            for f in sorted(root.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(self.paths.phase1_root).as_posix()
                manifest_entries.append(
                    {
                        "category": kind,
                        "relative_path": rel,
                        "size_bytes": f.stat().st_size,
                        "sha256": _sha256(f),
                        "modified_at_utc": datetime.fromtimestamp(f.stat().st_mtime, UTC).isoformat(),
                    }
                )

        _json_dump(
            self.paths.source_manifest,
            {
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "phase_start": self.start_date.isoformat(),
                "asof": self.asof.isoformat(),
                "entries": manifest_entries,
                "summary": {"copied_counts": copied, "total_entries": len(manifest_entries)},
            },
        )

        _json_dump(
            self.paths.legacy_inventory,
            {
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "items": [
                    {"path": "data_platform/pipelines/phase1_build.py", "status": "active", "reason": "canonical Phase-1 orchestrator"},
                    {
                        "path": "archive/data-legacy/2026-02-19/data_platform/pipelines/consolidate.py",
                        "status": "archived",
                        "reason": "references deprecated non-canonical raw source folders",
                    },
                    {
                        "path": "archive/data-legacy/2026-02-19/data_platform/processors/master_store_builder.py",
                        "status": "archived",
                        "reason": "legacy merge flow for deprecated source topology",
                    },
                    {
                        "path": "archive/data-legacy/2026-02-19/data_platform/pipelines/load_index_membership.py",
                        "status": "archived",
                        "reason": "fallback logic with placeholder intervals",
                    },
                    {"path": "scripts/README.md", "status": "legacy", "reason": "documents deprecated yahoo pipeline scripts"},
                ],
            },
        )

        self.summary["ingest"] = {"copied": copied, "manifest_entries": len(manifest_entries)}
        self._log(step, "completed", self.summary["ingest"])

    def build_equity(self) -> None:
        step = "build-equity"
        self._log(step, "started")
        equity_rows: list[pd.DataFrame] = []

        eq_symbols = set()
        eq_list_path = self.paths.project_root / "data_system" / "05_metadata" / "reference" / "equity_list.csv"
        if eq_list_path.exists():
            eq_list = pd.read_csv(eq_list_path)
            if "SYMBOL" in eq_list.columns and "SERIES" in eq_list.columns:
                eq_symbols.update(eq_list[eq_list["SERIES"].astype(str).str.upper() == "EQ"]["SYMBOL"].astype(str).str.upper())

        # Add symbols from ingested bhavcopy for in-range dates.
        for bhav_file in sorted(self.paths.source_bhavcopy.glob("*.csv")):
            try:
                bhav = pd.read_csv(bhav_file)
            except Exception:
                continue
            bhav.columns = [c.strip().upper() for c in bhav.columns]
            if "SYMBOL" not in bhav.columns or "SERIES" not in bhav.columns:
                continue
            if "DATE1" in bhav.columns:
                bhav["trade_date"] = pd.to_datetime(bhav["DATE1"], errors="coerce", dayfirst=True).dt.date
                bhav = bhav[(bhav["trade_date"] >= self.start_date) & (bhav["trade_date"] <= self.asof)]
            eq_symbols.update(bhav[bhav["SERIES"].astype(str).str.upper() == "EQ"]["SYMBOL"].astype(str).str.upper())

        # Include bhavcopy snapshots for freshest phase-1 dates.
        for bhav_file in sorted(self.paths.source_bhavcopy.glob("*.csv")):
            try:
                bhav = pd.read_csv(bhav_file)
            except Exception:
                continue
            bhav.columns = [c.strip().upper() for c in bhav.columns]
            required = {"SYMBOL", "SERIES", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"}
            if not required.issubset(set(bhav.columns)):
                continue
            if "DATE1" in bhav.columns:
                trade_date = pd.to_datetime(bhav["DATE1"], errors="coerce", dayfirst=True).dt.date
            else:
                m = re.search(r"(\d{2})(\d{2})(\d{4})", bhav_file.name)
                if not m:
                    continue
                fallback_date = datetime.strptime(m.group(0), "%d%m%Y").date()
                trade_date = pd.Series([fallback_date] * len(bhav))
            part = pd.DataFrame(
                {
                    "trade_date": trade_date,
                    "symbol": bhav["SYMBOL"].astype(str).str.upper().str.strip(),
                    "open": pd.to_numeric(bhav["OPEN_PRICE"], errors="coerce"),
                    "high": pd.to_numeric(bhav["HIGH_PRICE"], errors="coerce"),
                    "low": pd.to_numeric(bhav["LOW_PRICE"], errors="coerce"),
                    "close": pd.to_numeric(bhav["CLOSE_PRICE"], errors="coerce"),
                    "volume": pd.to_numeric(bhav["TTL_TRD_QNTY"], errors="coerce"),
                    "turnover": pd.to_numeric(bhav.get("TURNOVER_LACS", np.nan), errors="coerce") * 100000,
                    "source": "bhavcopy",
                    "series": bhav["SERIES"].astype(str).str.upper().str.strip(),
                }
            )
            part = part[
                (part["series"] == "EQ")
                & (part["trade_date"] >= self.start_date)
                & (part["trade_date"] <= self.asof)
            ].drop(columns=["series"])
            if not part.empty:
                equity_rows.append(part)

        if not equity_rows:
            raise RuntimeError("No equity rows found for Phase-1 date range.")

        equity_rows = [df for df in equity_rows if not df.empty]
        equity_df = pd.concat(equity_rows, ignore_index=True)
        symbol_map = {s: symbol_master.to_db(s) for s in equity_df["symbol"].dropna().unique()}
        equity_df["symbol"] = equity_df["symbol"].map(symbol_map).fillna(equity_df["symbol"])

        equity_df = equity_df.sort_values(["trade_date", "symbol"])
        equity_df = equity_df.drop_duplicates(subset=["trade_date", "symbol"], keep="last")
        equity_df = equity_df.dropna(subset=["trade_date", "symbol", "open", "high", "low", "close"])
        equity_df["volume"] = pd.to_numeric(equity_df["volume"], errors="coerce").fillna(0).astype("int64")
        equity_df = equity_df[
            ["trade_date", "symbol", "open", "high", "low", "close", "volume", "turnover", "source"]
        ].sort_values(["trade_date", "symbol"])

        out = self.paths.curated_root / "equity_ohlcv.parquet"
        equity_df.to_parquet(out, index=False)

        self.summary["equity"] = {
            "rows": int(len(equity_df)),
            "symbols": int(equity_df["symbol"].nunique()),
            "sources": equity_df["source"].value_counts().to_dict(),
            "min_date": equity_df["trade_date"].min().isoformat(),
            "max_date": equity_df["trade_date"].max().isoformat(),
            "artifact": str(out),
        }
        self._log(step, "completed", self.summary["equity"])

    def _parse_nifty50_pdf(self, pdf_path: Path) -> tuple[pd.DataFrame, float]:
        rows: list[dict[str, Any]] = []
        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("Symbol ") or line.startswith("Constituents of NIFTY 50"):
                    continue
                m = NIFTY50_LINE_RE.match(line)
                if not m:
                    continue
                symbol = symbol_master.to_db(m.group(1).upper())
                weight = float(m.group(3))
                rows.append({"symbol": symbol, "weight": weight})
        if not rows:
            return pd.DataFrame(columns=["symbol", "weight"]), 0.0
        df = pd.DataFrame(rows).drop_duplicates(subset=["symbol"], keep="last")
        confidence = min(1.0, len(df) / 50.0)
        return df, confidence

    def _fallback_nifty50_from_csv(self, month: date) -> pd.DataFrame:
        df, _ = self._load_nifty50_from_monthly_raw(month)
        return df

    def _load_nifty50_from_monthly_raw(self, month: date) -> tuple[pd.DataFrame, str]:
        return self._load_universe_from_monthly_raw("nifty50", month)

    def _load_universe_from_monthly_raw(self, universe_slug: str, month: date) -> tuple[pd.DataFrame, str]:
        roots = [
            self.paths.project_root / "data_system" / "03_universe" / "monthly_universe_raw",
        ]
        for root in roots:
            if not root.exists():
                continue
            for token in _month_raw_dir_candidates(month):
                csv_path = root / token / f"{universe_slug}.csv"
                if not csv_path.exists():
                    continue
                try:
                    df = pd.read_csv(csv_path)
                except Exception:
                    continue
                if "symbol" not in df.columns or "weight" not in df.columns:
                    continue
                out = pd.DataFrame(
                    {
                        "symbol": df["symbol"].astype(str).str.upper().map(symbol_master.to_db),
                        "weight": pd.to_numeric(df["weight"], errors="coerce"),
                    }
                ).dropna(subset=["symbol", "weight"])
                return out, csv_path.as_posix()
        return pd.DataFrame(columns=["symbol", "weight"]), ""

    @staticmethod
    def _normalize_universe_slug(universe_id: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", universe_id.lower())

    def _get_universe_spec(self, universe_id: str) -> dict[str, Any]:
        normalized = universe_id.upper()
        if normalized in UNIVERSE_SPECS:
            return UNIVERSE_SPECS[normalized]
        slug = self._normalize_universe_slug(normalized)
        return {
            "slug": slug,
            "csv_slug": slug,
            "expected_count": None,
            "allow_pdf_fallback": False,
            "pdf_pattern": None,
        }

    def build_universe(self, universe_id: str) -> None:
        spec = self._get_universe_spec(universe_id)
        slug = spec["slug"]
        step = "build-universe"
        self._log(step, "started")

        pdf_path_by_month: dict[str, Path] = {}
        pdf_pattern = spec.get("pdf_pattern")
        if spec.get("allow_pdf_fallback") and pdf_pattern:
            for pdf in sorted(self.paths.source_index_weights.rglob(pdf_pattern)):
                month = _month_from_filename(pdf.name)
                if month is None:
                    continue
                if month < _month_start(self.start_date) or month > _month_start(self.asof):
                    continue
                pdf_path_by_month[_month_key(month)] = pdf

        target_months = _iter_months(self.start_date, _month_start(self.asof))
        monthly_rows: list[pd.DataFrame] = []
        anomalies: list[dict[str, Any]] = []

        for month in target_months:
            mkey = _month_key(month)
            source_file = ""
            source_priority = "none"
            parse_conf = 0.0
            month_df = pd.DataFrame(columns=["symbol", "weight"])

            csv_ref, csv_source_path = self._load_universe_from_monthly_raw(spec["csv_slug"], month)
            csv_ref = csv_ref.drop_duplicates(subset=["symbol"], keep="last")
            if not csv_ref.empty:
                csv_weight_sum = float(pd.to_numeric(csv_ref["weight"], errors="coerce").sum())
                if csv_weight_sum <= 1.5:
                    csv_ref["weight"] = csv_ref["weight"] * 100.0
                month_df = csv_ref
                source_file = csv_source_path
                source_priority = "repo_raw_csv_primary"
                parse_conf = 0.95

            if month_df.empty and mkey in pdf_path_by_month:
                parsed_df, conf = self._parse_nifty50_pdf(pdf_path_by_month[mkey])
                parsed_df = parsed_df.drop_duplicates(subset=["symbol"], keep="last")
                if not parsed_df.empty:
                    month_df = parsed_df
                    source_file = str(pdf_path_by_month[mkey])
                    source_priority = "downloads_pdf_fallback"
                    parse_conf = conf

            if month_df.empty:
                anomalies.append({"month": mkey, "type": "missing_month", "message": "No source available for month"})
                continue

            month_df = month_df.drop_duplicates(subset=["symbol"], keep="last")
            weight_sum = float(pd.to_numeric(month_df["weight"], errors="coerce").sum())
            if weight_sum <= 1.5:
                month_df["weight"] = month_df["weight"] * 100.0
                weight_sum = float(month_df["weight"].sum())

            expected_count = spec.get("expected_count")
            if expected_count is not None and len(month_df) != expected_count:
                anomalies.append(
                    {
                        "month": mkey,
                        "type": "constituent_count",
                        "message": f"Expected {expected_count} constituents, found {len(month_df)}",
                        "source": source_priority,
                    }
                )
            if abs(weight_sum - 100.0) > 1.5:
                anomalies.append(
                    {
                        "month": mkey,
                        "type": "weight_sum",
                        "message": f"Weight sum {weight_sum:.4f} deviates from 100",
                        "source": source_priority,
                    }
                )

            month_payload = month_df.copy()
            month_payload["month"] = month
            month_payload["source_file"] = source_file
            month_payload["source_priority"] = source_priority
            month_payload["parse_confidence"] = parse_conf

            monthly_rows.append(month_payload[["month", "symbol", "weight", "source_file", "source_priority", "parse_confidence"]])

        if not monthly_rows:
            raise RuntimeError(f"Failed to build {universe_id} monthly weights.")

        monthly_df = pd.concat(monthly_rows, ignore_index=True).sort_values(["month", "symbol"])
        monthly_out = self.paths.curated_root / f"{slug}_weights_monthly.parquet"
        monthly_df.to_parquet(monthly_out, index=False)

        daily_rows: list[pd.DataFrame] = []
        months_sorted = sorted(monthly_df["month"].drop_duplicates().tolist())
        for m in months_sorted:
            month_start = pd.Timestamp(m).date().replace(day=1)
            next_m = _next_month(month_start)
            day_end = min(next_m - timedelta(days=1), self.asof)
            symbols = monthly_df[monthly_df["month"] == m][["symbol", "source_file", "source_priority"]]
            if symbols.empty:
                continue
            dates = pd.date_range(month_start, day_end, freq="D")
            expanded = symbols.loc[symbols.index.repeat(len(dates))].reset_index(drop=True)
            expanded["date"] = dates.tolist() * len(symbols)
            expanded["in_universe"] = True
            daily_rows.append(expanded[["date", "symbol", "in_universe", "source_file", "source_priority"]])

        daily_df = pd.concat(daily_rows, ignore_index=True).sort_values(["date", "symbol"])
        daily_out = self.paths.curated_root / f"{slug}_membership_daily.parquet"
        daily_df.to_parquet(daily_out, index=False)

        _json_dump(
            self.paths.metadata_root / f"{slug}_anomaly_report.json",
            {
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "run_id": self.run_id,
                "anomalies": anomalies,
            },
        )

        self.summary[slug] = {
            "universe_id": universe_id.upper(),
            "monthly_rows": int(len(monthly_df)),
            "daily_rows": int(len(daily_df)),
            "months_covered": int(monthly_df["month"].nunique()),
            "anomaly_count": len(anomalies),
            "monthly_artifact": str(monthly_out),
            "daily_artifact": str(daily_out),
        }
        self._log(step, "completed", self.summary[slug])

    def build_nifty50(self) -> None:
        self.build_universe("NIFTY50")

    def build_banknifty(self) -> None:
        self.build_universe("BANKNIFTY")

    @staticmethod
    def _parse_corp_action_factor(purpose: str) -> tuple[str | None, float | None]:
        text = str(purpose).lower()
        if "split" in text and "fv" in text:
            nums = list(map(int, re.findall(r"\d+", text)))
            if len(nums) >= 2 and nums[1] != 0:
                return "split", nums[0] / nums[1]
        m = re.search(r"bonus.*?(\d+)\s*:\s*(\d+)", text)
        if m:
            a, b = map(int, m.groups())
            if b != 0:
                return "bonus", (a + b) / b
        return None, None

    def apply_corp_actions(self) -> None:
        step = "apply-corp-actions"
        self._log(step, "started")

        equity_path = self.paths.curated_root / "equity_ohlcv.parquet"
        if not equity_path.exists():
            raise RuntimeError("equity_ohlcv.parquet missing. Run build-equity first.")
        equity = pd.read_parquet(equity_path)
        equity["trade_date"] = pd.to_datetime(equity["trade_date"], errors="coerce").dt.date

        ca_files = sorted(self.paths.source_corporate_actions.glob("CF-CA-equities*.csv"))
        if not ca_files:
            raise RuntimeError("Corporate actions source file missing. Run ingest-sources first.")
        ca_df = pd.read_csv(ca_files[-1])
        required = {"SYMBOL", "EX-DATE", "PURPOSE"}
        if not required.issubset(set(ca_df.columns)):
            raise RuntimeError("Corporate actions file schema mismatch.")

        parsed = ca_df["PURPOSE"].apply(self._parse_corp_action_factor)
        ca_df["action_type"] = [x[0] for x in parsed]
        ca_df["factor"] = [x[1] for x in parsed]
        ca_df = ca_df.dropna(subset=["action_type", "factor"]).copy()
        ca_df["ex_date"] = pd.to_datetime(ca_df["EX-DATE"], errors="coerce", dayfirst=True).dt.date
        ca_df["symbol"] = ca_df["SYMBOL"].astype(str).str.upper().map(symbol_master.to_db)
        ca_df = ca_df[
            (ca_df["ex_date"] > self.start_date)
            & (ca_df["ex_date"] <= self.asof)
            & (ca_df["factor"] > 0)
        ][["symbol", "ex_date", "action_type", "factor"]]

        adjusted = equity.copy()
        adjusted[["open", "high", "low", "close", "volume"]] = adjusted[
            ["open", "high", "low", "close", "volume"]
        ].apply(pd.to_numeric, errors="coerce")
        adjusted["volume"] = adjusted["volume"].astype(float)

        audit_rows: list[dict[str, Any]] = []
        for symbol, grp in ca_df.groupby("symbol"):
            mask_symbol = adjusted["symbol"] == symbol
            if not mask_symbol.any():
                continue
            sym_df = adjusted.loc[mask_symbol].copy()
            sym_df["volume"] = sym_df["volume"].astype(float)
            factor_product = 1.0
            for row in grp.sort_values("ex_date", ascending=False).itertuples():
                factor_product *= float(row.factor)
                mask_date = sym_df["trade_date"] < row.ex_date
                sym_df.loc[mask_date, ["open", "high", "low", "close"]] = (
                    sym_df.loc[mask_date, ["open", "high", "low", "close"]] / float(row.factor)
                )
                sym_df.loc[mask_date, "volume"] = sym_df.loc[mask_date, "volume"] * float(row.factor)
            adjusted.loc[mask_symbol, ["open", "high", "low", "close", "volume"]] = sym_df[
                ["open", "high", "low", "close", "volume"]
            ]
            audit_rows.append(
                {
                    "symbol": symbol,
                    "events": int(len(grp)),
                    "first_ex_date": grp["ex_date"].min(),
                    "last_ex_date": grp["ex_date"].max(),
                    "factor_product": factor_product,
                }
            )

        adjusted["volume"] = adjusted["volume"].fillna(0).astype("int64")
        adjusted_out = self.paths.curated_root / "equity_ohlcv_adj.parquet"
        adjusted.to_parquet(adjusted_out, index=False)

        audit_df = pd.DataFrame(audit_rows).sort_values("symbol") if audit_rows else pd.DataFrame()
        audit_df.to_csv(self.paths.corp_action_audit, index=False)

        self.summary["corp_actions"] = {
            "events_used": int(len(ca_df)),
            "symbols_adjusted": int(len(audit_df)),
            "artifact": str(adjusted_out),
            "audit": str(self.paths.corp_action_audit),
        }
        self._log(step, "completed", self.summary["corp_actions"])

    def build_snapshots(self) -> None:
        step = "build-snapshots"
        self._log(step, "started")

        raw_path = self.paths.curated_root / "equity_ohlcv.parquet"
        adj_path = self.paths.curated_root / "equity_ohlcv_adj.parquet"
        for p in [raw_path, adj_path]:
            if not p.exists():
                raise RuntimeError(f"Required input missing for snapshots: {p}")

        raw = pd.read_parquet(raw_path)
        adj = pd.read_parquet(adj_path)
        raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce").dt.date
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.date

        merged = raw.merge(
            adj[["trade_date", "symbol", "open", "high", "low", "close", "volume"]],
            on=["trade_date", "symbol"],
            how="left",
            suffixes=("", "_adj"),
        )
        merged["corporate_action_flag"] = (merged["close"] - merged["close_adj"]).abs() > 1e-9
        stock_snapshot = pd.DataFrame(
            {
                "date": merged["trade_date"].astype(str),
                "symbol": merged["symbol"],
                "open": merged["open"],
                "high": merged["high"],
                "low": merged["low"],
                "close": merged["close"],
                "volume": merged["volume"].astype("int64"),
                "turnover": merged["turnover"],
                "adj_open": merged["open_adj"],
                "adj_high": merged["high_adj"],
                "adj_low": merged["low_adj"],
                "adj_close": merged["close_adj"],
                "adj_volume": pd.to_numeric(merged["volume_adj"], errors="coerce").fillna(0).astype("int64"),
                "corporate_action_flag": merged["corporate_action_flag"],
            }
        ).sort_values(["date", "symbol"])

        stock_out = self.paths.curated_root / "snapshot_stock_daily.parquet"
        stock_snapshot.to_parquet(stock_out, index=False)

        universe_row_counts: dict[str, int] = {}
        for universe_id in self.universes:
            spec = self._get_universe_spec(universe_id)
            slug = spec["slug"]
            monthly_path = self.paths.curated_root / f"{slug}_weights_monthly.parquet"
            daily_membership_path = self.paths.curated_root / f"{slug}_membership_daily.parquet"
            if not monthly_path.exists() or not daily_membership_path.exists():
                continue

            monthly = pd.read_parquet(monthly_path)
            monthly["month"] = pd.to_datetime(monthly["month"], errors="coerce")
            monthly["month_key"] = monthly["month"].dt.strftime("%Y-%m")
            daily_membership = pd.read_parquet(daily_membership_path)
            daily_membership["date"] = pd.to_datetime(daily_membership["date"], errors="coerce")
            daily_membership["month_key"] = daily_membership["date"].dt.strftime("%Y-%m")
            daily_membership["date_str"] = daily_membership["date"].dt.strftime("%Y-%m-%d")

            uni = daily_membership.merge(
                monthly[["month_key", "symbol", "weight"]],
                on=["month_key", "symbol"],
                how="left",
            )
            uni = uni.merge(
                stock_snapshot[
                    [
                        "date",
                        "symbol",
                        "open",
                        "high",
                        "low",
                        "close",
                        "adj_close",
                        "volume",
                        "corporate_action_flag",
                    ]
                ],
                left_on=["date_str", "symbol"],
                right_on=["date", "symbol"],
                how="left",
            )
            universe_snapshot = pd.DataFrame(
                {
                    "date": uni["date_str"],
                    "universe_id": universe_id.upper(),
                    "symbol": uni["symbol"],
                    "in_universe": uni["in_universe"],
                    "weight": uni["weight"],
                    "open": uni["open"],
                    "high": uni["high"],
                    "low": uni["low"],
                    "close": uni["close"],
                    "adj_close": uni["adj_close"],
                    "volume": uni["volume"],
                    "corporate_action_flag": uni["corporate_action_flag"],
                }
            ).sort_values(["date", "symbol"])

            universe_out = self.paths.curated_root / f"snapshot_{slug}_daily.parquet"
            universe_snapshot.to_parquet(universe_out, index=False)
            universe_row_counts[universe_id.upper()] = int(len(universe_snapshot))

        self.summary["snapshots"] = {
            "stock_rows": int(len(stock_snapshot)),
            "universe_rows": universe_row_counts,
            "stock_artifact": str(stock_out),
        }
        self._log(step, "completed", self.summary["snapshots"])

    def validate(self) -> bool:
        step = "validate"
        self._log(step, "started")
        issues: list[str] = []
        warnings: list[str] = []
        metrics: dict[str, Any] = {}
        universe_snapshot_required_cols = {
            "date",
            "universe_id",
            "symbol",
            "in_universe",
            "weight",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "corporate_action_flag",
        }

        required_files = [
            self.paths.curated_root / "equity_ohlcv.parquet",
            self.paths.curated_root / "equity_ohlcv_adj.parquet",
            self.paths.curated_root / "snapshot_stock_daily.parquet",
        ]
        for universe_id in self.universes:
            spec = self._get_universe_spec(universe_id)
            slug = spec["slug"]
            required_files.extend(
                [
                    self.paths.curated_root / f"{slug}_weights_monthly.parquet",
                    self.paths.curated_root / f"{slug}_membership_daily.parquet",
                    self.paths.curated_root / f"snapshot_{slug}_daily.parquet",
                ]
            )
        missing = [str(f) for f in required_files if not f.exists()]
        if missing:
            issues.extend([f"missing_artifact:{m}" for m in missing])
            _json_dump(self.paths.validation_report, {"status": "failed", "issues": issues, "metrics": metrics})
            self._log(step, "failed", {"issues": issues})
            self.issues.extend(issues)
            return False

        eq = pd.read_parquet(self.paths.curated_root / "equity_ohlcv.parquet")
        dup_count = int(eq.duplicated(subset=["trade_date", "symbol"]).sum())
        metrics["equity_duplicates"] = dup_count
        if dup_count > 0:
            issues.append(f"equity_duplicate_keys:{dup_count}")
        eq_min = pd.to_datetime(eq["trade_date"], errors="coerce").min().date()
        if eq_min > self.start_date:
            issues.append(f"equity_min_date_after_phase_start:{eq_min.isoformat()}")
        metrics["equity_rows"] = int(len(eq))
        metrics["equity_symbols"] = int(eq["symbol"].nunique())

        for universe_id in self.universes:
            spec = self._get_universe_spec(universe_id)
            slug = spec["slug"]
            monthly = pd.read_parquet(self.paths.curated_root / f"{slug}_weights_monthly.parquet")
            month_stats = monthly.groupby("month").agg(cnt=("symbol", "count"), weight_sum=("weight", "sum")).reset_index()
            expected_count = spec.get("expected_count")
            if expected_count is not None:
                bad_count = month_stats[month_stats["cnt"] != expected_count]
                if not bad_count.empty:
                    warnings.append(f"{slug}_bad_count_months:{len(bad_count)}")
            bad_weight = month_stats[(month_stats["weight_sum"] < 98.5) | (month_stats["weight_sum"] > 101.5)]
            if not bad_weight.empty:
                warnings.append(f"{slug}_bad_weight_sum_months:{len(bad_weight)}")
            metrics[f"{slug}_months"] = int(month_stats.shape[0])

        stock_snap = pd.read_parquet(self.paths.curated_root / "snapshot_stock_daily.parquet")
        snap_dups = int(stock_snap.duplicated(subset=["date", "symbol"]).sum())
        metrics["snapshot_stock_duplicates"] = snap_dups
        if snap_dups > 0:
            issues.append(f"snapshot_stock_duplicate_keys:{snap_dups}")

        for universe_id in self.universes:
            spec = self._get_universe_spec(universe_id)
            slug = spec["slug"]
            universe_snap = pd.read_parquet(self.paths.curated_root / f"snapshot_{slug}_daily.parquet")
            snap_cols = set(universe_snap.columns)
            missing_cols = sorted(universe_snapshot_required_cols - snap_cols)
            if missing_cols:
                issues.append(f"snapshot_{slug}_missing_columns:{','.join(missing_cols)}")

            membership = pd.read_parquet(self.paths.curated_root / f"{slug}_membership_daily.parquet")
            membership["date"] = pd.to_datetime(membership["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            universe_snap["date"] = pd.to_datetime(universe_snap["date"], errors="coerce").dt.strftime("%Y-%m-%d")

            uni_dups = int(universe_snap.duplicated(subset=["date", "symbol"]).sum())
            metrics[f"snapshot_{slug}_duplicates"] = uni_dups
            if uni_dups > 0:
                issues.append(f"snapshot_{slug}_duplicate_keys:{uni_dups}")

            membership_keys = set(
                zip(membership["date"].astype(str), membership["symbol"].astype(str))
            )
            snapshot_keys = set(
                zip(universe_snap["date"].astype(str), universe_snap["symbol"].astype(str))
            )
            missing_keys = membership_keys - snapshot_keys
            metrics[f"snapshot_{slug}_missing_membership_keys"] = len(missing_keys)
            if missing_keys:
                issues.append(f"snapshot_{slug}_missing_membership_keys:{len(missing_keys)}")

            snap_dates = pd.to_datetime(universe_snap["date"], errors="coerce").dt.date
            if snap_dates.notna().any():
                if snap_dates.min() < self.start_date:
                    issues.append(f"snapshot_{slug}_min_date_before_start:{snap_dates.min().isoformat()}")
                if snap_dates.max() > self.asof:
                    issues.append(f"snapshot_{slug}_max_date_after_asof:{snap_dates.max().isoformat()}")

            in_uni_mask = universe_snap["in_universe"] == True
            missing_weight = int(universe_snap.loc[in_uni_mask, "weight"].isna().sum())
            missing_close = int(universe_snap.loc[in_uni_mask, "close"].isna().sum())
            metrics[f"snapshot_{slug}_missing_weight_rows"] = missing_weight
            metrics[f"snapshot_{slug}_missing_close_rows"] = missing_close
            if missing_weight > 0:
                issues.append(f"snapshot_{slug}_missing_weight_rows:{missing_weight}")
            if missing_close > 0:
                issues.append(f"snapshot_{slug}_missing_close_rows:{missing_close}")

        status = "passed" if not issues else "failed"
        report = {
            "status": status,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "asof": self.asof.isoformat(),
            "metrics": metrics,
            "issues": issues,
            "warnings": warnings,
        }
        _json_dump(self.paths.validation_report, report)
        self.issues.extend(issues)
        self.summary["validation"] = report
        self._log(step, status, {"issue_count": len(issues)})
        return not issues

    def publish(self) -> None:
        step = "publish"
        self._log(step, "started")
        artifact_paths = {
            "equity_ohlcv": self.paths.curated_root / "equity_ohlcv.parquet",
            "equity_ohlcv_adj": self.paths.curated_root / "equity_ohlcv_adj.parquet",
            "snapshot_stock_daily": self.paths.curated_root / "snapshot_stock_daily.parquet",
        }
        for universe_id in self.universes:
            spec = self._get_universe_spec(universe_id)
            slug = spec["slug"]
            universe_artifacts = {
                f"{slug}_weights_monthly": self.paths.curated_root / f"{slug}_weights_monthly.parquet",
                f"{slug}_membership_daily": self.paths.curated_root / f"{slug}_membership_daily.parquet",
                f"snapshot_{slug}_daily": self.paths.curated_root / f"snapshot_{slug}_daily.parquet",
            }
            for key, path in universe_artifacts.items():
                if path.exists():
                    artifact_paths[key] = path

        checksums = {
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "artifacts": [],
        }
        for name, path in artifact_paths.items():
            df = pd.read_parquet(path)
            date_col = next((c for c in ["trade_date", "date", "month"] if c in df.columns), None)
            if date_col is not None:
                min_date = pd.to_datetime(df[date_col], errors="coerce").min()
                max_date = pd.to_datetime(df[date_col], errors="coerce").max()
            else:
                min_date = pd.NaT
                max_date = pd.NaT
            checksums["artifacts"].append(
                {
                    "dataset_name": name,
                    "path": str(path),
                    "checksum": _sha256(path),
                    "row_count": int(len(df)),
                    "min_date": min_date.isoformat() if pd.notna(min_date) else None,
                    "max_date": max_date.isoformat() if pd.notna(max_date) else None,
                }
            )
        _json_dump(self.paths.checksums, checksums)
        manifest_hash = _sha256(self.paths.source_manifest) if self.paths.source_manifest.exists() else None

        db = SessionLocal()
        try:
            run = DatasetRun(
                run_id=self.run_id,
                asof_date=self.asof,
                mode=self.mode,
                status="running",
                source_manifest_hash=manifest_hash,
                details_json=json.dumps({"summary": self.summary}),
            )
            db.add(run)
            db.commit()

            for item in checksums["artifacts"]:
                row = DatasetArtifact(
                    run_id=self.run_id,
                    dataset_name=item["dataset_name"],
                    artifact_path=item["path"],
                    row_count=item["row_count"],
                    min_date=pd.to_datetime(item["min_date"]).date() if item["min_date"] else None,
                    max_date=pd.to_datetime(item["max_date"]).date() if item["max_date"] else None,
                    checksum=item["checksum"],
                    metadata_json=json.dumps({"published_at_utc": datetime.now(UTC).isoformat()}),
                )
                db.add(row)
            db.commit()

            stock_df = pd.read_parquet(artifact_paths["snapshot_stock_daily"], columns=["date", "symbol"]).drop_duplicates()
            stock_df["date"] = pd.to_datetime(stock_df["date"], errors="coerce").dt.date
            db.query(SnapshotIndexStock).filter(
                SnapshotIndexStock.snapshot_date >= self.start_date,
                SnapshotIndexStock.snapshot_date <= self.asof,
            ).delete(synchronize_session=False)
            db.commit()
            stock_records = [
                {
                    "snapshot_date": row.date,
                    "symbol": row.symbol,
                    "artifact_path": str(artifact_paths["snapshot_stock_daily"]),
                    "run_id": self.run_id,
                    "row_pointer": None,
                }
                for row in stock_df.itertuples(index=False)
            ]
            chunk_size = 5000
            for i in range(0, len(stock_records), chunk_size):
                db.bulk_insert_mappings(SnapshotIndexStock, stock_records[i : i + chunk_size])
                db.commit()

            for universe_id in self.universes:
                spec = self._get_universe_spec(universe_id)
                slug = spec["slug"]
                snapshot_key = f"snapshot_{slug}_daily"
                snapshot_path = artifact_paths.get(snapshot_key)
                if snapshot_path is None:
                    continue
                uni_df = pd.read_parquet(snapshot_path, columns=["date", "universe_id"]).drop_duplicates()
                uni_df["date"] = pd.to_datetime(uni_df["date"], errors="coerce").dt.date
                db.query(SnapshotIndexUniverse).filter(
                    SnapshotIndexUniverse.snapshot_date >= self.start_date,
                    SnapshotIndexUniverse.snapshot_date <= self.asof,
                    SnapshotIndexUniverse.universe_id == universe_id.upper(),
                ).delete(synchronize_session=False)
                db.commit()
                universe_records = [
                    {
                        "snapshot_date": row.date,
                        "universe_id": row.universe_id,
                        "artifact_path": str(snapshot_path),
                        "run_id": self.run_id,
                        "version": "v1",
                    }
                    for row in uni_df.itertuples(index=False)
                ]
                for i in range(0, len(universe_records), chunk_size):
                    db.bulk_insert_mappings(SnapshotIndexUniverse, universe_records[i : i + chunk_size])
                    db.commit()

            db.query(DatasetRun).filter(DatasetRun.run_id == self.run_id).update(
                {
                    DatasetRun.status: "completed",
                    DatasetRun.completed_at: datetime.now(UTC),
                },
                synchronize_session=False,
            )
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            db.query(DatasetRun).filter(DatasetRun.run_id == self.run_id).update(
                {
                    DatasetRun.status: "failed",
                    DatasetRun.completed_at: datetime.now(UTC),
                    DatasetRun.details_json: json.dumps({"error": str(exc), "summary": self.summary}),
                },
                synchronize_session=False,
            )
            db.commit()
            self._log(step, "failed", {"error": str(exc)})
            raise
        finally:
            db.close()

        self.summary["publish"] = {"artifacts_published": len(checksums["artifacts"]), "checksums_file": str(self.paths.checksums)}
        self._log(step, "completed", self.summary["publish"])


def _run_step(builder: Phase1Builder, step: str, universe: str | None = None) -> None:
    if step == "ingest-sources":
        builder.ingest_sources()
    elif step == "build-universe":
        if not universe:
            raise ValueError("--universe is required for build-universe step")
        builder.build_universe(universe)
    elif step == "build-banknifty":
        builder.build_banknifty()
    elif step == "build-equity":
        builder.build_equity()
    elif step == "build-nifty50":
        builder.build_nifty50()
    elif step == "apply-corp-actions":
        builder.apply_corp_actions()
    elif step == "build-snapshots":
        builder.build_snapshots()
    elif step == "validate":
        ok = builder.validate()
        if not ok:
            raise SystemExit(1)
    elif step == "publish":
        builder.publish()
    else:
        raise ValueError(f"Unsupported step: {step}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase-1 data consolidation orchestrator")
    parser.add_argument(
        "step",
        nargs="?",
        choices=[
            "ingest-sources",
            "build-universe",
            "build-nifty50",
            "build-banknifty",
            "build-equity",
            "apply-corp-actions",
            "build-snapshots",
            "validate",
            "publish",
        ],
        help="Optional step to run. If omitted, runs full pipeline.",
    )
    parser.add_argument("--asof", default=date.today().isoformat(), help="As-of date (YYYY-MM-DD)")
    parser.add_argument("--start-date", default=PHASE_START_DATE.isoformat(), help="Start date (YYYY-MM-DD)")
    parser.add_argument("--mode", default="full", choices=["full", "incremental"], help="Run mode")
    parser.add_argument(
        "--universes",
        default=",".join(DEFAULT_UNIVERSES),
        help="Comma-separated universe ids for full run and snapshot generation (e.g., NIFTY50,BANKNIFTY)",
    )
    parser.add_argument("--universe", default=None, help="Single universe id for build-universe step")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asof = _parse_date(args.asof)
    start_date = _parse_date(args.start_date)
    universes = [u.strip().upper() for u in str(args.universes).split(",") if u.strip()]
    builder = Phase1Builder(asof=asof, mode=args.mode, start_date=start_date, universes=universes)
    if args.step:
        _run_step(builder, args.step, universe=args.universe)
        return
    builder.run_all()


if __name__ == "__main__":
    main()

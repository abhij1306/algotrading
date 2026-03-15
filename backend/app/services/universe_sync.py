from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Company, IndexConstituentHistory, IndexUniverseDefinition, UniverseSnapshot
from .index_universe_loader import INDEX_FILES, index_universe_loader
from .symbol_master import symbol_master

logger = logging.getLogger(__name__)

BROAD_INDEX_PREFIXES = (
    "NIFTY50",
    "NIFTY100",
    "NIFTY200",
    "NIFTY500",
    "NIFTYNEXT50",
    "NIFTYTOTALMARKET",
    "NIFTYSMALLCAP500",
    "NIFTYMIDCAP50",
    "NIFTYMIDCAP100",
    "NIFTYMIDCAP150",
    "NIFTYMIDCAPSELECT",
    "NIFTYSMALLCAP250",
    "NIFTYLARGEMIDCAP250",
    "NIFTYMICROCAP250",
    "NIFTYMIDSMALLCAP400",
)


@dataclass
class UniverseSyncSummary:
    as_of_date: str
    indices_processed: int
    definitions_created: int
    constituents_added: int
    constituents_closed: int
    snapshots_written: int
    companies_updated: int


def _is_broad_index(index_code: str) -> bool:
    return index_code.startswith(BROAD_INDEX_PREFIXES)


def sync_index_universes_from_loader(
    db: Session,
    *,
    as_of_date: date | None = None,
    update_company_categories: bool = True,
) -> UniverseSyncSummary:
    target_date = as_of_date or date.today()
    loader = index_universe_loader
    available_indices = loader.get_available_indices()

    definitions = {
        definition.index_code: definition
        for definition in db.query(IndexUniverseDefinition)
        .filter(IndexUniverseDefinition.index_code.in_(available_indices))
        .all()
    }

    definitions_created = 0
    constituents_added = 0
    constituents_closed = 0
    snapshots_written = 0
    companies_updated = 0

    broad_mapping: dict[str, list[str]] = {}
    sector_mapping: dict[str, list[str]] = {}

    for index_code in available_indices:
        definition = definitions.get(index_code)
        if definition is None:
            definition = IndexUniverseDefinition(
                index_code=index_code,
                index_name=loader.get_index_description(index_code) or index_code,
                description=loader.get_index_description(index_code) or index_code,
                is_custom=False,
                last_download_date=target_date,
            )
            db.add(definition)
            db.flush()
            definitions[index_code] = definition
            definitions_created += 1
        else:
            definition.index_name = loader.get_index_description(index_code) or index_code
            definition.description = loader.get_index_description(index_code) or index_code
            definition.last_download_date = target_date

        universe = loader.get_index_universe(index_code)
        constituents = universe.constituents if universe else []
        current_symbols = {constituent.symbol for constituent in constituents}

        active_rows = {
            row.symbol: row
            for row in db.query(IndexConstituentHistory)
            .filter(
                IndexConstituentHistory.universe_id == definition.id,
                IndexConstituentHistory.effective_to.is_(None),
            )
            .all()
        }

        for symbol, row in active_rows.items():
            if symbol not in current_symbols:
                row.effective_to = target_date - timedelta(days=1)
                constituents_closed += 1

        for constituent in constituents:
            if _is_broad_index(index_code):
                broad_mapping.setdefault(constituent.symbol, []).append(index_code)
            else:
                sector_mapping.setdefault(constituent.symbol, []).append(index_code)

            row = active_rows.get(constituent.symbol)
            source_file = INDEX_FILES.get(index_code, "")
            if row is None:
                db.add(
                    IndexConstituentHistory(
                        universe_id=definition.id,
                        symbol=constituent.symbol,
                        fyers_symbol=symbol_master.to_fyers(constituent.symbol),
                        isin=constituent.isin,
                        effective_from=target_date,
                        effective_to=None,
                        weight=getattr(constituent, "weight", None),
                        company_name=constituent.company_name,
                        industry=constituent.industry,
                        source_file=source_file,
                        import_date=target_date,
                    )
                )
                constituents_added += 1
            else:
                row.fyers_symbol = symbol_master.to_fyers(constituent.symbol)
                row.isin = constituent.isin
                row.weight = getattr(constituent, "weight", None)
                row.company_name = constituent.company_name
                row.industry = constituent.industry
                row.source_file = source_file
                row.import_date = target_date

        snapshot = (
            db.query(UniverseSnapshot)
            .filter(
                UniverseSnapshot.universe_id == definition.id,
                UniverseSnapshot.snapshot_date == target_date,
            )
            .first()
        )
        payload = json.dumps(sorted(current_symbols))
        if snapshot is None:
            db.add(
                UniverseSnapshot(
                    universe_id=definition.id,
                    snapshot_date=target_date,
                    symbols=payload,
                    source_data_date=target_date,
                )
            )
        else:
            snapshot.symbols = payload
            snapshot.source_data_date = target_date
        snapshots_written += 1

    if update_company_categories:
        companies = db.query(Company).filter(Company.is_active.is_(True)).all()
        for company in companies:
            broad_indices = broad_mapping.get(company.symbol, [])
            sector_indices = sector_mapping.get(company.symbol, [])
            company.broad_market = broad_indices[0] if broad_indices else None
            company.sector_index = sector_indices[0] if sector_indices else None
            if broad_indices or sector_indices:
                companies_updated += 1

    db.commit()

    summary = UniverseSyncSummary(
        as_of_date=target_date.isoformat(),
        indices_processed=len(available_indices),
        definitions_created=definitions_created,
        constituents_added=constituents_added,
        constituents_closed=constituents_closed,
        snapshots_written=snapshots_written,
        companies_updated=companies_updated,
    )
    logger.info("Universe sync complete: %s", summary)
    return summary

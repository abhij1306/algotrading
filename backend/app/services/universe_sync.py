from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta

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
        definition, created = _upsert_definition(db, definitions, index_code, target_date)
        definitions_created += int(created)

        universe = loader.get_index_universe(index_code)
        constituents = universe.constituents if universe else []
        current_symbols = {constituent.symbol for constituent in constituents}

        mapping = broad_mapping if _is_broad_index(index_code) else sector_mapping
        for constituent in constituents:
            mapping.setdefault(constituent.symbol, []).append(index_code)

        added, closed = _sync_constituents(
            db, definition.id, index_code, constituents, current_symbols, target_date
        )
        constituents_added += added
        constituents_closed += closed

        _write_snapshot(db, definition.id, current_symbols, target_date)
        snapshots_written += 1

    if update_company_categories:
        companies_updated = _update_company_categories(db, broad_mapping, sector_mapping)

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


def _upsert_definition(
    db: Session,
    definitions: dict[str, IndexUniverseDefinition],
    index_code: str,
    target_date: date,
) -> tuple[IndexUniverseDefinition, bool]:
    """The index's definition, created (and flushed for its id) or refreshed; True if created."""
    loader = index_universe_loader
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
        return definition, True

    definition.index_name = loader.get_index_description(index_code) or index_code
    definition.description = loader.get_index_description(index_code) or index_code
    definition.last_download_date = target_date
    return definition, False


def _sync_constituents(
    db: Session,
    universe_id: int,
    index_code: str,
    constituents: list,
    current_symbols: set[str],
    target_date: date,
) -> tuple[int, int]:
    """Close dropped members, add new ones and refresh the rest; returns (added, closed)."""
    active_rows = {
        row.symbol: row
        for row in db.query(IndexConstituentHistory)
        .filter(
            IndexConstituentHistory.universe_id == universe_id,
            IndexConstituentHistory.effective_to.is_(None),
        )
        .all()
    }

    closed = 0
    for symbol, row in active_rows.items():
        if symbol not in current_symbols:
            row.effective_to = target_date - timedelta(days=1)
            closed += 1

    added = 0
    source_file = INDEX_FILES.get(index_code, "")
    for constituent in constituents:
        row = active_rows.get(constituent.symbol)
        if row is None:
            row = IndexConstituentHistory(
                universe_id=universe_id,
                symbol=constituent.symbol,
                effective_from=target_date,
                effective_to=None,
            )
            _apply_constituent(row, constituent, source_file, target_date)
            db.add(row)
            added += 1
        else:
            _apply_constituent(row, constituent, source_file, target_date)
    return added, closed


def _apply_constituent(
    row: IndexConstituentHistory, constituent: object, source_file: str, target_date: date
) -> None:
    """Copy the loader's constituent details onto a history row."""
    row.fyers_symbol = symbol_master.to_fyers(constituent.symbol)
    row.isin = constituent.isin
    row.weight = getattr(constituent, "weight", None)
    row.company_name = constituent.company_name
    row.industry = constituent.industry
    row.source_file = source_file
    row.import_date = target_date


def _write_snapshot(
    db: Session, universe_id: int, current_symbols: set[str], target_date: date
) -> None:
    """Create or overwrite the universe's member snapshot for the target date."""
    snapshot = (
        db.query(UniverseSnapshot)
        .filter(
            UniverseSnapshot.universe_id == universe_id,
            UniverseSnapshot.snapshot_date == target_date,
        )
        .first()
    )
    payload = json.dumps(sorted(current_symbols))
    if snapshot is None:
        db.add(
            UniverseSnapshot(
                universe_id=universe_id,
                snapshot_date=target_date,
                symbols=payload,
                source_data_date=target_date,
            )
        )
    else:
        snapshot.symbols = payload
        snapshot.source_data_date = target_date


def _update_company_categories(
    db: Session, broad_mapping: dict[str, list[str]], sector_mapping: dict[str, list[str]]
) -> int:
    """Set each active company's first broad and sector index; returns how many have one."""
    companies_updated = 0
    for company in db.query(Company).filter(Company.is_active.is_(True)).all():
        broad_indices = broad_mapping.get(company.symbol, [])
        sector_indices = sector_mapping.get(company.symbol, [])
        company.broad_market = broad_indices[0] if broad_indices else None
        company.sector_index = sector_indices[0] if sector_indices else None
        if broad_indices or sector_indices:
            companies_updated += 1
    return companies_updated

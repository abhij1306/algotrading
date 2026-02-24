"""
Validation Reports Service
=========================
Service for generating validation reports on universe data,
index constituents, and symbol mappings.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import and_, func

from ..database import get_db_session
from ..models.company import Company
from ..models.universe import IndexConstituentHistory, IndexUniverseDefinition


class ValidationSeverity(str, Enum):
    """Severity levels for validation issues"""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A single validation issue"""

    severity: ValidationSeverity
    category: str
    message: str
    entity_type: str  # e.g., 'index', 'symbol', 'company'
    entity_id: Any
    details: dict[str, Any] = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass
class ValidationReport:
    """A complete validation report"""

    report_date: datetime
    total_checks: int
    passed_checks: int
    failed_checks: int
    issues: list[ValidationIssue]
    summary: dict[str, int]

    @property
    def success_rate(self) -> float:
        if self.total_checks == 0:
            return 100.0
        return (self.passed_checks / self.total_checks) * 100


class ValidationReportsService:
    """
    Service for generating validation reports on universe data.
    """

    def generate_universe_validation_report(
        self,
        index_code: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ValidationReport:
        """
        Generate a validation report for universe data.

        Args:
            index_code: Optional index to validate (None for all)
            start_date: Optional start date for range
            end_date: Optional end date for range

        Returns:
            ValidationReport with all findings
        """
        session = get_db_session()
        issues: list[ValidationIssue] = []

        try:
            # Build base query
            query = session.query(IndexUniverseDefinition)
            if index_code:
                query = query.filter(IndexUniverseDefinition.index_code == index_code)

            indices = query.all()

            # Check each index
            for idx in indices:
                # 1. Check for duplicate index codes
                dup_count = (
                    session.query(IndexUniverseDefinition)
                    .filter(IndexUniverseDefinition.index_code == idx.index_code)
                    .count()
                )

                if dup_count > 1:
                    issues.append(
                        ValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            category="duplicate",
                            message=f"Duplicate index code: {idx.index_code}",
                            entity_type="index",
                            entity_id=idx.id,
                        )
                    )

                # 2. Check constituent history
                constituents_query = session.query(IndexConstituentHistory).filter(
                    IndexConstituentHistory.universe_id == idx.id
                )

                if start_date:
                    constituents_query = constituents_query.filter(
                        IndexConstituentHistory.effective_from >= start_date
                    )
                if end_date:
                    constituents_query = constituents_query.filter(
                        IndexConstituentHistory.effective_from <= end_date
                    )

                constituents = constituents_query.all()

                # Check for gaps in history
                if len(constituents) > 1:
                    # Sort by date
                    sorted_constituents = sorted(constituents, key=lambda x: x.effective_from)

                    # Check for gaps
                    for i in range(1, len(sorted_constituents)):
                        prev_date = sorted_constituents[i - 1].effective_to
                        curr_date = sorted_constituents[i].effective_from

                        if prev_date and curr_date:
                            # Check for gap
                            if (curr_date - prev_date).days > 1:
                                issues.append(
                                    ValidationIssue(
                                        severity=ValidationSeverity.WARNING,
                                        category="data_gap",
                                        message=f"Gap in constituent history for {idx.index_code}: "
                                        f"{prev_date} to {curr_date}",
                                        entity_type="index",
                                        entity_id=idx.id,
                                        details={
                                            "gap_start": str(prev_date),
                                            "gap_end": str(curr_date),
                                        },
                                    )
                                )

                # 3. Check for missing symbols
                # Batch load all company symbols to avoid N+1 queries
                const_symbols = [c.symbol for c in constituents if c.symbol]
                company_map = {}
                if const_symbols:
                    companies = (
                        session.query(Company).filter(Company.symbol.in_(const_symbols)).all()
                    )
                    company_map = {c.symbol: c for c in companies}

                for const in constituents:
                    if const.symbol:
                        company = company_map.get(const.symbol)

                        if not company:
                            issues.append(
                                ValidationIssue(
                                    severity=ValidationSeverity.WARNING,
                                    category="missing_company",
                                    message=f"Symbol {const.symbol} in {idx.index_code} "
                                    f"not found in companies table",
                                    entity_type="symbol",
                                    entity_id=const.symbol,
                                    details={
                                        "index_code": idx.index_code,
                                        "effective_from": str(const.effective_from),
                                    },
                                )
                            )

                # 4. Check weight totals
                checked_dates = set()
                for const in constituents:
                    if const.weight is not None and const.effective_from not in checked_dates:
                        checked_dates.add(const.effective_from)
                        # Check if weights sum to 100
                        day_weights = (
                            session.query(IndexConstituentHistory)
                            .filter(
                                and_(
                                    IndexConstituentHistory.universe_id == idx.id,
                                    IndexConstituentHistory.effective_from == const.effective_from,
                                )
                            )
                            .all()
                        )

                        total_weight = sum(w.weight for w in day_weights if w.weight)
                        if total_weight and abs(total_weight - 100.0) > 0.01:
                            issues.append(
                                ValidationIssue(
                                    severity=ValidationSeverity.WARNING,
                                    category="weight_total",
                                    message=f"Weights for {idx.index_code} on {const.effective_from} "
                                    f"sum to {total_weight:.2f}%, expected 100%",
                                    entity_type="index",
                                    entity_id=idx.id,
                                    details={
                                        "date": str(const.effective_from),
                                        "total_weight": total_weight,
                                    },
                                )
                            )
            # Count issues by severity
            error_count = sum(1 for i in issues if i.severity == ValidationSeverity.ERROR)
            warning_count = sum(1 for i in issues if i.severity == ValidationSeverity.WARNING)
            info_count = sum(1 for i in issues if i.severity == ValidationSeverity.INFO)

            total_checks = len(indices) * 4  # 4 checks per index
            passed_checks = total_checks - error_count - warning_count

            return ValidationReport(
                report_date=datetime.now(),
                total_checks=total_checks,
                passed_checks=passed_checks,
                failed_checks=error_count + warning_count,
                issues=issues,
                summary={
                    "total_indices": len(indices),
                    "errors": error_count,
                    "warnings": warning_count,
                    "info": info_count,
                },
            )

        finally:
            session.close()

    def generate_symbol_coverage_report(
        self, start_date: date | None = None, end_date: date | None = None
    ) -> dict[str, Any]:
        """
        Generate a report on symbol coverage across indices.

        Returns:
            Dictionary with coverage statistics
        """
        session = get_db_session()

        try:
            # Get all unique symbols from constituent history
            query = session.query(
                IndexConstituentHistory.symbol,
                func.count(IndexConstituentHistory.id).label("occurrence_count"),
                func.min(IndexConstituentHistory.effective_from).label("first_seen"),
                func.max(IndexConstituentHistory.effective_from).label("last_seen"),
            ).group_by(IndexConstituentHistory.symbol)

            if start_date:
                query = query.filter(IndexConstituentHistory.effective_from >= start_date)
            if end_date:
                query = query.filter(IndexConstituentHistory.effective_from <= end_date)
            symbol_stats = query.all()

            # Get all companies
            companies = session.query(Company).all()
            company_symbols = {c.symbol for c in companies}

            # Find symbols in history but not in companies
            unknown_symbols = []
            for stat in symbol_stats:
                if stat.symbol and stat.symbol not in company_symbols:
                    unknown_symbols.append(
                        {
                            "symbol": stat.symbol,
                            "occurrences": stat.occurrence_count,
                            "first_seen": str(stat.first_seen) if stat.first_seen else None,
                            "last_seen": str(stat.last_seen) if stat.last_seen else None,
                        }
                    )

            # Get coverage stats
            total_history_symbols = len([s for s in symbol_stats if s.symbol])
            covered_symbols = len(
                [s for s in symbol_stats if s.symbol and s.symbol in company_symbols]
            )

            return {
                "report_date": datetime.now().isoformat(),
                "period": {
                    "start_date": str(start_date) if start_date else None,
                    "end_date": str(end_date) if end_date else None,
                },
                "total_symbols_in_history": total_history_symbols,
                "symbols_with_company_data": covered_symbols,
                "symbols_without_company_data": len(unknown_symbols),
                "coverage_percentage": (covered_symbols / total_history_symbols * 100)
                if total_history_symbols > 0
                else 0,
                "unknown_symbols": unknown_symbols[:50],  # Limit to 50
            }

        finally:
            session.close()

    def generate_change_detection_report(
        self, index_code: str, start_date: date, end_date: date
    ) -> dict[str, Any]:
        """
        Generate a report on index constituent changes.

        Args:
            index_code: Index to analyze
            start_date: Start of period
            end_date: End of period

        Returns:
            Dictionary with change analysis
        """
        session = get_db_session()

        try:
            # Get index
            index = (
                session.query(IndexUniverseDefinition)
                .filter(IndexUniverseDefinition.index_code == index_code)
                .first()
            )

            if not index:
                return {"error": f"Index {index_code} not found"}

            # Get all constituents in period
            constituents = (
                session.query(IndexConstituentHistory)
                .filter(
                    and_(
                        IndexConstituentHistory.universe_id == index.id,
                        IndexConstituentHistory.effective_from >= start_date,
                        IndexConstituentHistory.effective_from <= end_date,
                    )
                )
                .order_by(IndexConstituentHistory.effective_from)
                .all()
            )

            # Group by date
            by_date: dict[date, set] = {}
            for const in constituents:
                if const.effective_from not in by_date:
                    by_date[const.effective_from] = set()
                if const.symbol:
                    by_date[const.effective_from].add(const.symbol)

            # Calculate changes
            sorted_dates = sorted(by_date.keys())
            additions = []
            removals = []

            for i in range(1, len(sorted_dates)):
                prev_symbols = by_date[sorted_dates[i - 1]]
                curr_symbols = by_date[sorted_dates[i]]

                new_symbols = curr_symbols - prev_symbols
                removed_symbols = prev_symbols - curr_symbols

                for sym in new_symbols:
                    additions.append({"symbol": sym, "date": str(sorted_dates[i])})

                for sym in removed_symbols:
                    removals.append({"symbol": sym, "date": str(sorted_dates[i])})

            return {
                "report_date": datetime.now().isoformat(),
                "index_code": index_code,
                "period": {"start_date": str(start_date), "end_date": str(end_date)},
                "total_constituent_changes": len(additions) + len(removals),
                "additions": additions,
                "removals": removals,
                "unique_additions": len(set(a["symbol"] for a in additions)),
                "unique_removals": len(set(r["symbol"] for r in removals)),
            }

        finally:
            session.close()


# Singleton
_validation_service: ValidationReportsService | None = None


def get_validation_service() -> ValidationReportsService:
    """Get the singleton ValidationReportsService instance"""
    global _validation_service
    if _validation_service is None:
        _validation_service = ValidationReportsService()
    return _validation_service

"""
Database Schema Validator
Ensures database integrity and validates constraints

Run this after any schema changes or migrations
"""
import sys

from sqlalchemy import inspect, text

from app.database import (
    SessionLocal,
    engine,
)


class SchemaValidator:
    """Validates database schema and constraints"""

    def __init__(self):
        self.db = SessionLocal()
        self.errors = []
        self.warnings = []
        self.passed = []

    def validate_all(self):
        """Run all validation checks"""
        print("=" * 80)
        print("DATABASE SCHEMA VALIDATION")
        print("=" * 80)

        self.check_tables_exist()
        self.check_required_columns()
        self.check_indexes()
        self.check_foreign_keys()
        self.check_data_integrity()

        self.print_summary()

        return len(self.errors) == 0

    def check_tables_exist(self):
        """Verify all required tables exist"""
        print("\n📋 Checking tables...")

        required_tables = [
            'companies', 'historical_prices', 'financial_statements',
            'portfolios', 'portfolio_holdings',
            'strategy_contracts', 'backtest_runs', 'backtest_daily_results',
            'paper_orders', 'paper_positions',
            'stock_universes'
        ]

        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        for table in required_tables:
            if table in existing_tables:
                self.passed.append(f"✅ Table '{table}' exists")
            else:
                self.errors.append(f"❌ Missing table: '{table}'")

        print(f"  Found {len(existing_tables)}/{len(required_tables)} required tables")

    def check_required_columns(self):
        """Verify critical columns exist"""
        print("\n📊 Checking required columns...")

        critical_columns = {
            'strategy_contracts': ['strategy_id', 'lifecycle_state', 'allowed_universes'],
            'backtest_runs': ['run_id', 'strategy_id', 'total_return', 'sharpe_ratio'],
            'portfolios': ['id', 'created_at'],
            'companies': ['symbol', 'name', 'is_active']
        }

        inspector = inspect(engine)

        for table, columns in critical_columns.items():
            table_columns = [col['name'] for col in inspector.get_columns(table)]

            for col in columns:
                if col in table_columns:
                    self.passed.append(f"✅ {table}.{col} exists")
                else:
                    self.errors.append(f"❌ Missing column: {table}.{col}")

    def check_indexes(self):
        """Check for performance-critical indexes"""
        print("\n⚡ Checking indexes...")

        inspector = inspect(engine)

        # Check critical indexes
        critical_indexes = {
            'historical_prices': ['symbol', 'date'],
            'backtest_daily_results': ['strategy_id', 'date'],
            'portfolio_holdings': ['portfolio_id']
        }

        for table, index_columns in critical_indexes.items():
            indexes = inspector.get_indexes(table)
            index_cols = [idx['column_names'] for idx in indexes]

            for col in index_columns:
                # Check if column is indexed (either alone or in composite)
                is_indexed = any(col in idx_col for idx_col in index_cols)

                if is_indexed:
                    self.passed.append(f"✅ Index on {table}.{col}")
                else:
                    self.warnings.append(f"⚠️  Missing index: {table}.{col}")

    def check_foreign_keys(self):
        """Verify foreign key constraints"""
        print("\n🔗 Checking foreign keys...")

        inspector = inspect(engine)

        critical_fks = {
            'portfolio_holdings': 'portfolios',
            'backtest_daily_results': 'backtest_runs',
            'paper_positions': 'paper_orders'
        }

        for table, referenced_table in critical_fks.items():
            try:
                fks = inspector.get_foreign_keys(table)
                has_fk_to_table = any(fk['referred_table'] == referenced_table for fk in fks)

                if has_fk_to_table:
                    self.passed.append(f"✅ FK: {table} → {referenced_table}")
                else:
                    self.warnings.append(f"⚠️  Missing FK: {table} → {referenced_table}")
            except Exception as e:
                self.warnings.append(f"⚠️  Could not check FK for {table}: {str(e)}")

    def check_data_integrity(self):
        """Check data quality and integrity"""
        print("\n🔍 Checking data integrity...")

        try:
            # Check for orphaned records
            orphaned_holdings = self.db.execute(text("""
                SELECT COUNT(*) FROM portfolio_holdings
                WHERE portfolio_id NOT IN (SELECT id FROM portfolios)
            """)).scalar()

            if orphaned_holdings == 0:
                self.passed.append("✅ No orphaned portfolio holdings")
            else:
                self.errors.append(f"❌ Found {orphaned_holdings} orphaned portfolio holdings")

            # Check for NULL critical fields
            null_symbols = self.db.execute(text("""
                SELECT COUNT(*) FROM companies WHERE symbol IS NULL
            """)).scalar()

            if null_symbols == 0:
                self.passed.append("✅ No NULL symbols in companies")
            else:
                self.errors.append(f"❌ Found {null_symbols} companies with NULL symbol")

            # Check lifecycle states are valid
            invalid_states = self.db.execute(text("""
                SELECT COUNT(*) FROM strategy_contracts
                WHERE lifecycle_state NOT IN ('RESEARCH', 'PAPER', 'LIVE', 'RETIRED')
            """)).scalar()

            if invalid_states == 0:
                self.passed.append("✅ All lifecycle states are valid")
            else:
                self.errors.append(f"❌ Found {invalid_states} invalid lifecycle states")

        except Exception as e:
            self.warnings.append(f"⚠️  Data integrity check failed: {str(e)}")

    def print_summary(self):
        """Print validation summary"""
        print("\n" + "=" * 80)
        print("VALIDATION SUMMARY")
        print("=" * 80)

        print(f"\n✅ Passed: {len(self.passed)}")
        print(f"⚠️  Warnings: {len(self.warnings)}")
        print(f"❌ Errors: {len(self.errors)}")

        if self.warnings:
            print("\n⚠️  WARNINGS:")
            for warning in self.warnings[:10]:  # Show first 10
                print(f"  {warning}")

        if self.errors:
            print("\n❌ ERRORS:")
            for error in self.errors:
                print(f"  {error}")

        if len(self.errors) == 0:
            print("\n✅ DATABASE SCHEMA VALIDATION PASSED")
        else:
            print("\n❌ DATABASE SCHEMA VALIDATION FAILED")

        print("=" * 80)

    def __del__(self):
        self.db.close()


def main():
    """Run schema validation"""
    validator = SchemaValidator()
    success = validator.validate_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

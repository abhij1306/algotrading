"""Money helpers: Decimal only, never float (HAT-SCH-014).

HAT reuses the repo's exact-arithmetic helpers instead of duplicating them. `to_money` rounds
to paise (INR); the column type is `numeric(18, 4)`, so storage keeps a little more.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.execution.paper_book import to_decimal, to_money

__all__ = ["MONEY_PRECISION", "MONEY_SCALE", "Decimal", "to_decimal", "to_money"]

MONEY_PRECISION = 18
MONEY_SCALE = 4

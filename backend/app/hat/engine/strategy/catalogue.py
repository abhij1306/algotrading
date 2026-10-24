"""The criterion catalogue (HAT-CRIT-021): `hat_criterion_type` as a copy of the registry."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import HatCriterionType
from ..criteria import REGISTRY


def sync_criterion_types(session: Session) -> int:
    """Make `hat_criterion_type` hold exactly the registry: insert missing rows and bring each
    row's stage, `param_schema` and `data_needs` to the registry's. Returns the rows written."""
    existing = {
        (row.code, row.version): row for row in session.scalars(select(HatCriterionType)).all()
    }
    written = 0
    for ctype in REGISTRY.values():
        schema, needs = ctype.param_schema(), list(ctype.data_needs)
        row = existing.pop((ctype.code, ctype.version), None)
        if row is None:
            session.add(
                HatCriterionType(
                    code=ctype.code,
                    version=ctype.version,
                    stage=ctype.stage,
                    param_schema=schema,
                    data_needs=needs,
                )
            )
            written += 1
        elif (row.stage, row.param_schema, row.data_needs) != (ctype.stage, schema, needs):
            row.stage, row.param_schema, row.data_needs = ctype.stage, schema, needs
            written += 1
    session.flush()
    return written

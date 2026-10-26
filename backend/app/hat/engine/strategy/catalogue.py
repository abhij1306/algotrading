"""The criterion catalogue (HAT-CRIT-021): the `criterion_type` config records as a copy of the
registry."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...configs.records import CriterionType
from ...configs.unit import configs_of
from ..criteria import REGISTRY


def sync_criterion_types(session: Session) -> int:
    """Make the `criterion_type` records hold exactly the registry: add missing records and bring
    each one's stage, `param_schema` and `data_needs` to the registry's (written at the caller's
    commit). Returns the records written."""
    unit = configs_of(session)
    written = 0
    for ctype in REGISTRY.values():
        schema, needs = ctype.param_schema(), list(ctype.data_needs)
        row = unit.get(CriterionType, f"{ctype.code}@{ctype.version}")
        if row is None:
            unit.add(
                CriterionType(
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
    return written

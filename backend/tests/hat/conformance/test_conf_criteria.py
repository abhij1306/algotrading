"""CONF checks of the criteria library and the seeded strategy configs."""

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine.criteria import REGISTRY
from app.hat.engine.strategy.config import parse_config
from app.hat.engine.strategy.registry import STRATEGIES
from app.hat.engine.strategy.versions import (
    seed_config,
    seed_text,
    sync_criterion_types,
)
from app.hat.models import HatCriterionType
from app.hat.schema import seed_hat_reference


def _memory_session() -> Session:
    from hat.hat_support import memory_engine

    engine = memory_engine()
    seed_hat_reference(engine)
    return Session(engine)


def test_criterion_type_registry() -> None:
    """The stored `param_schema` of every type equals the schema generated from its
    parameter model, and the registry holds exactly the catalogue."""
    with _memory_session() as session:
        sync_criterion_types(session)
        sync_criterion_types(session)  # repeating changes nothing
        rows = {r.code: r for r in session.scalars(select(HatCriterionType)).all()}
    assert set(rows) == set(REGISTRY)
    for code, ctype in REGISTRY.items():
        assert rows[code].param_schema == ctype.params.model_json_schema(), code
        assert rows[code].stage == ctype.stage, code


def test_seeds_parse_and_hash_stably() -> None:
    for name in STRATEGIES:
        _seed_parses(name)


def _seed_parses(name: str) -> None:
    from_file = yaml.safe_load(seed_text(name))
    parsed = parse_config(from_file)
    assert parsed.hash() == seed_config(name).hash()
    assert parse_config(from_file).to_json() == parsed.to_json()

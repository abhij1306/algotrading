"""CONF checks of the criteria library, the config model and the two seeded bots (HAT-DEC-B2,
HAT-DEC-B3, HAT-CRIT-020, HAT-CRIT-021, HAT-BOT-060, HAT-BOT-061, HAT-BOT-072, HAT-GOV-080). The
expected catalogue and seed configs are read from the spec markdown, not restated here."""

import uuid

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.engine.criteria import REGISTRY
from app.hat.engine.strategy.config import Instance, parse_config
from app.hat.engine.strategy.versions import (
    SEED_NAMES,
    seed_config,
    seed_default_bots,
    seed_text,
    sync_criterion_types,
)
from app.hat.models import HatBot, HatCriterionType
from app.hat.risk.policy import seed_policy
from app.hat.schema import seed_hat_reference

STAGES = {"universe", "setup", "trigger", "entry_filter"}
S1, S2 = SEED_NAMES


def _memory_session() -> Session:
    from hat.hat_support import memory_engine

    engine = memory_engine()
    seed_hat_reference(engine)
    return Session(engine)


def test_criterion_instance_schema() -> None:
    """HAT-CRIT-020: an instance has exactly the seven documented fields."""
    fields = {"type", "stage", "mode", "weight", "params", "enabled", "order"}
    assert set(Instance.model_fields) == fields


def test_criterion_type_registry() -> None:
    """HAT-CRIT-021: the stored `param_schema` of every type equals the schema generated from its
    parameter model, and the registry holds exactly the catalogue."""
    with _memory_session() as session:
        sync_criterion_types(session)
        sync_criterion_types(session)  # repeating changes nothing
        rows = {r.code: r for r in session.scalars(select(HatCriterionType)).all()}
    assert set(rows) == set(REGISTRY)
    for code, ctype in REGISTRY.items():
        assert rows[code].param_schema == ctype.params.model_json_schema(), code
        assert rows[code].stage == ctype.stage, code


def test_criterion_catalogue_complete() -> None:
    """HAT-DEC-B3: the library registers 25 types, each in a known stage."""
    assert len(REGISTRY) == 25
    assert {ctype.stage for ctype in REGISTRY.values()} <= STAGES


def _seed_parses(name: str) -> None:
    from_file = yaml.safe_load(seed_text(name))
    parsed = parse_config(from_file)
    assert parsed.hash() == seed_config(name).hash()
    assert parse_config(from_file).to_json() == parsed.to_json()


def test_bot_seed_s1_parses() -> None:
    """HAT-BOT-060: the S1 seed file parses, hashes stably and keeps earnings_growth off."""
    _seed_parses(S1)
    parsed = seed_config(S1)
    off = [i.type for i in parsed.playbooks[0].setup.criteria if not i.enabled]
    assert off == ["earnings_growth"]


def test_bot_seed_s2_parses() -> None:
    """HAT-BOT-060 (S2 half): the S2 seed file parses and hashes stably."""
    _seed_parses(S2)


def test_bot_seed_default_mode() -> None:
    """HAT-BOT-061: both seeds declare shadow."""
    for name in SEED_NAMES:
        assert yaml.safe_load(seed_text(name))["execution_mode"] == "shadow"
        assert seed_config(name).execution_mode == "shadow"


def test_seed_bots_confirm() -> None:
    """HAT-DEC-B2: every seeded bot row asks for confirm, one per seeded strategy, and starts in
    research, so it runs shadow (HAT-PORT-063)."""
    with _memory_session() as session:
        bots = seed_default_bots(session, user_id=uuid.uuid4(), broker_account_id=uuid.uuid4())
        rows = session.scalars(select(HatBot)).all()
    assert len(bots) == len(rows) == 2
    assert {b.execution_mode for b in rows} == {"confirm"}
    assert {b.lifecycle_state for b in rows} == {"research"}


def test_risk_policy_seed_bot_limits() -> None:
    """HAT-BOT-072: the risk policy, not the strategy, holds p_max 0.20 (S1) and 0.25 (S2) and
    6 open positions."""
    policy = seed_policy()
    assert {k: str(v) for k, v in policy.p_max.items()} == {S1: "0.20", S2: "0.25"}
    assert policy.max_open_positions == 6

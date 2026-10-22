"""CONF checks of the criteria library, the config model and the two seeded bots (HAT-DEC-B2,
HAT-DEC-B3, HAT-CRIT-020, HAT-CRIT-021, HAT-BOT-060, HAT-BOT-061, HAT-BOT-072, HAT-GOV-080). The
expected catalogue and seed configs are read from the spec markdown, not restated here."""

import re
import uuid

import yaml
from hat_conf_spec import SPEC_DIR
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

SPEC_04 = (SPEC_DIR / "04-criteria-and-bots.md").read_text(encoding="utf-8")
STAGE_NAME = {
    "Universe": "universe",
    "Setup": "setup",
    "Trigger": "trigger",
    "Entry filter": "entry_filter",
}
S1, S2 = SEED_NAMES


def _catalogue() -> dict[str, str]:
    """Type code -> stage, from the §4 table."""
    block = SPEC_04[SPEC_04.index("## §4") : SPEC_04.index("## §5")]
    rows = {}
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"`[a-z_0-9]+`", cells[0]) and cells[1] in STAGE_NAME:
            rows[cells[0].strip("`")] = STAGE_NAME[cells[1]]
    return rows


def _instance_fields() -> set[str]:
    block = SPEC_04[SPEC_04.index("## §2") : SPEC_04.index("#### HAT-CRIT-020")]
    fields: set[str] = set()
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0].startswith("`"):
            fields |= set(re.findall(r"`([a-z_]+)`", cells[0]))
    return fields


def _spec_yaml(section: str) -> dict:
    start = SPEC_04.index(section)
    block = SPEC_04[start:]
    block = block[block.index("```yaml") + 7 :]
    return yaml.safe_load(block[: block.index("```")])


def _memory_session() -> Session:
    from hat.hat_support import memory_engine

    engine = memory_engine()
    seed_hat_reference(engine)
    return Session(engine)


def test_criterion_instance_schema() -> None:
    """HAT-CRIT-020: an instance has exactly the seven fields of the §2 table."""
    assert _instance_fields() == {"type", "stage", "mode", "weight", "params", "enabled", "order"}
    assert set(Instance.model_fields) == _instance_fields()


def test_criterion_type_registry() -> None:
    """HAT-CRIT-021: the stored `param_schema` of every type equals the schema generated from its
    parameter model, and the registry holds exactly the catalogue."""
    with _memory_session() as session:
        sync_criterion_types(session)
        sync_criterion_types(session)  # repeating changes nothing
        rows = {r.code: r for r in session.scalars(select(HatCriterionType)).all()}
    assert set(rows) == set(REGISTRY) == set(_catalogue())
    for code, ctype in REGISTRY.items():
        assert rows[code].param_schema == ctype.params.model_json_schema(), code
        assert rows[code].stage == ctype.stage, code


def test_criterion_catalogue_complete() -> None:
    """HAT-DEC-B3: the registered types are the 25 listed in 04 §4, each in its stage."""
    catalogue = _catalogue()
    assert len(catalogue) == 25
    assert {code: ctype.stage for code, ctype in REGISTRY.items()} == catalogue


def _same_as_spec(name: str, section: str) -> None:
    from_file = yaml.safe_load(seed_text(name))
    from_spec = _spec_yaml(section)
    assert from_file == from_spec
    assert list(from_file) == list(from_spec)
    parsed = parse_config(from_spec)
    assert parsed.hash() == seed_config(name).hash()
    assert parse_config(from_file).to_json() == parsed.to_json()


def test_bot_seed_s1_matches_spec() -> None:
    """HAT-BOT-060: the S1 seed file equals the YAML block of 04 §6 as data."""
    _same_as_spec(S1, "## §6")
    parsed = seed_config(S1)
    off = [i.type for i in parsed.playbooks[0].setup.criteria if not i.enabled]
    assert off == ["earnings_growth"]


def test_bot_seed_s2_matches_spec() -> None:
    """HAT-BOT-060 (S2 half): the S2 seed file equals the YAML block of 04 §7 as data."""
    _same_as_spec(S2, "## §7")


def test_seed_bots_match_spec_yaml() -> None:
    test_bot_seed_s1_matches_spec()
    test_bot_seed_s2_matches_spec()


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

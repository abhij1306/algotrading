"""PBT config.hash_canonical: the config hash is canonical."""

import copy
import random
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import yaml
from hat.hat_config_support import (
    raw_configs,
)
from hat.hat_risk_support import calendar_around
from hat.hat_support import memory_engine, memory_session
from hypothesis import assume, given
from hypothesis import strategies as st

from app.hat.confighash import config_hash
from app.hat.engine.criteria import REGISTRY
from app.hat.engine.criteria.live import LiveContext
from app.hat.engine.strategy.config import (
    ConfigError,
    Instance,
    Playbook,
    StrategyConfig,
    parse_config,
)
from app.hat.engine.strategy.registry import STRATEGIES, RegistryError, parse_seed
from app.hat.engine.strategy.versions import (
    VersionError,
    save_draft,
    seed_config,
    seed_default_strategies,
    seed_text,
)
from app.hat.engine.strategy.yamlio import import_yaml
from app.hat.engine.trigger import daily_level, evaluate_trigger, needs_setup_level
from app.hat.risk.policy import POLICY_FIELDS, SEED_POLICY
from app.hat.schema import seed_hat_reference

# Any string, lone surrogates included: JSON text from an API can carry them.
text = st.text(alphabet=st.characters(blacklist_categories=()), max_size=8)
leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**20), max_value=10**20),
    st.floats(allow_nan=False, allow_infinity=False),
    text,
)
json_value = st.recursive(
    leaf,
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(text, children, max_size=4)
    ),
    max_leaves=12,
)
configs = st.dictionaries(text, json_value, min_size=1, max_size=5)


def shuffled(value: object, rng: random.Random) -> object:
    """The same data with every dict's keys inserted in a random order."""
    if isinstance(value, dict):
        keys = list(value)
        rng.shuffle(keys)
        return {key: shuffled(value[key], rng) for key in keys}
    if isinstance(value, list):
        return [shuffled(item, rng) for item in value]
    return value


def same_value(a: object, b: object) -> bool:
    """Strict equality: 1, 1.0 and True are three different config values."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and isinstance(b, float):
        return a == b  # 0.0 and -0.0 are one value
    return a == b


def leaf_paths(value: object, prefix: tuple = ()) -> list[tuple]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in leaf_paths(v, (*prefix, k))] or [prefix]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in leaf_paths(v, (*prefix, i))] or [prefix]
    return [prefix]


def replaced(value: object, path: tuple, new: object) -> object:
    if not path:
        return new
    head, rest = path[0], path[1:]
    if isinstance(value, dict):
        return {k: (replaced(v, rest, new) if k == head else v) for k, v in value.items()}
    assert isinstance(value, list)
    return [replaced(v, rest, new) if i == head else v for i, v in enumerate(value)]


@given(config=configs, data=st.data())
def test_hash_changes_with_any_value(config: dict, data: st.DataObject) -> None:
    """Changing one value anywhere, including its type, changes the hash."""
    path = data.draw(st.sampled_from(leaf_paths(config)))
    new = data.draw(json_value)
    mutated = replaced(config, path, new)
    assume(not same_value(mutated, config))
    assert config_hash(mutated) != config_hash(config)


@given(a=configs, b=configs)
def test_hash_equal_iff_same_config(a: dict, b: dict) -> None:
    assert (config_hash(a) == config_hash(b)) == same_value(a, b)


def with_zero_signs_flipped(value: object) -> object:
    """The same config with the sign of every float zero reversed (0.0 and -0.0 are equal)."""
    if isinstance(value, dict):
        return {k: with_zero_signs_flipped(v) for k, v in value.items()}
    if isinstance(value, list):
        return [with_zero_signs_flipped(v) for v in value]
    if isinstance(value, float) and value == 0.0:
        return -value
    return value


@given(config=st.dictionaries(text, st.lists(st.sampled_from([0.0, -0.0, 1.5]), max_size=4)))
def test_hash_ignores_the_sign_of_zero(config: dict) -> None:
    """Equal numbers hash alike: 0.0 and -0.0 are one config value."""
    flipped = with_zero_signs_flipped(config)
    assert same_value(flipped, config)
    assert config_hash(flipped) == config_hash(config)


# --- the strategy config model --------------------------

ENGINE = memory_engine()
seed_hat_reference(ENGINE)
USER = uuid.UUID("00000000-0000-7000-8000-0000000000a1")


def all_instances(config: StrategyConfig) -> list[Instance]:
    out = [*config.universe]
    for playbook in config.playbooks:
        out += [*playbook.setup.criteria, *playbook.trigger, *playbook.entry_filters]
    return out


def codes_of(raw: dict) -> set[str]:
    with pytest.raises(ConfigError) as caught:
        parse_config(raw)
    return caught.value.codes


@given(raw_configs(), st.randoms(use_true_random=False))
def test_stage_slots(raw: dict, rnd: random.Random) -> None:
    """PBT config.stage_slots: an instance only fits a section its type
    allows, scoring only in setup; the same instances in any list order store identically."""
    config = parse_config(raw)
    for instance in all_instances(config):
        ctype = REGISTRY[instance.type]
        assert instance.stage == ctype.stage or instance.stage in ctype.extra_stages
        assert instance.mode == "hard" or instance.stage == "setup"
    for instance in config.universe:
        wrong = {"type": "gap_up", "params": {"min_pct": 2.0}}
        assert codes_of({**raw, "universe": [wrong]}) & {"wrong_stage", "invalid"}
        assert instance.stage == "universe"
    # explicit order and stage make the list order irrelevant
    explicit = parse_config(raw).to_json()
    for section in (explicit["universe"],):
        rnd.shuffle(section)
    assert parse_config(explicit).hash() == config.hash()
    scored_trigger = {
        "type": "gap_up",
        "stage": "trigger",
        "order": 0,
        "mode": "score",
        "weight": 1,
    }
    explicit["playbooks"][0]["trigger"] = [scored_trigger]
    assert "score_outside_setup" in codes_of(explicit)


@given(
    gate_at=st.integers(0, 13),
    vcp_at=st.one_of(st.none(), st.integers(0, 13)),
    gate_on=st.booleans(),
    vcp_on=st.booleans(),
    scored=st.booleans(),
)
def test_close_vs_pivot_needs_vcp(
    gate_at: int, vcp_at: int | None, gate_on: bool, vcp_on: bool, scored: bool
) -> None:
    """PBT config.close_vs_pivot_needs_vcp: a setup with an enabled
    `close_vs_pivot` is valid exactly when an enabled `vcp` comes before it (S1's seed setup
    with the two moved, removed or switched off), and `close_vs_pivot` is refused in score mode.
    Without an enabled `vcp` S1's trigger names a level no setup criterion exposes.
    Harm: a config whose gate can never pass is saved, and the playbook silently stops trading;
    a scored gate only lowers the score, so a stale breakout still publishes."""
    raw = yaml.safe_load(seed_text("stage2-contraction-breakout"))
    criteria = raw["setup"]["criteria"]
    gate = next(c for c in criteria if c["type"] == "close_vs_pivot")
    vcp = next(c for c in criteria if c["type"] == "vcp")
    rest = [c for c in criteria if c["type"] not in {"close_vs_pivot", "vcp"}]
    scoring = {"mode": "score", "weight": 1} if scored else {}
    rest.insert(min(gate_at, len(rest)), {**gate, "enabled": gate_on, **scoring})
    if vcp_at is not None:
        rest.insert(min(vcp_at, len(rest)), {**vcp, "enabled": vcp_on})
    raw["setup"]["criteria"] = rest
    types = [c["type"] for c in rest]
    valid = not gate_on or (
        vcp_at is not None and vcp_on and types.index("vcp") < types.index("close_vs_pivot")
    )
    if scored:
        assert "close_vs_pivot_not_hard" in codes_of(raw)
    elif not valid:
        assert codes_of(raw) == {"close_vs_pivot_needs_vcp"}
    elif vcp_at is not None and vcp_on:
        parse_config(raw)
    else:
        assert codes_of(raw) == {"level_not_exposed"}


@given(raw_configs())
def test_playbook_shape(raw: dict) -> None:
    """PBT config.playbook_shape: a playbook is a setup, a trigger list, entry filters, one stop
    rule and optionally its own universe, exit stack and validity window; the regime rule
    belongs to the bot, and a playbook without its own universe or exits uses the bot's."""
    config = parse_config(raw)
    stored = config.to_json()
    assert config.playbooks
    assert len({p.name for p in config.playbooks}) == len(config.playbooks)
    assert set(Playbook.model_fields) == {
        "name",
        "setup",
        "trigger",
        "entry_filters",
        "stop",
        "universe",
        "exits",
        "valid_sessions",
    }
    assert set(StrategyConfig.model_fields) >= {"regime", "exits", "playbooks", "universe"}
    leaked = copy.deepcopy(stored)
    leaked["playbooks"][0]["regime"] = stored["regime"]
    assert "extra_forbidden" in codes_of(leaked)
    first = config.playbooks[0].name
    assert config.exits_for(first) == config.exits
    assert config.universe_for(first) == config.universe
    twice = copy.deepcopy(stored)
    twice["playbooks"] = [stored["playbooks"][0]] * 2
    assert "duplicate_playbook" in codes_of(twice)


@given(raw_configs())
def test_exits_need_one_enabled_hard_stop(raw: dict) -> None:
    """A config is valid only with exactly one enabled hard_stop exit: none, a disabled one or
    two are refused. Harm: a backtest and shadow ride through the stop the live broker stop
    enforces, so the promotion evidence is of another strategy."""
    stored = parse_config(raw).to_json()
    others = [e for e in stored["exits"] if e["rule"] != "hard_stop"]
    for exits in (
        others,
        [{"rule": "hard_stop", "enabled": False}, *others],
        [{"rule": "hard_stop"}, {"rule": "hard_stop"}, *others],
    ):
        if exits:
            assert "one_enabled_hard_stop" in codes_of({**stored, "exits": exits})
    disabled_too = [{"rule": "hard_stop"}, {"rule": "hard_stop", "enabled": False}, *others]
    assert parse_config({**stored, "exits": disabled_too})


def inject(node: object, rnd: random.Random, key: str) -> bool:
    """Put `key` into a random dict inside `node`; False when none is reachable."""
    dicts: list[dict] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            dicts.append(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    rnd.choice(dicts)[key] = 1
    return True


RISK_NAMES = sorted(
    {*POLICY_FIELDS, "risk_pct", "position_size", "qty", "stop_price", "max_positions", "leverage"}
)


@given(raw_configs(), st.sampled_from(RISK_NAMES), st.randoms(use_true_random=False))
def test_no_risk_keys(raw: dict, key: str, rnd: random.Random) -> None:
    """PBT config.no_risk_keys: a config that names a risk or sizing
    key at any depth is refused with `risk_key`, however valid the rest is."""
    bad = copy.deepcopy(raw)
    inject(bad, rnd, key)
    assert "risk_key" in codes_of(bad)
    parse_config(raw)  # the clean one still passes


def dump_yaml(config: StrategyConfig) -> str:
    """The canonical config as YAML text, every default explicit and keys in model order."""
    return yaml.safe_dump(config.to_json(), sort_keys=False, allow_unicode=True)


@given(raw_configs())
def test_yaml_round_trip(raw: dict) -> None:
    """PBT config.yaml_round_trip: the canonical config dumped as YAML imports to the same
    config and hash; the two seeds round-trip too."""
    config = parse_config(raw)
    again = import_yaml(dump_yaml(config))
    assert again.hash() == config.hash()
    assert again.to_json() == config.to_json()


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_yaml_round_trip_seed(name: str) -> None:
    config = seed_config(name)
    assert import_yaml(dump_yaml(config)).hash() == config.hash()
    assert import_yaml(seed_text(name)).hash() == config.hash()


GATE_CALENDAR = calendar_around(date(2026, 12, 1), span=75)


SEED = (
    "sleeve: S3\npersona: Test-style\nregime: momentum\n---\nbot: {name}\nentry_filters:\n{filters}"
)
CHASE = "  - {{type: max_chase, params: {{pct: {pct}}}}}\n"


def test_registry_reads_a_seed() -> None:
    text = SEED.format(name="new-bot", filters=CHASE.format(pct=3) + CHASE.format(pct=1.5))
    strategy = parse_seed("new-bot", text)
    assert (strategy.sleeve, strategy.persona, strategy.regime) == ("S3", "Test-style", "momentum")
    assert strategy.max_chase_pct == Decimal("1.5")  # the strictest
    assert strategy.config_text == text.partition("\n---\n")[2]
    assert parse_seed("new-bot", SEED.format(name="new-bot", filters="")).max_chase_pct is None


@pytest.mark.parametrize(
    "text",
    [
        "bot: new-bot\n",  # no header
        "sleeve: S3\nregime: momentum\n---\nbot: new-bot\n",  # no persona
        "sleeve: S3\npersona: P\n---\nbot: new-bot\n",  # no regime profile
        "sleeve: S3\npersona: P\nregime: ' '\n---\nbot: new-bot\n",
        "sleeve: X3\npersona: P\nregime: momentum\n---\nbot: new-bot\n",
        'sleeve: "S3\\n"\npersona: P\nregime: momentum\n---\nbot: new-bot\n',  # a trailing newline
        "sleeve: S3\npersona: P\nregime: momentum\nrisk_pct: 1\n---\nbot: new-bot\n",
        "sleeve: S3\npersona: P\nregime: momentum\n---\nbot: other-bot\n",
    ],
)
def test_registry_refuses_a_bad_seed(text: str) -> None:
    with pytest.raises(RegistryError):
        parse_seed("new-bot", text)


def test_seed_policy_names_registered_strategies() -> None:
    assert set(SEED_POLICY["p_max"]) <= set(STRATEGIES)
    assert set(SEED_POLICY["base_weight"]) <= {s.sleeve for s in STRATEGIES.values()}


def test_save_draft_refuses_another_bot_name() -> None:
    """One identity: a strategy saves only a config whose `bot` is its own name (F3)."""
    saturday = datetime(2026, 12, 5, 12, tzinfo=UTC)
    with memory_session(ENGINE) as session:
        first, second = seed_default_strategies(session, USER)[:2]
        other_bot = seed_config(second.name)
        with pytest.raises(VersionError) as caught:
            save_draft(
                session,
                strategy_id=first.id,
                config=other_bot,
                calendar=GATE_CALENDAR,
                now=saturday,
            )
        assert caught.value.code == "bot_name_mismatch"
        draft = save_draft(
            session,
            strategy_id=first.id,
            config=seed_config(first.name),
            calendar=GATE_CALENDAR,
            now=saturday,
        )
        assert draft.version == 2


def test_playbook_kind_from_data_needs() -> None:
    """A playbook is pre-open when one of its criteria, enabled or not, declares the pre-open
    data (`data_needs`), whatever the playbook is called; the seeds' EP is the only one."""
    kinds = {(name, p.name): p.preopen for name in STRATEGIES for p in seed_config(name).playbooks}
    assert kinds == {
        ("stage2-contraction-breakout", "default"): False,
        ("momentum-breakout-ep", "breakout"): False,
        ("momentum-breakout-ep", "episodic_pivot"): True,
        ("darvas-box-breakout", "default"): False,
        ("trend-pullback-ema", "default"): False,
        ("rsi2-uptrend-reversion", "default"): False,
    }
    ep = seed_config("momentum-breakout-ep").playbook("episodic_pivot")
    assert ep.model_copy(update={"name": "gap_and_go"}).preopen
    raw = ep.model_dump(mode="json")
    raw["trigger"] = [i for i in raw["trigger"] if i["type"] != "gap_up"]
    assert not Playbook.model_validate(raw).preopen
    gap = next(i for i in ep.model_dump(mode="json")["trigger"] if i["type"] == "gap_up")
    raw["trigger"].append({**gap, "enabled": False, "order": 9})
    assert Playbook.model_validate(raw).preopen


def test_preopen_playbook_triggers_on_the_opening_range_only() -> None:
    """A pre-open playbook has no setup levels (its candidates come from the gap scan), so a
    `price_breaks_level` on any other level is refused when the config is saved."""
    raw = seed_config("momentum-breakout-ep").playbook("episodic_pivot").model_dump(mode="json")
    for instance in raw["trigger"]:
        if instance["type"] == "price_breaks_level":
            instance["params"]["level"] = "pivot"
    with pytest.raises(ValueError, match="preopen_level_not_opening_range"):
        Playbook.model_validate(raw)


def test_trigger_level_must_be_exposed() -> None:
    """A `price_breaks_level` names only levels its playbook's enabled setup criteria expose
    (`CriterionType.levels`) or the opening range, so a playbook that could never trigger is
    refused when saved. Harm: the backtest triggered on whatever `pivot` held."""
    raw = seed_config("momentum-breakout-ep").playbook("breakout").model_dump(mode="json")
    assert Playbook.model_validate(raw).setup_levels == ("consolidation_high",)
    for instance in raw["trigger"]:
        if instance["type"] == "price_breaks_level":
            instance["params"]["level"] = "pivot"  # no setup criterion of S2's breakout has one
    with pytest.raises(ValueError, match="level_not_exposed"):
        Playbook.model_validate(raw)
    for instance in raw["setup"]["criteria"]:
        if instance["type"] == "consolidation":
            instance["enabled"] = False
    for instance in raw["trigger"]:
        if instance["type"] == "price_breaks_level":
            instance["params"]["level"] = "consolidation_high"  # its criterion is disabled
    with pytest.raises(ValueError, match="level_not_exposed"):
        Playbook.model_validate(raw)


def test_one_trigger_level() -> None:
    """A playbook has one enabled `price_breaks_level`: live read the last passing one and the
    scan and backtest the first, so two naming different levels gave different entries. A second,
    disabled one is allowed; the live trigger's level is the one `daily_level` reads."""
    raw = seed_config("momentum-breakout-ep").playbook("breakout").model_dump(mode="json")
    extra = {
        "type": "price_breaks_level",
        "stage": "trigger",
        "order": 9,
        "params": {"level": "consolidation_high", "bar": "5m"},
    }
    with pytest.raises(ValueError, match="one_trigger_level"):
        Playbook.model_validate({**raw, "trigger": [*raw["trigger"], extra]})
    book = Playbook.model_validate(
        {**raw, "trigger": [*raw["trigger"], {**extra, "enabled": False}]}
    )
    levels = {"consolidation_high": Decimal(100), "opening_range_high": Decimal(90)}
    ctx = LiveContext(
        at=datetime(2026, 10, 21, 4, 0, tzinfo=UTC),
        price=Decimal(101),
        bar_high=Decimal(102),
        bar_close=Decimal(101),
        levels=levels,
    )
    level = evaluate_trigger(book, ctx, "confirm").level
    assert level == daily_level(book, levels) == Decimal(100)


def test_score_level_source_needs_its_level() -> None:
    """S1's pivot comes from a score-mode `vcp` and S2's breakout level from a score-mode
    `consolidation`: either can fail while the setup passes, and that candidate has no trigger
    level, so the scan does not watchlist it (`needs_setup_level` with `daily_level` None). An
    opening-range-only playbook needs no setup level."""
    s1 = seed_config("stage2-contraction-breakout").playbook("default")
    s2 = seed_config("momentum-breakout-ep")
    assert any(i.mode == "score" and i.type == "vcp" for i in s1.setup.criteria)
    assert needs_setup_level(s1)
    assert daily_level(s1, {}) is None
    assert needs_setup_level(s2.playbook("breakout"))
    assert daily_level(s2.playbook("breakout"), {"consolidation_high": None}) is None
    assert not needs_setup_level(s2.playbook("episodic_pivot"))


def test_seed_trigger_levels() -> None:
    """Each seed playbook's level expression and the setup levels the scan stores for it."""
    found = {
        (name, p.name): (p.level_expression, p.setup_levels)
        for name in STRATEGIES
        for p in seed_config(name).playbooks
    }
    pivot = ("pivot", ("pivot", "final_contraction_low"))
    assert found == {
        ("stage2-contraction-breakout", "default"): pivot,
        ("momentum-breakout-ep", "breakout"): (
            "max(consolidation_high, opening_range_high)",
            ("consolidation_high",),
        ),
        ("momentum-breakout-ep", "episodic_pivot"): ("opening_range_high", ()),
        ("darvas-box-breakout", "default"): pivot,
        ("trend-pullback-ema", "default"): pivot,
        ("rsi2-uptrend-reversion", "default"): ("pivot", ("pivot",)),
    }

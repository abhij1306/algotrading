"""PBT config.hash_canonical (HAT-SCH-016): the config hash is canonical."""

import copy
import json
import random
import uuid
from datetime import UTC, date, datetime, time

import pytest
import yaml
from hat.hat_config_support import (
    param_leaves,
    raw_configs,
    set_at,
    with_unknown_key,
)
from hat.hat_criteria_support import Lcg, frame, make_rows, universe_frame
from hat.hat_risk_support import AT, CALENDAR, calendar_around
from hat.hat_support import memory_engine
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.hat.confighash import canonical_json, config_hash
from app.hat.data.sessions import IST
from app.hat.engine.criteria import REGISTRY, EvalContext
from app.hat.engine.portfolio import lifecycle
from app.hat.engine.strategy.config import (
    ConfigError,
    Instance,
    Playbook,
    StrategyConfig,
    config_json,
    parse_config,
)
from app.hat.engine.strategy.scoring import evaluate_stage
from app.hat.engine.strategy.versions import (
    SEED_NAMES,
    VersionError,
    assign_version,
    create_bot,
    promote_version,
    publish_version,
    save_draft,
    seed_config,
    seed_default_strategies,
    seed_text,
    start_shadow,
)
from app.hat.engine.strategy.yamlio import export_yaml, import_yaml
from app.hat.models import HatBacktestRun, HatBot, HatEvent, HatStrategyVersion
from app.hat.risk.policy import POLICY_FIELDS
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


@given(config=configs, seed=st.integers(), indent=st.sampled_from([None, 0, 1, 4, "\t"]))
def test_hash_canonical(config: dict, seed: int, indent: object) -> None:
    """Key order and JSON whitespace never change the hash."""
    reordered = shuffled(config, random.Random(seed))  # noqa: S311 - shuffling test data
    text_form = json.dumps(reordered, indent=indent, sort_keys=False, ensure_ascii=bool(seed % 2))
    assert config_hash(json.loads(text_form)) == config_hash(config)
    assert canonical_json(json.loads(text_form)) == canonical_json(config)


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


# --- the strategy config model (HAT-CRIT-020..022, HAT-BOT-050..082) --------------------------

ENGINE = memory_engine()
seed_hat_reference(ENGINE)
NIGHT = AT.replace(hour=20)
USER = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
ACCOUNT = uuid.UUID("00000000-0000-7000-8000-0000000000c1")


def all_instances(config: StrategyConfig) -> list[Instance]:
    out = [*config.universe]
    for playbook in config.playbooks:
        out += [*playbook.setup.criteria, *playbook.trigger, *playbook.entry_filters]
    return out


def codes_of(raw: dict) -> set[str]:
    with pytest.raises(ConfigError) as caught:
        parse_config(raw)
    return caught.value.codes


@given(raw_configs())
def test_instance_fields(raw: dict) -> None:
    """PBT config.instance_fields (HAT-CRIT-020): every stored instance carries exactly the seven
    fields, parameters spelled out in full; `weight` has no effect on a hard criterion."""
    config = parse_config(raw)
    stored = config.to_json()
    fields = {"type", "stage", "mode", "weight", "params", "enabled", "order"}
    for instance in all_instances(config):
        assert set(Instance.model_fields) == fields
        assert instance.mode in ("hard", "score")
        assert set(instance.params) == set(REGISTRY[instance.type].params.model_fields)
    assert stored["universe"] == [i.model_dump(mode="json") for i in config.universe]
    # a hard criterion's weight is ignored: compare outcomes on a fixed context
    ctx = EvalContext(
        date(2024, 1, 2),
        "x",
        frame(make_rows(Lcg(1), 1)),
        universe=universe_frame([("x", {"rs_pct": 50.0})]),
        series="EQ",
    )
    for instance in config.universe:
        if instance.mode != "hard":
            continue
        a = evaluate_stage("universe", [instance], ctx)
        b = evaluate_stage("universe", [instance.model_copy(update={"weight": 7.0})], ctx)
        assert (a.passed, a.score, a.reason) == (b.passed, b.score, b.reason)


@given(raw_configs(), st.randoms(use_true_random=False))
def test_stage_slots(raw: dict, rnd: random.Random) -> None:
    """PBT config.stage_slots (HAT-CRIT-021, 022): an instance only fits a section its type
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
    """PBT config.close_vs_pivot_needs_vcp (HAT-CRIT-123): a setup with an enabled
    `close_vs_pivot` is valid exactly when an enabled `vcp` comes before it (S1's seed setup
    with the two moved, removed or switched off), and `close_vs_pivot` is refused in score mode.
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
    elif valid:
        parse_config(raw)
    else:
        assert codes_of(raw) == {"close_vs_pivot_needs_vcp"}


@given(raw_configs(), st.data())
def test_invalid_never_saves(raw: dict, data: st.DataObject) -> None:
    """PBT config.invalid_never_saves (HAT-CRIT-020, HAT-BOT-081): a config with an unknown
    type, a wrong stage, an unknown or out-of-range parameter or a duplicate order is refused
    with a coded error and no version row or event is written."""
    leaves = param_leaves(raw)
    assume(leaves)
    breakage = data.draw(st.sampled_from(["unknown_param", "bad_value", "unknown_type", "dup"]))
    bad = copy.deepcopy(raw)
    leaf = data.draw(st.sampled_from(leaves))
    if breakage == "unknown_param":
        bad = with_unknown_key(raw, leaf, "zzz_unknown")
    elif breakage == "bad_value":
        bad = set_at(raw, leaf, {"not": "a scalar"})
    elif breakage == "unknown_type":
        holder = bad
        for part in leaf[: leaf.index("params")]:
            holder = holder[part]
        holder["type"] = "no_such_criterion"
    else:
        assume(len(bad["universe"]) >= 1)
        first = bad["universe"][0]
        first["stage"], first["order"] = "universe", 0
        bad["universe"] = [first, {**copy.deepcopy(first)}]
    with Session(ENGINE) as session:
        strategy = seed_default_strategies(session, USER)[0]
        before = (
            session.scalar(select(func.count()).select_from(HatStrategyVersion)),
            session.scalar(select(func.count()).select_from(HatEvent)),
        )
        with pytest.raises(ConfigError):
            save_draft(session, strategy_id=strategy.id, config=bad, calendar=CALENDAR, now=NIGHT)
        after = (
            session.scalar(select(func.count()).select_from(HatStrategyVersion)),
            session.scalar(select(func.count()).select_from(HatEvent)),
        )
        session.rollback()
    assert before == after


@given(raw_configs())
def test_playbook_shape(raw: dict) -> None:
    """PBT config.playbook_shape (HAT-BOT-050, 051): a playbook is a setup, a trigger list,
    entry filters and one stop rule; the regime rule and the exit stack belong to the bot."""
    config = parse_config(raw)
    stored = config.to_json()
    assert config.playbooks
    assert len({p.name for p in config.playbooks}) == len(config.playbooks)
    assert set(Playbook.model_fields) == {"name", "setup", "trigger", "entry_filters", "stop"}
    assert set(StrategyConfig.model_fields) >= {"regime", "exits", "playbooks", "universe"}
    for name in ("regime", "exits"):
        leaked = copy.deepcopy(stored)
        leaked["playbooks"][0][name] = stored[name]
        assert "extra_forbidden" in codes_of(leaked)
    twice = copy.deepcopy(stored)
    twice["playbooks"] = [stored["playbooks"][0]] * 2
    assert "duplicate_playbook" in codes_of(twice)


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
    """PBT config.no_risk_keys (HAT-DEC-B3, HAT-BOT-070): a config that names a risk or sizing
    key at any depth is refused with `risk_key`, however valid the rest is."""
    bad = copy.deepcopy(raw)
    inject(bad, rnd, key)
    assert "risk_key" in codes_of(bad)
    parse_config(raw)  # the clean one still passes


@given(raw_configs())
def test_yaml_round_trip(raw: dict) -> None:
    """PBT config.yaml_round_trip (HAT-BOT-082): export then import gives the same config and
    hash, and the text is stable under a second export; the two seeds round-trip too."""
    config = parse_config(raw)
    text = export_yaml(config)
    again = import_yaml(text)
    assert again.hash() == config.hash()
    assert again.to_json() == config.to_json()
    assert export_yaml(again) == text
    assert config_json(yaml.safe_load(text)) == config.to_json()


@pytest.mark.parametrize("name", SEED_NAMES)
def test_yaml_round_trip_seed(name: str) -> None:
    config = seed_config(name)
    assert import_yaml(export_yaml(config)).hash() == config.hash()
    assert import_yaml(seed_text(name)).hash() == config.hash()


def add_run(session: Session, version: HatStrategyVersion) -> HatBacktestRun:
    """A backtest run row for `version` (what publishing needs as evidence)."""
    run = HatBacktestRun(
        id=uuid.uuid4(),
        strategy_version_id=version.id,
        risk_policy_version_id=uuid.uuid4(),
        family_key="probe",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        data_snapshot={},
        status="completed",
        metrics={},
    )
    session.add(run)
    session.flush()
    return run


def row_of(session: Session, version_id: uuid.UUID) -> HatStrategyVersion:
    """A version of the same strategy other than `version_id` (the first saved one)."""
    version = session.get(HatStrategyVersion, version_id)
    assert version is not None
    first = session.scalars(
        select(HatStrategyVersion)
        .where(HatStrategyVersion.strategy_id == version.strategy_id)
        .order_by(HatStrategyVersion.version)
    ).first()
    assert first is not None
    return first


@given(st.lists(raw_configs(), min_size=1, max_size=4))
@settings(max_examples=25, deadline=None)
def test_save_creates_version(raws: list[dict]) -> None:
    """PBT config.save_creates_version (HAT-BOT-080, 081, 084): each save adds one draft with
    the next number, no gaps, linked to its parent, config and hash as validated; publishing
    freezes a draft; inside the market session nothing is written."""
    with Session(ENGINE) as session:
        market = seed_default_strategies(session, uuid.uuid4())[0]
        base = session.scalar(
            select(func.count())
            .select_from(HatStrategyVersion)
            .where(HatStrategyVersion.strategy_id == market.id)
        )
        previous = None
        for index, raw in enumerate(raws):
            row = save_draft(
                session, strategy_id=market.id, config=raw, calendar=CALENDAR, now=NIGHT
            )
            assert row.version == base + index + 1
            assert row.status == "draft"
            assert row.parent_version_id == (previous.id if previous else row.parent_version_id)
            assert row.config_hash == parse_config(raw).hash()
            assert row.config == parse_config(raw).to_json()
            previous = row
        numbers = [
            v.version
            for v in session.scalars(
                select(HatStrategyVersion).where(HatStrategyVersion.strategy_id == market.id)
            )
        ]
        assert sorted(numbers) == list(range(1, len(numbers) + 1))
        count = session.scalar(select(func.count()).select_from(HatStrategyVersion))
        with pytest.raises(VersionError) as caught:
            save_draft(session, strategy_id=market.id, config=raws[0], calendar=CALENDAR, now=AT)
        assert caught.value.code == "market_hours"
        assert session.scalar(select(func.count()).select_from(HatStrategyVersion)) == count
        assert previous is not None
        other = add_run(session, row_of(session, previous.id))
        with pytest.raises(VersionError) as refused:  # evidence from another version
            publish_version(session, previous.id, backtest_run_id=other.id)
        assert refused.value.code in ("run_not_for_version", "unknown_run")
        run = add_run(session, previous)
        frozen = publish_version(session, previous.id, backtest_run_id=run.id)
        assert frozen.status == "backtested"
        with pytest.raises(VersionError):
            publish_version(session, previous.id, backtest_run_id=run.id)
        session.rollback()


@given(st.sampled_from([None, "shadow", "confirm", "auto"]))
def test_new_bot_mode_confirm(mode: str | None) -> None:
    """PBT config.new_bot_mode_confirm (HAT-BOT-061, HAT-DEC-B2): a new bot asks for confirm
    whatever mode its config names; asking creation for any other mode is refused. It still
    runs shadow: its sleeve starts in research (HAT-PORT-063)."""
    with Session(ENGINE) as session:
        strategy = seed_default_strategies(session, USER)[0]
        if mode in (None, "confirm"):
            bot = create_bot(
                session,
                user_id=USER,
                strategy_id=strategy.id,
                broker_account_id=ACCOUNT,
                execution_mode=mode,
            )
            assert bot.execution_mode == "confirm"
            assert bot.lifecycle_state == "research"
            assert isinstance(bot, HatBot)
        else:
            with pytest.raises(VersionError):
                create_bot(
                    session,
                    user_id=USER,
                    strategy_id=strategy.id,
                    broker_account_id=ACCOUNT,
                    execution_mode=mode,
                )
        session.rollback()


@settings(max_examples=20)
@given(st.integers(60, 99))
def test_trade_keeps_entry_version(price: int) -> None:
    """HAT-BOT-083: a version promoted after the entry never changes the exit rules of an open
    trade. The trade entered under a stack whose `close_below_sma` exits below 100; the bot then
    moves to a version with the stop alone; the trade still closes on that rule, and a trade
    opened after the move does not. Harm: a promotion silently rewrites the exits of positions
    already held."""
    from decimal import Decimal

    from hat.hat_engine_support import exit_stack, open_trade, seed_engine_world, step_trade

    from app.hat.engine import trades
    from app.hat.models import HatPosition, HatSignal

    with Session(memory_engine()) as session:
        world = seed_engine_world(session, exits=exit_stack("trend"))
        old = open_trade(session, world, world.instruments[0])
        newer = HatStrategyVersion(
            id=uuid.uuid4(),
            strategy_id=world.strategy_id,
            version=2,
            status="draft",
            config={
                **session.get(HatStrategyVersion, world.version_id).config,
                "exits": [{"rule": "hard_stop"}],
            },
            config_hash="h2",
        )
        session.add(newer)
        session.flush()
        session.get(HatBot, world.bot_id).live_version_id = newer.id
        session.flush()
        new = open_trade(session, world, world.instruments[1])
        for trade, rules in (
            (old, ["hard_stop", "move_stop_to_breakeven", "close_below_sma", "time_stop"]),
            (new, ["hard_stop"]),
        ):
            signal = session.get(HatSignal, trade.outcome.signal_id)
            assert [r.rule for r in trades.exit_rules_of(session, signal)] == rules
        step_trade(session, old, Decimal(price))
        step_trade(session, new, Decimal(price))
        old_pos = session.get(HatPosition, old.outcome.position_id)
        new_pos = session.get(HatPosition, new.outcome.position_id)
        # price < 100 is below the old stack's sma(10) = 100, and above the 95 stop for 96..99
        assert (old_pos.closed_at is not None) is True
        assert (new_pos.closed_at is not None) == (price <= 95)


GATE_CALENDAR = calendar_around(date(2026, 12, 1), span=75)
GATE_START = date(2026, 10, 1)


def gate_night(after: int) -> datetime:
    """20:00 IST of the `after`-th trading session after GATE_START, as UTC (0: GATE_START)."""
    days = [d for d in GATE_CALENDAR.trading_days() if d > GATE_START]
    day = days[after - 1] if after else GATE_START
    return datetime.combine(day, time(20, 0), tzinfo=IST).astimezone(UTC)


def released(session: Session, strategy_id: uuid.UUID, at: datetime) -> HatStrategyVersion:
    """A new version of the strategy, published on a run and in shadow from `at`."""
    draft = save_draft(
        session,
        strategy_id=strategy_id,
        config=seed_config(SEED_NAMES[0]),
        calendar=GATE_CALENDAR,
        now=at,
    )
    publish_version(session, draft.id, backtest_run_id=add_run(session, draft).id)
    return start_shadow(session, draft.id, calendar=GATE_CALENDAR, now=at)


@settings(max_examples=40, deadline=None)
@given(
    state=st.sampled_from(lifecycle.STATES),
    waited=st.integers(0, 30),
    replaces=st.booleans(),
    in_hours=st.booleans(),
)
@example(state="live", waited=20, replaces=True, in_hours=False)
@example(state="shadow", waited=19, replaces=False, in_hours=False)
def test_release_gate(state: str, waited: int, replaces: bool, in_hours: bool) -> None:
    """PBT config.release_gate (HAT-BOT-084, HAT-GOV-083): a version goes live only from shadow,
    outside market hours, after at least 20 trading sessions of shadow; it retires the version it
    replaces and moves that version's sleeves onto it in the same step, with one
    `strategy.promoted` event; a refusal changes nothing. A sleeve that places real orders (Live,
    Probation) is never pointed at a version that is not live. Harm: an untested edit trades real
    money, or two live versions of one strategy split the sleeves."""
    with Session(ENGINE) as session:
        strategy = seed_default_strategies(session, uuid.uuid4())[0]
        previous = None
        start = 0
        if replaces:
            previous = released(session, strategy.id, gate_night(0))
            promote_version(
                session, previous.id, calendar=GATE_CALENDAR, now=gate_night(20), chat_id=7
            )
            start = 20
        version = released(session, strategy.id, gate_night(start))
        bot = HatBot(
            id=uuid.uuid4(),
            user_id=strategy.user_id,
            strategy_id=strategy.id,
            broker_account_id=ACCOUNT,
            execution_mode="shadow",
            status="active",
            lifecycle_state=state,
            risk_cluster="momentum-long",
            live_version_id=None if previous is None else previous.id,
        )
        session.add(bot)
        session.flush()
        real_money = state in (lifecycle.LIVE, lifecycle.PROBATION)
        if real_money:
            with pytest.raises(VersionError) as refused:
                assign_version(session, bot_id=bot.id, version_id=version.id)
            assert refused.value.code == "version_not_live"
        else:
            assert assign_version(session, bot_id=bot.id, version_id=version.id) is bot
            bot.live_version_id = None if previous is None else previous.id
        session.flush()
        now = gate_night(start + waited)
        if in_hours:
            now = now.replace(hour=5)  # 10:30 IST, inside the session
        events = session.scalar(select(func.count()).select_from(HatEvent))
        if in_hours or waited < lifecycle.SHADOW_DAYS:
            with pytest.raises(VersionError) as refused:
                promote_version(session, version.id, calendar=GATE_CALENDAR, now=now, chat_id=7)
            assert refused.value.code == ("market_hours" if in_hours else "shadow_days")
            assert version.status == "shadow"
            assert previous is None or previous.status == "live"
            assert bot.live_version_id == (None if previous is None else previous.id)
            assert session.scalar(select(func.count()).select_from(HatEvent)) == events
        else:
            promote_version(session, version.id, calendar=GATE_CALENDAR, now=now, chat_id=7)
            assert version.status == "live"
            live = session.scalars(
                select(HatStrategyVersion).where(
                    HatStrategyVersion.strategy_id == strategy.id,
                    HatStrategyVersion.status == "live",
                )
            ).all()
            assert live == [version]
            assert previous is None or previous.status == "retired"
            assert bot.live_version_id == (None if previous is None else version.id)
            promoted = session.scalars(
                select(HatEvent).where(
                    HatEvent.event_type == "strategy.promoted", HatEvent.aggregate_id == version.id
                )
            ).all()
            assert len(promoted) == 1
            assert promoted[0].payload["bots"] == ([str(bot.id)] if previous else [])
            assert promoted[0].payload["chat_id"] == 7
            assert assign_version(session, bot_id=bot.id, version_id=version.id) is bot
        session.rollback()

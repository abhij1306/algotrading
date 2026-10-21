"""PBT config.hash_canonical (HAT-SCH-016): the config hash is canonical."""

import json
import random

from hypothesis import assume, given
from hypothesis import strategies as st

from app.hat.confighash import canonical_json, config_hash

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

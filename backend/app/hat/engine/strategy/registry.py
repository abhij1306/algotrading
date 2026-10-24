"""The strategy registry: the one source of strategy identity.

Each file `seeds/<name>.yaml` registers one strategy. It holds two YAML documents: a header with
the strategy's `sleeve` (its key in the risk policy's `base_weight`) and `persona`, then, after a
`---` line, the strategy config itself, whose `bot` must be `<name>`. The registry also reads the
strategy's documented max-chase default (HAT-CRIT-122) from the `max_chase` filters of that config.

Adding a strategy is one seed file here, plus any new criteria in `engine.criteria`; its risk
numbers (`p_max`, `base_weight`) go into the risk policy through a policy version, never here
(bots hold no risk numbers). Imports nothing from the database, broker or risk code, so the risk
rules can read it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from types import MappingProxyType

import yaml

from ...errors import HatError
from ...money import require_exact

SEPARATOR = "\n---\n"
SLEEVE = re.compile(r"S[1-9]\d*")
HEADER_FIELDS = frozenset({"sleeve", "persona"})


class RegistryError(HatError):
    """A seed file does not register a strategy: bad header, name or sleeve."""


@dataclass(frozen=True)
class Strategy:
    """A registered strategy. `config_text` is the seed config YAML, verbatim."""

    name: str
    sleeve: str
    persona: str
    max_chase_pct: Decimal | None
    config_text: str


def _max_chase_pcts(node: object) -> Iterator[Decimal]:
    if isinstance(node, Mapping):
        if node.get("type") == "max_chase":
            yield require_exact(node["params"]["pct"], "max_chase.pct", floats=True)
        for value in node.values():
            yield from _max_chase_pcts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _max_chase_pcts(value)


def parse_seed(name: str, text: str) -> Strategy:
    """The strategy a seed file registers, or `RegistryError`."""
    head, sep, body = text.partition(SEPARATOR)
    header = yaml.safe_load(head)
    if not sep or not isinstance(header, Mapping) or set(header) != HEADER_FIELDS:
        raise RegistryError(f"{name}: needs a header of exactly {sorted(HEADER_FIELDS)}")
    sleeve, persona = header["sleeve"], header["persona"]
    if not isinstance(sleeve, str) or not SLEEVE.fullmatch(sleeve):
        raise RegistryError(f"{name}: sleeve must look like S1")
    if not isinstance(persona, str) or not persona.strip():
        raise RegistryError(f"{name}: persona must be text")
    config = yaml.safe_load(body)
    if not isinstance(config, Mapping) or config.get("bot") != name:
        raise RegistryError(f"{name}: the config's bot must be the file name")
    # The strictest when playbooks differ.
    max_chase = min(_max_chase_pcts(config), default=None)
    return Strategy(name, sleeve, persona, max_chase, body)


def _load() -> Mapping[str, Strategy]:
    seeds = resources.files(__package__).joinpath("seeds")
    found = [
        parse_seed(path.name.removesuffix(".yaml"), path.read_text("utf-8"))
        for path in seeds.iterdir()
        if path.name.endswith(".yaml")
    ]
    if not found:
        raise RegistryError("no seed files: every strategy is registered by one")
    sleeves = [s.sleeve for s in found]
    if len(set(sleeves)) != len(sleeves):
        raise RegistryError("two seeds share a sleeve")
    ordered = sorted(found, key=lambda s: int(s.sleeve[1:]))
    return MappingProxyType({s.name: s for s in ordered})


# Every registered strategy by name, in sleeve order (S1, S2, ...).
STRATEGIES: Mapping[str, Strategy] = _load()

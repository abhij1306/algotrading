"""YAML import of a strategy config (HAT-BOT-082): the canonical config dumped as YAML imports to
an equal config with the same `config_hash`. Only plain data is read (`safe_load`), and input is
capped."""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from .config import ConfigError, StrategyConfig, parse_config

MAX_YAML_BYTES = 256 * 1024


def import_yaml(text: str) -> StrategyConfig:
    """Parse YAML text (the seeds' shape or a canonical dump) into a validated config, or raise
    `ConfigError`."""
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise ConfigError([("", "yaml_too_large")])
    try:
        raw = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ConfigError([("", "bad_yaml")]) from None
    if not isinstance(raw, dict):
        raise ConfigError([("", "not_a_mapping")])
    try:
        return parse_config(raw)
    except ValidationError:  # pragma: no cover - parse_config converts these
        raise ConfigError([("", "invalid")]) from None

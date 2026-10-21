"""Canonical JSON and the sha256 `config_hash` of a config (HAT-SCH-016)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def _without_negative_zero(value: object) -> object:
    """0.0 and -0.0 are one number, so they must be one text."""
    if isinstance(value, dict):
        return {key: _without_negative_zero(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_negative_zero(item) for item in value]
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def canonical_json(config: Mapping[str, object]) -> str:
    """Keys sorted at every depth, no whitespace, non-ASCII escaped: one text per config value.

    `-0.0` is written as `0.0`. Only JSON data is accepted (dict, list, str, int, float, bool,
    None); NaN and infinity raise. ASCII escaping keeps any string, even a lone surrogate,
    hashable.
    """
    return json.dumps(
        _without_negative_zero(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def config_hash(config: Mapping[str, object]) -> str:
    """sha256 hex digest of the canonical JSON of `config`."""
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()

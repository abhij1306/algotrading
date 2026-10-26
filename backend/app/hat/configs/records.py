"""The config records HAT keeps as versioned JSON files (`configs.store`), not database rows:
strategies and their versions, risk-policy versions, regime-model versions, the criterion
catalogue, bots and broker accounts (docs/architecture.md, Storage).

Each record kind declares its fields once (`FIELDS`, name -> type tag); the JSON schema every
file is validated against on load and before a write is built from that declaration, with the
kind's value domains (`ENUMS`). `MUTABLE` names the fields a later revision of a record may
change: every other field is fixed by the record's first revision (`immutable_change`).
`record_rules` holds the per-record rules the database CHECKs used to keep, `kind_rules` the rules
across a kind's records its unique indexes kept. Pure.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, ClassVar

from ..errors import HatError
from ..governance import MODE_VALUES, VersionStatus, require_cooling_off
from ..ids import uuid7
from ..time import as_utc, utc_now

__all__ = [
    "KINDS",
    "Bot",
    "BrokerAccount",
    "ConfigRecordError",
    "CriterionType",
    "Record",
    "RegimeModelVersion",
    "RiskPolicyVersion",
    "Strategy",
    "StrategyVersion",
    "doc_of",
    "immutable_change",
    "kind_rules",
    "kind_schema",
    "record_from_doc",
    "record_rules",
]

# `hat_broker_account.status` and `.broker` values, the one copy every reader imports.
ACCOUNT_PENDING = "pending"
ACCOUNT_ACTIVE = "active"  # passed its capability check (HAT-BRK-092)
ACCOUNT_FAILED = "failed"
BROKER_FYERS = "fyers"
BROKER_ZERODHA = "zerodha"
# the brokers HAT has an adapter for; `HAT_BROKER` names the one active broker among them
BROKERS = (BROKER_FYERS, BROKER_ZERODHA)

BOT_STATUSES = ("active", "paused", "manage_only")
LIFECYCLE_STATES = ("research", "backtested", "shadow", "live", "probation", "retired")
CRITERION_STAGES = ("universe", "setup", "trigger", "entry_filter")
POLICY_DIRECTIONS = ("initial", "increase", "decrease")
REGIME_KINDS = ("rules", "hmm", "gmm")


class ConfigRecordError(HatError, ValueError):
    """A config record or file breaks its kind's schema or rules; `code` names the rule."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# Type tags: a trailing `?` makes the field nullable.
_SCHEMA_OF = {
    "uuid": {"type": "string", "format": "uuid", "pattern": "^[0-9a-f-]{36}$"},
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "bool": {"type": "boolean"},
    "datetime": {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT"},
    "object": {"type": "object"},
    "list": {"type": "array"},
}


@dataclass(eq=False)
class Record:
    """One config record. The instances a session reads are the ones it changes: a field set
    on one is written as the record's next revision when the session commits (`configs.unit`)."""

    KIND: ClassVar[str]
    FIELDS: ClassVar[dict[str, str]]
    MUTABLE: ClassVar[frozenset[str]] = frozenset()
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {}

    @property
    def key(self) -> str:
        """The record's id in its kind's folder."""
        return str(getattr(self, "id"))  # noqa: B009 - every kind but the catalogue has an id


@dataclass(eq=False)
class Strategy(Record):
    KIND: ClassVar[str] = "strategy"
    FIELDS: ClassVar[dict[str, str]] = {
        "id": "uuid",
        "user_id": "uuid",
        "market_id": "uuid",
        "name": "str",
        "persona": "str",
        "created_at": "datetime",
    }

    user_id: uuid.UUID
    market_id: uuid.UUID
    name: str
    persona: str
    created_at: datetime = field(default_factory=utc_now)

    id: uuid.UUID = field(default_factory=uuid7)


@dataclass(eq=False)
class StrategyVersion(Record):
    """A strategy's config at one version. The config never changes after the version is
    created; its release state and evidence move on (`engine.strategy.versions`, `.release`).
    `backtest_run_id` is the id of the run folder that evidences it (`backtests/runs/<id>`)."""

    KIND: ClassVar[str] = "strategy_version"
    FIELDS: ClassVar[dict[str, str]] = {
        "id": "uuid",
        "strategy_id": "uuid",
        "version": "int",
        "status": "str",
        "config": "object",
        "config_hash": "str",
        "parent_version_id": "uuid?",
        "backtest_run_id": "str?",
        "shadow_started_at": "datetime?",
        "promoted_at": "datetime?",
        "created_at": "datetime",
    }
    MUTABLE: ClassVar[frozenset[str]] = frozenset(
        {"status", "backtest_run_id", "shadow_started_at", "promoted_at"}
    )
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {"status": tuple(VersionStatus)}

    strategy_id: uuid.UUID
    version: int
    status: str
    config: dict[str, Any]
    config_hash: str
    parent_version_id: uuid.UUID | None = None
    backtest_run_id: str | None = None
    shadow_started_at: datetime | None = None
    promoted_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)

    id: uuid.UUID = field(default_factory=uuid7)


@dataclass(eq=False)
class RiskPolicyVersion(Record):
    """One version of the owner's risk policy: immutable once written, except that it is
    superseded once (HAT-SCH-084's cooling-off holds for an increase)."""

    KIND: ClassVar[str] = "risk_policy_version"
    FIELDS: ClassVar[dict[str, str]] = {
        "id": "uuid",
        "user_id": "uuid",
        "version": "int",
        "policy": "object",
        "direction": "str",
        "requested_at": "datetime",
        "effective_from": "datetime",
        "gate_evidence": "object?",
        "superseded_at": "datetime?",
    }
    MUTABLE: ClassVar[frozenset[str]] = frozenset({"superseded_at"})
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {"direction": POLICY_DIRECTIONS}

    user_id: uuid.UUID
    version: int
    policy: dict[str, Any]
    direction: str
    effective_from: datetime
    requested_at: datetime = field(default_factory=utc_now)
    gate_evidence: dict[str, Any] | None = None
    superseded_at: datetime | None = None

    id: uuid.UUID = field(default_factory=uuid7)


@dataclass(eq=False)
class BrokerAccount(Record):
    """A broker account. `ddpi_confirmed` is the owner's word that DDPI (or a POA) is registered
    on the demat, set only through the admin command line with the time and a dated note; until
    then no bot on the account runs confirm or auto (HAT-GOV-085)."""

    KIND: ClassVar[str] = "broker_account"
    FIELDS: ClassVar[dict[str, str]] = {
        "id": "uuid",
        "user_id": "uuid",
        "broker": "str",
        "account_ref": "str",
        "capabilities": "object",
        "status": "str",
        "ddpi_confirmed": "bool",
        "ddpi_confirmed_at": "datetime?",
        "ddpi_note": "str?",
    }
    MUTABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "account_ref",
            "capabilities",
            "status",
            "ddpi_confirmed",
            "ddpi_confirmed_at",
            "ddpi_note",
        }
    )
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {"broker": BROKERS}

    user_id: uuid.UUID
    broker: str
    account_ref: str
    capabilities: dict[str, Any]
    status: str
    ddpi_confirmed: bool = False
    ddpi_confirmed_at: datetime | None = None
    ddpi_note: str | None = None

    id: uuid.UUID = field(default_factory=uuid7)


@dataclass(eq=False)
class Bot(Record):
    """The sleeve. It holds no risk numbers: every one lives in the risk policy."""

    KIND: ClassVar[str] = "bot"
    FIELDS: ClassVar[dict[str, str]] = {
        "id": "uuid",
        "user_id": "uuid",
        "strategy_id": "uuid",
        "live_version_id": "uuid?",
        "broker_account_id": "uuid",
        "execution_mode": "str",
        "status": "str",
        "universe_version_id": "uuid?",
        "lifecycle_state": "str",
        "risk_cluster": "str",
    }
    MUTABLE: ClassVar[frozenset[str]] = frozenset(
        {
            "live_version_id",
            "execution_mode",
            "status",
            "universe_version_id",
            "lifecycle_state",
            "risk_cluster",
        }
    )
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {
        "execution_mode": tuple(sorted(MODE_VALUES)),
        "status": BOT_STATUSES,
        "lifecycle_state": LIFECYCLE_STATES,
    }

    user_id: uuid.UUID
    strategy_id: uuid.UUID
    broker_account_id: uuid.UUID
    status: str
    lifecycle_state: str
    risk_cluster: str
    # Confirm by default (HAT-DEC-B2, D130/D132): the mode the owner wants. It routes shadow
    # until the sleeve is Live and the account and policy gates pass (`execution.modes`).
    execution_mode: str = "confirm"
    live_version_id: uuid.UUID | None = None
    universe_version_id: uuid.UUID | None = None

    id: uuid.UUID = field(default_factory=uuid7)


@dataclass(eq=False)
class RegimeModelVersion(Record):
    KIND: ClassVar[str] = "regime_model_version"
    FIELDS: ClassVar[dict[str, str]] = {
        "id": "uuid",
        "market_id": "uuid",
        "kind": "str",
        "version": "int",
        "status": "str",
        "config": "object",
    }
    MUTABLE: ClassVar[frozenset[str]] = frozenset({"status"})
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {"kind": REGIME_KINDS}

    market_id: uuid.UUID
    kind: str
    version: int
    status: str
    config: dict[str, Any]

    id: uuid.UUID = field(default_factory=uuid7)


@dataclass(eq=False)
class CriterionType(Record):
    """One criterion of the catalogue at one version, keyed `<code>@<version>`."""

    KIND: ClassVar[str] = "criterion_type"
    FIELDS: ClassVar[dict[str, str]] = {
        "code": "str",
        "version": "int",
        "stage": "str",
        "param_schema": "object",
        "data_needs": "list",
    }
    MUTABLE: ClassVar[frozenset[str]] = frozenset({"stage", "param_schema", "data_needs"})
    ENUMS: ClassVar[dict[str, tuple[str, ...]]] = {"stage": CRITERION_STAGES}

    code: str
    version: int
    stage: str
    param_schema: dict[str, Any]
    data_needs: list[Any]

    @property
    def key(self) -> str:
        return f"{self.code}@{self.version}"


KINDS: dict[str, type[Record]] = {
    cls.KIND: cls
    for cls in (
        Strategy,
        StrategyVersion,
        RiskPolicyVersion,
        BrokerAccount,
        Bot,
        RegimeModelVersion,
        CriterionType,
    )
}


def kind_schema(cls: type[Record]) -> dict[str, Any]:
    """The JSON schema of one revision file of `cls`'s kind: the envelope and the record."""
    properties: dict[str, Any] = {}
    for name, tag in cls.FIELDS.items():
        base = dict(_SCHEMA_OF[tag.rstrip("?")])
        if name in cls.ENUMS:
            base["enum"] = list(cls.ENUMS[name])
        properties[name] = {"anyOf": [base, {"type": "null"}]} if tag.endswith("?") else base
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "id", "rev", "written_at", "record"],
        "properties": {
            "kind": {"const": cls.KIND},
            "id": {"type": "string", "minLength": 1},
            "rev": {"type": "integer", "minimum": 1},
            "written_at": _SCHEMA_OF["datetime"],
            "record": {
                "type": "object",
                "additionalProperties": False,
                "required": list(cls.FIELDS),
                "properties": properties,
            },
        },
    }


def _to_json(tag: str, value: Any) -> Any:  # noqa: ANN401 - a field value of any tag
    if value is None:
        return None
    base = tag.rstrip("?")
    if base == "uuid":
        return str(uuid.UUID(str(value)))
    if base == "datetime":
        return as_utc(value).isoformat()
    if base in ("object", "list"):
        return copy.deepcopy(value)  # never share a nested value with a cached revision
    if base == "str":
        return str(value)
    return value


def _from_json(tag: str, value: Any) -> Any:  # noqa: ANN401 - a field value of any tag
    if value is None:
        return None
    base = tag.rstrip("?")
    if base == "uuid":
        return uuid.UUID(value)
    if base == "datetime":
        return as_utc(datetime.fromisoformat(value))
    if base in ("object", "list"):
        return copy.deepcopy(value)  # never share a nested value with a cached revision
    return value


def doc_of(record: Record) -> dict[str, Any]:
    """The record's fields as JSON values (uuids and instants as text, aware UTC)."""
    return {name: _to_json(tag, getattr(record, name)) for name, tag in record.FIELDS.items()}


def record_from_doc(cls: type[Record], doc: dict[str, Any]) -> Record:
    """The record a validated revision's `record` object holds."""
    names = {f.name for f in fields(cls)}
    return cls(**{n: _from_json(t, doc[n]) for n, t in cls.FIELDS.items() if n in names})


def immutable_change(cls: type[Record], before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The fields outside `cls.MUTABLE` that differ between two revisions of one record (empty
    when the later revision keeps every fixed field)."""
    return sorted(n for n in cls.FIELDS if n not in cls.MUTABLE and before.get(n) != after.get(n))


def record_rules(cls: type[Record], before: dict[str, Any] | None, after: dict[str, Any]) -> None:
    """The per-record rules the database CHECKs and triggers kept, for a revision `after` of a
    record whose current revision is `before` (None for a new record). Raises
    `ConfigRecordError` naming the rule."""
    if before is not None:
        changed = immutable_change(cls, before, after)
        if changed:
            raise ConfigRecordError("config_immutable", f"{cls.KIND}: {', '.join(changed)}")
    if cls is StrategyVersion:
        if before is not None and before["status"] != "draft" and after["status"] == "draft":
            raise ConfigRecordError("version_back_to_draft")
        if after["status"] == VersionStatus.RELEASED and (
            after["backtest_run_id"] is None or after["shadow_started_at"] is None
        ):
            raise ConfigRecordError("release_evidence")
    elif cls is RiskPolicyVersion:
        if before is not None and before["superseded_at"] is not None:
            raise ConfigRecordError("config_immutable", "risk_policy_version: superseded")
        # HAT-SCH-084: raises `CoolingOffError`
        require_cooling_off(
            after["direction"],
            datetime.fromisoformat(after["requested_at"]),
            datetime.fromisoformat(after["effective_from"]),
        )
    elif cls is Bot and after["broker_account_id"] is None:
        raise ConfigRecordError("bot_account")


def kind_rules(kind: str, records: Iterable[Mapping[str, Any]]) -> None:
    """The rules across all of a kind's records in force (after a commit) that the database's
    unique indexes kept: version numbers unique per strategy and per user's risk policy, and one
    released version per strategy (HAT-PORT-020). The store checks them under the kind's lock,
    so two writers that each pass on their own snapshot cannot both commit."""
    records = list(records)
    if kind == StrategyVersion.KIND:
        _unique(kind, records, ("strategy_id", "version"))
        released = [r for r in records if r["status"] == VersionStatus.RELEASED]
        _unique(kind, released, ("strategy_id",), code="one_released_version")
    elif kind == RiskPolicyVersion.KIND:
        _unique(kind, records, ("user_id", "version"))


def _unique(
    kind: str,
    records: Iterable[Mapping[str, Any]],
    key: tuple[str, ...],
    code: str = "config_unique",
) -> None:
    seen: set[tuple[Any, ...]] = set()
    for record in records:
        value = tuple(record[name] for name in key)
        if value in seen:
            raise ConfigRecordError(code, f"{kind} {'/'.join(map(str, value))}")
        seen.add(value)

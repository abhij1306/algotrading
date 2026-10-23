"""CONF checks of the event store against docs/specs/hat/03-schema.md §6 (HAT-SCH-063)."""

import re

from hat_conf_spec import SPEC_DIR, parse_event_ddl, parse_event_types

from app.hat.events import ACTORS, AGGREGATE_TYPES, EVENT_GROUPS, EVENT_TYPES


def test_events__catalogue() -> None:
    """The event types `app/hat/events.py` knows equal the 48 of the spec table, group by
    group: nothing missing, nothing extra, no duplicate."""
    spec = parse_event_types()
    assert EVENT_GROUPS == spec
    flat = [t for group in spec.values() for t in group]
    assert len(flat) == len(set(flat)) == len(EVENT_TYPES) == 48
    assert EVENT_TYPES == frozenset(flat)


def test_events__vocabularies_match_the_ddl() -> None:
    """The actors and aggregate types in code are the ones the DDL names."""
    assert set(parse_event_ddl().actors) == set(ACTORS)
    text = (SPEC_DIR / "03-schema.md").read_text(encoding="utf-8")
    comment = re.search(r"aggregate_type text not null,\s*-- ([^\n]+)", text)
    assert comment
    assert {name.strip() for name in comment.group(1).split(",")} == set(AGGREGATE_TYPES)

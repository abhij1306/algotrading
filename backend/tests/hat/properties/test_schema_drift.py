"""The schema drift comparison (DB audit M5) on two small reflected shapes."""

import copy

from app.hat.schema import TableShape, pg_ddl_objects, schema_drift

MODEL: dict[str, TableShape] = {
    "hat_order": {
        "columns": {"id": ("uuid", True), "qty": ("bigint", True), "note": ("text", False)},
        "constraints": {
            "hat_order_pkey": "PRIMARY KEY (id)",
            "hat_order_qty_check": "CHECK ((qty > 0))",
        },
        "indexes": {"hat_order_pkey": "CREATE UNIQUE INDEX hat_order_pkey ON hat_order (id)"},
    }
}


def live() -> dict[str, TableShape]:
    return copy.deepcopy(MODEL)


def test_matching_shapes_have_no_drift() -> None:
    assert schema_drift(live(), MODEL) == []


def test_a_missing_nullable_column_prints_its_ddl() -> None:
    shape = live()
    del shape["hat_order"]["columns"]["note"]
    assert schema_drift(shape, MODEL) == ["ALTER TABLE hat_order ADD COLUMN note text;"]


def test_a_missing_not_null_column_is_added_in_one_transaction() -> None:
    # never `ADD COLUMN ... NOT NULL` (refused on a populated table) and never half of the steps
    shape = live()
    del shape["hat_order"]["columns"]["qty"]
    (block,) = schema_drift(shape, MODEL)
    lines = block.splitlines()
    assert lines[0].startswith("BEGIN;")
    assert lines[1] == "ALTER TABLE hat_order ADD COLUMN qty bigint;"
    assert lines[2].startswith("--")
    assert lines[3:] == ["ALTER TABLE hat_order ALTER COLUMN qty SET NOT NULL;", "COMMIT;"]


def test_type_and_nullability_changes_print_their_ddl() -> None:
    shape = live()
    shape["hat_order"]["columns"]["qty"] = ("integer", False)
    assert schema_drift(shape, MODEL) == [
        "ALTER TABLE hat_order ALTER COLUMN qty TYPE bigint;  -- is integer",
        "ALTER TABLE hat_order ALTER COLUMN qty SET NOT NULL;",
    ]


def test_missing_and_changed_checks_and_indexes_print_their_ddl() -> None:
    shape = live()
    del shape["hat_order"]["constraints"]["hat_order_qty_check"]
    shape["hat_order"]["indexes"]["hat_order_pkey"] = (
        "CREATE INDEX hat_order_pkey ON hat_order (id)"
    )
    assert schema_drift(shape, MODEL) == [
        "ALTER TABLE hat_order ADD CONSTRAINT hat_order_qty_check CHECK ((qty > 0));",
        "DROP INDEX hat_order_pkey;  -- is CREATE INDEX hat_order_pkey ON hat_order (id)",
        "CREATE UNIQUE INDEX hat_order_pkey ON hat_order (id);",
    ]


def test_extras_are_drift_unless_allowed_or_pg_ddl_owned() -> None:
    shape = live()
    shape["hat_order"]["columns"]["legacy"] = ("text", True)
    shape["hat_order"]["indexes"]["hat_order_signal"] = "CREATE INDEX hat_order_signal ..."
    shape["hat_order"]["constraints"]["hat_order_guard"] = "CHECK (true)"
    shape["hat_isin_merge"] = {"columns": {}, "constraints": {}, "indexes": {}}
    shape["hat_stray"] = {"columns": {}, "constraints": {}, "indexes": {}}
    drift = schema_drift(
        shape,
        MODEL,
        ddl_indexes={"hat_order_signal": "hat_order"},
        ddl_constraints={"hat_order_guard": "hat_order"},
    )
    assert drift == [
        "-- table hat_stray is in the database, not in the models",
        "-- column hat_order.legacy is in the database, not in the models",
    ]


def test_a_missing_pg_ddl_index_is_drift() -> None:
    assert schema_drift(live(), MODEL, ddl_indexes={"hat_order_signal": "hat_order"}) == [
        "-- index hat_order_signal on hat_order is missing (schema.PG_DDL creates it)"
    ]


def test_pg_ddl_objects_names_its_indexes_and_constraints() -> None:
    indexes, constraints = pg_ddl_objects()
    assert indexes["hat_event_bot_type"] == "hat_event"
    assert constraints["hat_symbol_history_no_overlap"] == "hat_instrument_symbol_history"

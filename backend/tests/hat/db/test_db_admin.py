"""The owner's admin command line against Postgres (HAT-OPS-091, PR 73; PR 35 follow-up 5).

The properties run the commands on SQLite; this runs the "First deploy" runbook through `main`
on the production database, inside a transaction that is rolled back, so the row locks, types
and constraints SQLite does not enforce are exercised too.
"""

import uuid
from pathlib import Path

import pytest
from hat.hat_admin_support import (
    CHAT,
    START,
    deploy_to_shadow,
    new_world,
    night,
    snapshot,
    trading_days_after,
)
from sqlalchemy import Connection, select

from app.hat.engine.portfolio import lifecycle
from app.hat.models import HatBot, HatBrokerAccount, HatEvent

pytestmark = pytest.mark.postgres


def test_admin__first_deploy_on_postgres(conn: Connection, tmp_path: Path) -> None:
    """The runbook takes a fresh database to a Live sleeve on a live version, DDPI confirmed
    then revoked, every owner's step recorded as the owner's; a refused step leaves every HAT
    table as it was."""
    world = new_world(tmp_path, conn)
    bot, version = deploy_to_shadow(world)
    before = snapshot(world, "hat_")
    code, _, err = world.run("promote", "--bot", bot, "--to", "live")
    assert (code, err) == (2, "refused: version_not_live\n")
    assert snapshot(world, "hat_") == before
    world.now = night(trading_days_after(START, lifecycle.SHADOW_DAYS))
    assert world.ok("promote-version", "--version", version)["status"] == "live"
    assert world.ok("promote", "--bot", bot, "--to", "live")["lifecycle_state"] == "live"
    with world.scope() as session:
        account = str(session.get_one(HatBot, uuid.UUID(bot)).broker_account_id)
    world.ok("confirm-ddpi", "--account", account, "--since", START.isoformat(), "--note", "on")
    world.ok("revoke-ddpi", "--account", account, "--note", "withdrawn")
    with world.scope() as session:
        row = session.get_one(HatBrokerAccount, uuid.UUID(account))
        assert (row.ddpi_confirmed, row.ddpi_confirmed_at) == (False, None)
        owner = session.scalars(select(HatEvent).where(HatEvent.actor == "user")).all()
        kinds = sorted(e.event_type for e in owner)
        assert kinds.count("sleeve.version_assigned") == 1
        assert {"broker.ddpi_confirmed", "broker.ddpi_revoked", "strategy.promoted"} <= set(kinds)
        assert kinds.count("sleeve.lifecycle_changed") == 3  # backtested, shadow, live
        assert all(e.payload["chat_id"] == CHAT and e.payload["via"] == "cli" for e in owner)

"""Properties of the owner's admin command line (HAT-OPS-091, PR 35, 73).

The commands run through `main` against an in-memory database (the rig is `hat_admin_support`);
no network is touched.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from hat.hat_admin_support import (
    CHAT,
    START,
    SYMBOLS,
    World,
    add_run,
    at,
    constituents_csv,
    deploy_to_shadow,
    new_world,
    night,
    snapshot,
    trading_days_after,
)
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, update

from app.hat.engine.portfolio import lifecycle
from app.hat.engine.strategy.versions import seed_text
from app.hat.execution import modes
from app.hat.models import (
    HatBot,
    HatBrokerAccount,
    HatEvent,
    HatMarket,
    HatStrategyVersion,
)


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(waited=st.integers(0, 25))
@example(waited=19)
@example(waited=20)
def test_first_deploy(tmp_path: Path, waited: int) -> None:
    """PBT admin.first_deploy (HAT-OPS-091, HAT-PORT-060, HAT-BOT-084): the first-deploy
    runbook, run through the command line, takes a fresh database to a sleeve in Shadow that
    scans (a version and a live universe), then to Live only after 20 trading sessions of shadow
    and only on a promoted version; the owner's steps are recorded as the owner's. Harm: a fresh
    deploy never scans, or a sleeve trades real money on an unproven version."""
    world = new_world(tmp_path)
    bot, version = deploy_to_shadow(world)
    with world.scope() as session:
        row = session.get(HatBot, uuid.UUID(bot))
        assert row is not None
        assert row.lifecycle_state == lifecycle.SHADOW
        assert row.execution_mode == "confirm"  # asked for; runs shadow until Live and the gates
        assert str(row.live_version_id) == version
        assert row.universe_version_id is not None
        account = str(row.broker_account_id)
    ddpi = world.ok(
        "confirm-ddpi", "--account", account, "--since", START.isoformat(), "--note", "DDPI on"
    )
    assert ddpi == {"broker_account_id": account, "ddpi_confirmed": True}
    code, _, err = world.run("promote", "--bot", bot, "--to", "live")
    assert code == 2
    assert err == "refused: version_not_live\n"
    day = trading_days_after(START, waited) if waited else START
    world.now = night(day)
    code, result, err = world.run("promote-version", "--version", version)
    if waited < lifecycle.SHADOW_DAYS:
        assert code == 2
        assert err == "refused: shadow_days\n"
        return
    assert code == 0, err
    assert result["status"] == "live"
    assert world.ok("promote", "--bot", bot, "--to", "live")["lifecycle_state"] == "live"
    config = world.tmp / "next.yaml"
    config.write_text(seed_text("stage2-contraction-breakout"), "utf-8")
    with world.scope() as session:
        strategy_id = session.get(HatStrategyVersion, uuid.UUID(version)).strategy_id  # type: ignore[union-attr]
    draft = world.ok("save-draft", "--strategy", str(strategy_id), "--config", config.name)
    assert (draft["version"], draft["status"]) == (2, "draft")
    with world.scope() as session:
        owner_events = session.scalars(
            select(HatEvent).where(
                HatEvent.event_type.in_(("strategy.promoted", "sleeve.lifecycle_changed"))
            )
        ).all()
        assert len(owner_events) == 4  # backtested, shadow, version promoted, live
        owner_events += session.scalars(
            select(HatEvent).where(HatEvent.event_type == "broker.ddpi_confirmed")
        ).all()
        assert len(owner_events) == 5
        for event in owner_events:
            assert event.actor == "user"
            assert event.payload["chat_id"] == CHAT
            assert event.payload["via"] == "cli"


REFUSALS = {
    "promote_live_from_shadow": "version_not_live",
    "promote_version_early": "shadow_days",
    "promote_version_in_hours": "market_hours",
    "shadow_version_twice": "not_backtested",
    "assign_draft": "version_not_released",
    "unknown_symbol": "unknown_symbols: ZZZ",
    "no_owner_chat": "no_owner_chat",
    "no_such_edge": "no_such_edge",
    "publish_other_run": "run_not_for_version",
    "promote_in_hours": "market_hours",
    "universe_unknown_bot": "bot_unknown",
    "universe_in_hours": "market_hours",
    "isin_mismatch": "unknown_symbols: BBB(INE123456A01)",
    "bots_on_unknown_account": "account_not_active",
    "bots_on_foreign_account": "account_not_active",
    "draft_in_hours": "market_hours",
    "draft_bad_utf8": "config_unreadable",
    "file_outside_admin": "file_outside_admin",
    "draft_wrong_suffix": "file_outside_admin",
    "ddpi_in_hours": "market_hours",
    "ddpi_in_future": "ddpi_date_in_future",
    "ddpi_blank_note": "ddpi_note_required",
    "ddpi_twice": "ddpi_already_confirmed",
    "ddpi_unknown_account": "account_not_active",
    "revoke_unconfirmed": "ddpi_not_confirmed",
    "revoke_blank_note": "ddpi_note_required",
    "revoke_unknown_account": "account_unknown",
    "sleeve_unknown_strategy": "unknown_strategy",
    "sleeve_unknown_account": "account_not_active",
    "sleeve_foreign_account": "account_not_active",
    "resolve_unknown_position": "position_unknown",
    "resolve_in_hours": "market_hours",
    "assign_no_owner_chat": "no_owner_chat",
}


def _foreign_account(world: World) -> str:
    """An active account of another user: no sleeve of the owner's may be bound to it."""
    with world.scope() as session:
        account = HatBrokerAccount(
            user_id=uuid.uuid4(),
            broker="fyers",
            account_ref="XF0073",
            capabilities={},
            status="active",
        )
        session.add(account)
        session.flush()
        return str(account.id)


@settings(  # every case: Hypothesis does not repeat a case, so this enumerates them
    max_examples=len(REFUSALS),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=st.sampled_from(sorted(REFUSALS)))
def test_refusal_writes_nothing(tmp_path: Path, case: str) -> None:
    """PBT admin.refusal_writes_nothing (HAT-OPS-091): a command the services refuse exits 2
    with the fixed code of the rule that refused it on stderr and leaves every table as it was.
    Harm: a half-applied release step (a version promoted but the sleeves not moved, a universe
    published but not given to the sleeves)."""
    world = new_world(tmp_path)
    bot, version = deploy_to_shadow(world)
    with world.scope() as session:
        strategy_id = session.get(HatStrategyVersion, uuid.UUID(version)).strategy_id  # type: ignore[union-attr]
        draft = session.scalar(
            select(HatStrategyVersion.id).where(
                HatStrategyVersion.strategy_id != strategy_id,
                HatStrategyVersion.status == "draft",
            )
        )
        other_bot = session.scalar(select(HatBot.id).where(HatBot.strategy_id != strategy_id))
    later = trading_days_after(START, lifecycle.SHADOW_DAYS)
    argv: tuple[str, ...]
    if case == "promote_live_from_shadow":
        argv = ("promote", "--bot", bot, "--to", "live")
    elif case == "promote_version_early":
        argv = ("promote-version", "--version", version)
    elif case == "promote_version_in_hours":
        world.now = at(later, 11)
        argv = ("promote-version", "--version", version)
    elif case == "shadow_version_twice":
        argv = ("shadow-version", "--version", version)
    elif case == "assign_draft":
        argv = ("assign-version", "--bot", str(other_bot), "--version", str(draft))
    elif case == "unknown_symbol":
        csv = constituents_csv(world.tmp, (*SYMBOLS[:2], "ZZZ"))
        argv = ("publish-universe", "--constituents", csv.name, "--as-of", START.isoformat())
    elif case == "no_owner_chat":
        world.env = {}
        world.now = night(later)
        argv = ("promote-version", "--version", version)
    elif case == "no_such_edge":
        argv = ("promote", "--bot", bot, "--to", "retired")
    elif case == "promote_in_hours":
        world.now = at(START + timedelta(days=1), 11)
        argv = ("promote", "--bot", bot, "--to", "probation")
    elif case == "universe_in_hours":
        world.now = at(START + timedelta(days=1), 11)
        csv = constituents_csv(world.tmp, SYMBOLS)
        argv = ("publish-universe", "--constituents", csv.name, "--as-of", START.isoformat())
    elif case == "isin_mismatch":
        csv = constituents_csv(world.tmp, SYMBOLS, {"BBB": "INE123456A01"})
        argv = ("publish-universe", "--constituents", csv.name, "--as-of", START.isoformat())
    elif case == "bots_on_unknown_account":
        argv = ("seed-bots", "--account", str(uuid.uuid4()))
    elif case == "bots_on_foreign_account":
        argv = ("seed-bots", "--account", _foreign_account(world))
    elif case in ("draft_in_hours", "draft_bad_utf8"):
        config = world.tmp / f"draft-{uuid.uuid4().hex}.yaml"
        config.write_text(seed_text("stage2-contraction-breakout"), "utf-8")
        if case == "draft_in_hours":
            world.now = at(START + timedelta(days=1), 11)
        else:
            config.write_bytes(b"bot: \xff\xfe\n")
        argv = ("save-draft", "--strategy", str(strategy_id), "--config", config.name)
    elif case == "file_outside_admin":
        outside = constituents_csv(world.tmp.parent, SYMBOLS)
        argv = ("publish-universe", "--constituents", f"../{outside.name}", "--as-of", "2026-11-02")
    elif case == "draft_wrong_suffix":
        argv = ("save-draft", "--strategy", str(strategy_id), "--config", "../../etc/passwd")
    elif case.startswith("ddpi_"):
        with world.scope() as session:
            account = str(session.get_one(HatBot, uuid.UUID(bot)).broker_account_id)
        since, note = START.isoformat(), "DDPI on"
        if case == "ddpi_in_hours":
            world.now = at(START + timedelta(days=1), 11)
        elif case == "ddpi_in_future":
            since = (START + timedelta(days=3)).isoformat()
        elif case == "ddpi_blank_note":
            note = "   "
        elif case == "ddpi_twice":
            world.ok("confirm-ddpi", "--account", account, "--since", since, "--note", note)
        else:
            account = str(uuid.uuid4())
        argv = ("confirm-ddpi", "--account", account, "--since", since, "--note", note)
    elif case.startswith("revoke_"):
        with world.scope() as session:
            account = str(session.get_one(HatBot, uuid.UUID(bot)).broker_account_id)
        note = "   " if case == "revoke_blank_note" else "DDPI withdrawn"
        if case == "revoke_unknown_account":
            account = str(uuid.uuid4())
        argv = ("revoke-ddpi", "--account", account, "--note", note)
    elif case.startswith("sleeve_"):
        with world.scope() as session:
            account = str(session.get_one(HatBot, uuid.UUID(bot)).broker_account_id)
        if case == "sleeve_unknown_strategy":
            argv = ("add-sleeve", "--strategy", str(uuid.uuid4()), "--account", account)
        elif case == "sleeve_foreign_account":
            argv = (
                "add-sleeve",
                "--strategy",
                str(strategy_id),
                "--account",
                _foreign_account(world),
            )
        else:
            argv = ("add-sleeve", "--strategy", str(strategy_id), "--account", str(uuid.uuid4()))
    elif case.startswith("resolve_"):
        if case == "resolve_in_hours":
            world.now = at(START + timedelta(days=1), 11)
        argv = ("resolve-action", "--position", str(uuid.uuid4()), "--action", "2026-11-03:other")
        argv = (*argv, "--close-at", "101.5")
    elif case == "assign_no_owner_chat":
        world.env = {}
        argv = ("assign-version", "--bot", bot, "--version", version)
    elif case == "universe_unknown_bot":
        csv = constituents_csv(world.tmp, SYMBOLS)
        argv = ("publish-universe", "--constituents", csv.name, "--as-of", START.isoformat())
        argv = (*argv, "--bot", bot, "--bot", str(uuid.uuid4()))
    else:
        run = add_run(world, version)
        argv = ("publish-version", "--version", str(draft), "--run", run)
    before = snapshot(world)
    code, _, err = world.run(*argv)
    assert code == 2
    assert err == f"refused: {REFUSALS[case]}\n"
    assert snapshot(world) == before
    with world.scope() as session:
        assert session.scalar(select(func.count()).select_from(HatEvent)) > 0


@settings(
    max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(in_hours=st.booleans(), later=st.integers(0, 5), no_calendar=st.booleans())
def test_ddpi_revoked(tmp_path: Path, in_hours: bool, later: int, no_calendar: bool) -> None:
    """PBT admin.ddpi_revoked (HAT-GOV-085, PR 73; PR 45 follow-up 2): `revoke-ddpi` withdraws
    a DDPI confirmation at any time, inside market hours too and with no trading calendar
    loaded (it only narrows), recorded as the owner's under the account's user; the
    account's DDPI gate is off from that instant on, so its bots run shadow, and a second
    revocation is refused. Harm: DDPI withdrawn at the broker while HAT still places real
    sales that need a TPIN, and a stop that cannot sell."""
    world = new_world(tmp_path)
    bot, _ = deploy_to_shadow(world)
    with world.scope() as session:
        account = str(session.get_one(HatBot, uuid.UUID(bot)).broker_account_id)
    world.ok("confirm-ddpi", "--account", account, "--since", START.isoformat(), "--note", "on")
    day = trading_days_after(START, 1 + later)
    world.now = at(day, 11) if in_hours else night(day)
    if no_calendar:  # the market's calendar can't be read (`no_calendar` for the other acts)
        with world.scope() as session:
            session.execute(update(HatMarket).values(code="XNSE"))
    result = world.ok("revoke-ddpi", "--account", account, "--note", "withdrawn at the DP")
    assert result == {"broker_account_id": account, "ddpi_confirmed": False}
    with world.scope() as session:
        row = session.get_one(HatBrokerAccount, uuid.UUID(account))
        assert row.ddpi_note == f"revoked {day.isoformat()}: withdrawn at the DP"
        for instant in (world.now, world.now + timedelta(days=later + 1)):
            assert modes.account_gates(session, row, instant).ddpi_confirmed is False
        events = session.scalars(
            select(HatEvent).where(HatEvent.event_type == "broker.ddpi_revoked")
        ).all()
        assert [(e.actor, e.payload["chat_id"], e.payload["via"]) for e in events] == [
            ("user", CHAT, "cli")
        ]
        assert [e.user_id for e in events] == [row.user_id]
        confirmed = session.scalars(
            select(HatEvent.user_id).where(HatEvent.event_type == "broker.ddpi_confirmed")
        ).all()
        assert confirmed == [row.user_id]
    code, _, err = world.run("revoke-ddpi", "--account", account, "--note", "again")
    assert (code, err) == (2, "refused: ddpi_not_confirmed\n")


@settings(
    max_examples=6, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(other_account=st.booleans())
def test_shadow_beside_live(tmp_path: Path, other_account: bool) -> None:
    """PBT admin.shadow_beside_live (HAT-PORT-060, HAT-BOT-084, PR 73; PR 35 follow-ups 2, 3):
    with a strategy's sleeve Live, `add-sleeve` gives the strategy a second sleeve (on the same
    account or another) that takes a new version through Research, Backtested and Shadow while
    the Live one keeps trading the live version; every assignment and the promotion's move of
    the Live sleeve are recorded as the owner's `sleeve.version_assigned`; and the second sleeve
    never goes Live while the first holds the strategy, even once its version is live. Harm: a
    new version has no way to shadow beside the live one, or two sleeves of one strategy trade
    real money and take its base weight twice."""
    world = new_world(tmp_path)
    bot, v1 = deploy_to_shadow(world)
    world.now = night(trading_days_after(START, lifecycle.SHADOW_DAYS))
    world.ok("promote-version", "--version", v1)
    world.ok("promote", "--bot", bot, "--to", "live")
    with world.scope() as session:
        live = session.get_one(HatBot, uuid.UUID(bot))
        strategy_id, account = str(live.strategy_id), str(live.broker_account_id)
    if other_account:
        account = world.ok("onboard-account", "--account-ref", "XA00001")["broker_account_id"]
    sleeve = world.ok("add-sleeve", "--strategy", strategy_id, "--account", account)
    assert sleeve["lifecycle_state"] == lifecycle.RESEARCH
    second = sleeve["bot_id"]
    config = world.tmp / "v2.yaml"
    config.write_text(seed_text("stage2-contraction-breakout"), "utf-8")
    v2 = world.ok("save-draft", "--strategy", strategy_id, "--config", config.name)["version_id"]
    world.ok("publish-version", "--version", v2, "--run", add_run(world, v2))
    world.ok("assign-version", "--bot", second, "--version", v2)
    world.ok("promote", "--bot", second, "--to", "backtested")
    world.ok("promote", "--bot", second, "--to", "shadow")
    world.ok("shadow-version", "--version", v2)
    with world.scope() as session:
        assert str(session.get_one(HatBot, uuid.UUID(bot)).live_version_id) == v1
        assert session.get_one(HatBot, uuid.UUID(bot)).lifecycle_state == lifecycle.LIVE
    code, _, err = world.run("promote", "--bot", second, "--to", "live")
    assert (code, err) == (2, "refused: version_not_live\n")
    started = trading_days_after(START, lifecycle.SHADOW_DAYS)
    world.now = night(trading_days_after(started, lifecycle.SHADOW_DAYS))
    world.ok("promote-version", "--version", v2)
    code, _, err = world.run("promote", "--bot", second, "--to", "live")
    assert (code, err) == (2, "refused: strategy_has_live_sleeve\n")
    with world.scope() as session:
        assert str(session.get_one(HatBot, uuid.UUID(bot)).live_version_id) == v2
        assert session.get_one(HatBot, uuid.UUID(second)).lifecycle_state == lifecycle.SHADOW
        assigned = session.scalars(
            select(HatEvent)
            .where(HatEvent.event_type == "sleeve.version_assigned")
            .order_by(HatEvent.occurred_at, HatEvent.id)
        ).all()
        moves = [
            (str(e.aggregate_id), e.payload["version_id"], e.payload["replaced"]) for e in assigned
        ]
        assert moves == [(bot, v1, None), (second, v2, None), (bot, v2, 1)]
        for event in assigned:
            assert (event.actor, event.payload["chat_id"], event.payload["via"]) == (
                "user",
                CHAT,
                "cli",
            )

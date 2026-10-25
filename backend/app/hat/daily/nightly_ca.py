"""The body of `ca.adjust`: the night's corporate actions, dividends, demerger notices and
lineage, each failing closed with an alert, then the check that the stored factors load into
the adjusted view."""

from __future__ import annotations

import logging
from datetime import date, datetime

from ..data import ca_bc, ca_nightly
from ..data.ca_gate import CaFileError
from ..data.ca_versions import projection_mismatches, read_versions, stored_rows
from ..data.calendar import session_calendar
from ..data.chains import stored_adjustment
from ..data.corporate_actions import lookup_actions, store_actions
from ..data.demerger_notice import announce_stored, store_notices, unlisted_notices
from ..data.lineage import refresh_lineage
from ..data.quarantine import open_quarantines
from ..data.store import cumulative_segments
from ..engine.demerger_exit import flag_positions, flag_unlisted
from ..engine.positions import restate_dividends
from ..engine.rescale import ALERT_ACTION_UNCHECKED, flag_isin_changes, held_and_watchlisted
from ..ops.jobs import JobResult
from ..time import utc_now
from .nightly_deps import NightlyDeps, mcp_client, next_session

logger = logging.getLogger(__name__)

# The ISIN lineage refresh failed (the stored one stands), or found a succession it would not
# link: the owner reads `python -m app.hat.data.lineage report`.
ALERT_LINEAGE_UNCHECKED = "lineage.unchecked"
ALERT_LINEAGE_REVIEW = "lineage.review"
# NSE's daily corporate-action file could not be read or stored: the night's dividends are not
# in the total-return series until `ca_dividends --bc` imports that day's file (runbook).
ALERT_DIVIDENDS_UNCHECKED = "dividend.unchecked"
ALERT_DEMERGERS_UNCHECKED = "demerger.unchecked"  # tonight's demerger notices failed (CA8)
# the closed trades' dividends could not be restated: their P&L and R lag the stored dividends
ALERT_TRADE_DIVIDENDS_STALE = "dividend.trades_stale"
# `hat_corporate_action` is not the hindsight selection of the version ledger (a writer broke the
# pairing) or the check failed: `python -m app.hat.data.point_in_time report` lists the keys
ALERT_PROJECTION = "corporate_action.projection"
PROJECTION_KEYS = 3  # mismatched keys named in the alert; the report lists them all


class CaAdjust:
    """`ca.adjust` over one `NightlyDeps`, for the trade date the run is kept under."""

    def __init__(self, deps: NightlyDeps) -> None:
        self.deps = deps

    def _now(self) -> datetime:
        return utc_now(self.deps.clock)

    def run(self, day: date) -> JobResult:
        """Corporate actions adjust prices at read time (`bars_daily` over the stored factors).
        First the next session's actions of the held and watchlisted instruments are looked up
        and stored (HAT-EXEC-140), so the features, the scan and the morning's `ca.rescale` see
        them; a failed lookup is alerted and never fails the step. Then the actions of every
        quarantined instrument on its `since` date are asked again (HAT-DATA-062, PR 33): a
        split whose lookup failed on the day releases its quarantine at the next gate once
        stored. A failure there is only logged: the quarantine was alerted already and stands.
        After the lineage refresh each position held across an ISIN change is flagged
        (`_isin_changes`, Q4).
        Then the day's corporate-action file is kept (`_archive_bc`), its dividends go in with
        their amounts (`_dividends`, CA4), its demergers become notices and the positions held
        into them are alerted (`_demergers`, CA8), and every closed trade's dividends are
        brought up to the stored ones (`restate_dividends`, R-DIV: a trade closed on tonight's
        ex-date was written without it). The stored actions are checked against the version
        ledger (`_projection`, CA6). Then the factors are checked to load into the adjusted view
        (`cumulative_segments`, no connection) before the features read it."""
        try:
            self._next_session_actions(day)
        except Exception:
            # Fail closed without stopping the chain: tomorrow's features and scan still run, and
            # the alert says the next session's actions are unknown.
            logger.exception("next-session corporate-action lookup failed")
            self.deps.notifier.alert(ALERT_ACTION_UNCHECKED, day, {"symbol": "*"})
        text: str | None = None
        try:
            text = self._corporate_action_file(day)
            if text is not None:
                self._archive_bc(day, text)
                self._dividends(day, text)
        except Exception:
            # The series stays without tonight's dividends (a drop it does not pay back) until
            # the day's file is imported by hand; the chain goes on.
            logger.exception("nightly dividend import failed")
            self.deps.notifier.alert(ALERT_DIVIDENDS_UNCHECKED, day, {})
        try:
            self._demergers(day, text)
        except Exception:
            # The positions keep today's handling (no forced exit from tonight's file); alerted.
            logger.exception("nightly demerger notices failed")
            self.deps.notifier.alert(ALERT_DEMERGERS_UNCHECKED, day, {})
        try:
            with self.deps.session_factory() as session:
                restated = restate_dividends(session, self._now())
                session.commit()
            logger.info("trade dividends restated: %d trades changed", restated)
        except Exception:
            # The trades keep their figures until a later night restates them; alerted, so a
            # repeated failure does not leave their P&L and R quietly stale.
            logger.exception("trade dividend restatement failed")
            self.deps.notifier.alert(ALERT_TRADE_DIVIDENDS_STALE, day, {})
        try:
            self._quarantine_actions()
        except Exception:
            logger.exception("quarantine corporate-action lookup failed")
        self._refresh_lineage(day)
        self._isin_changes(day)
        self._projection(day)
        with self.deps.session_factory() as session:
            adjustment = stored_adjustment(session)
        cumulative_segments(adjustment.factors)  # raises when a factor cannot load into the view
        logger.info(
            "adjusted view loads with %d factors and %d linked ISINs",
            len(adjustment.factors),
            len(adjustment.lineage),
        )
        return JobResult(rows=len(adjustment.factors))

    def _projection(self, day: date) -> None:
        """Every write of a corporate action appends its version in the same transaction, so
        `hat_corporate_action` is the hindsight selection of the ledger
        (`ca_versions.projection_mismatches`). Tonight's writes are in: a mismatch, or a check
        that fails, is alerted, never fatal (the adjusted view reads the stored rows either
        way)."""
        try:
            with self.deps.session_factory() as session:
                found = projection_mismatches(stored_rows(session), read_versions(session))
                session.rollback()
        except Exception:
            logger.exception("corporate-action projection check failed")
            self.deps.notifier.alert(ALERT_PROJECTION, day, {"mismatches": "unchecked"})
            return
        logger.info("corporate-action projection: %d mismatches", len(found))
        if found:
            keys = ", ".join(f"{k[0]}:{k[1]}:{k[2]}:{why}" for k, why in found[:PROJECTION_KEYS])
            self.deps.notifier.alert(
                ALERT_PROJECTION, day, {"mismatches": len(found), "keys": keys}
            )

    def _refresh_lineage(self, day: date) -> None:
        """The ISIN lineage from tonight's bars and NSE's symbol-change list (PR 74), before the
        features read the adjusted view. A failure keeps the stored lineage and is alerted; a
        succession newly on the review list is alerted once (`python -m app.hat.data.lineage
        report` lists them)."""
        sources = self.deps.sources
        if sources.symbol_changes is None:
            return
        try:
            report = refresh_lineage(
                self.deps.session_factory,
                self.deps.store,
                sources.symbol_changes,
                sources.name_changes,
            )
        except Exception:
            logger.exception("isin lineage refresh failed; the stored lineage stands")
            self.deps.notifier.alert(ALERT_LINEAGE_UNCHECKED, day, {})
            return
        logger.info("isin lineage: %d linked, %d for review", report.linked, report.review)
        for pair in report.new_review:
            self.deps.notifier.alert(ALERT_LINEAGE_REVIEW, day, {"isins": pair})

    def _isin_changes(self, day: date) -> None:
        """Each open position held across an ISIN change of the lineage gets one URGENT
        `position.isin_changed` (Q4); it is manage-only and its chain takes no entry
        (`rescale.open_guards`) whether or not this runs. A failure is alerted."""
        try:
            with self.deps.session_factory() as session:
                flagged = flag_isin_changes(session, self._now())
                session.commit()
        except Exception:
            logger.exception("isin change flags failed")
            self.deps.notifier.alert(ALERT_LINEAGE_UNCHECKED, day, {})
            return
        logger.info("positions held across an isin change: %d newly flagged", flagged)

    def _next_session_actions(self, day: date) -> None:
        with self.deps.session_factory() as session:
            target = next_session(session, day)
            instruments = held_and_watchlisted(session, day)
        if target is None or not instruments:
            return
        found = lookup_actions(mcp_client(self.deps), instruments, target, target)
        stored = store_actions(self.deps.session_factory, found.records, (self.deps.notifier, day))
        logger.info(
            "next-session actions: %d stored, %d lookups failed, %d conflicts",
            stored.created,
            len(found.failed),
            len(stored.conflicts),
        )
        for symbol in found.failed:
            self.deps.notifier.alert(ALERT_ACTION_UNCHECKED, day, {"symbol": symbol})

    def _corporate_action_file(self, day: date) -> str | None:
        """The night's `bc` file of NSE's PR archive; None when no source is configured."""
        source = self.deps.sources.corporate_actions
        return None if source is None else source(day)()

    def _archive_bc(self, day: date, text: str) -> None:
        """Keep tonight's `bc` file under the data root (`DataStore.store_bc`, owner 2026-10-25),
        so a later check can see which nights NSE listed a demerger. A failure is only logged:
        the night's imports read the text either way."""
        try:
            kept = self.deps.store.store_bc(day, text)
        except Exception:
            logger.exception("tonight's bc file was not archived")
            return
        logger.info("bc file of %s kept at %s", day, kept.name)

    def _demergers(self, day: date, text: str | None) -> None:
        """The demergers the night's `bc` file lists become notices dated tonight, a stored notice
        ahead of tonight the file no longer lists is recorded for the owner (`flag_unlisted`,
        Q6: never retired), a stored demerger takes its notice's date, and each open position
        held into one is alerted (`demerger_exit.flag_positions`, CA8), in one transaction.
        Without tonight's file the stored notices are still checked, so a position whose exit
        passed is alerted late, and so it is when tonight's file cannot be read (alerted)."""
        rows = None
        if text is not None:
            try:
                rows = ca_bc.read_bc(text)
            except CaFileError:
                logger.exception("tonight's bc file is unreadable; the stored notices are checked")
                self.deps.notifier.alert(ALERT_DEMERGERS_UNCHECKED, day, {})
        with self.deps.session_factory() as session:
            stored = store_notices(session, rows, day) if rows is not None else 0
            unlisted = (
                flag_unlisted(session, unlisted_notices(session, rows, day), self._now())
                if rows is not None
                else 0
            )
            dated = announce_stored(session)
            calendar = session_calendar(session)
            sessions = sorted(calendar.trading_days()) if calendar is not None else []
            due, late = flag_positions(session, day, sessions, self._now())
            session.commit()
        logger.info(
            "demergers of %s: %d notices stored, %d newly unlisted, %d actions dated, "
            "%d exits due, %d late",
            day,
            stored,
            unlisted,
            dated,
            due,
            late,
        )

    def _dividends(self, day: date, text: str) -> None:
        """The dividends going ex on `day` (and the catch-up days) from NSE's corporate-action
        file of `day`, stored with their cash in one transaction (`ca_nightly.nightly_import`,
        the CA3 checks). Each review or conflict case is alerted, and so is each stored dividend
        of those days still without an amount: it is never guessed."""
        with self.deps.session_factory() as session:
            report = ca_nightly.nightly_import(session, self.deps.store, text, day, self._now())
            session.commit()
        logger.info(
            "dividends of %s: %d stored, %d amounts added, %d for review, %d without an amount",
            day,
            report.created,
            report.added,
            len(report.flagged),
            len(report.missing),
        )
        for o in report.flagged:
            p = o.proposal
            self.deps.notifier.alert(
                ca_nightly.ALERT_DIVIDEND_REVIEW,
                day,
                {
                    "symbol": p.symbol,
                    "ex_date": p.ex_date.isoformat() if p.ex_date else "-",
                    "status": o.status,
                    "reason": o.reason,
                },
            )
        for m in report.missing:
            self.deps.notifier.alert(
                ca_nightly.ALERT_DIVIDEND_NO_AMOUNT,
                day,
                {"symbol": m.symbol, "isin": m.isin, "ex_date": m.ex_date.isoformat()},
            )

    def _quarantine_actions(self) -> None:
        with self.deps.session_factory() as session:
            rows = open_quarantines(session)
        if not rows:
            return
        client = mcp_client(self.deps)
        for since in sorted({r.since for r in rows}):
            instruments = {r.symbol: r.instrument_id for r in rows if r.since == since}
            found = lookup_actions(client, instruments, since, since)
            stored = store_actions(
                self.deps.session_factory, found.records, (self.deps.notifier, since)
            )
            logger.info(
                "quarantine actions of %s: %d stored, %d lookups failed, %d conflicts",
                since,
                stored.created,
                len(found.failed),
                len(stored.conflicts),
            )

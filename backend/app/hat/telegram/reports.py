"""Scheduled messages (08 §2; HAT-TG-021, HAT-TG-022, HAT-TG-024, HAT-EXEC-130, -131; the model
is `verification/tla/JobScheduler.tla`, LoginPromptOrder and ReportsAfterClose).

| Job | When (IST) | Sends |
| --- | --- | --- |
| `telegram.login_prompt` | 08:30 | one login link per active broker account |
| `telegram.login_reprompt` | 09:05 | links only for the accounts with no live session today |
| `telegram.rejections` | 15:35 | the day's `signal.rejected` signals with their reasons |
| `telegram.daily_report` | 15:40 | per bot P&L; last night's GTT check; close gaps |
| `telegram.veto_digest` | 15:45 | the week's veto score and verdict (last trading day) |

Each job runs once per trade date (`run_job`) and only on a trading day of
`hat_trading_calendar`: on any other day it sends nothing. The reports start after the 15:30
close. The watchlist digest is sent per bot after the nightly scan (`send_watchlist_digest`,
called by the daily cycle). A send that fails fails the job, so the scheduler retries it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..engine import approvals
from ..models import HatBot, HatEvent, HatJobRun, HatPosition
from ..ops.jobs import JobError, JobResult
from ..ops.scheduler import ScheduledJob
from ..risk.sessions import SessionCalendar
from ..time import IST, as_utc, day_bounds, trade_date_of, utc_now
from . import reads, render, store
from .client import BotClient, TelegramError

__all__ = [
    "DAILY_REPORT_AT",
    "LOGIN_PROMPT_AT",
    "LOGIN_REPROMPT_AT",
    "REJECTIONS_AT",
    "VETO_DIGEST_AT",
    "Reports",
    "is_week_end",
    "previous_trading_day",
    "telegram_jobs",
]

LOGIN_PROMPT_AT = time(8, 30)
LOGIN_REPROMPT_AT = time(9, 5)
REJECTIONS_AT = time(15, 35)
DAILY_REPORT_AT = time(15, 40)
VETO_DIGEST_AT = time(15, 45)
SESSION_CLOSE = time(15, 30)
GTT_VERIFY_JOB = "gtt.verify"
SEND_FAILED = "telegram_send_failed"


def previous_trading_day(calendar: SessionCalendar | None, day: date) -> date | None:
    if calendar is None:
        return None
    earlier = [d for d, s in calendar.days.items() if d < day and s.is_trading]
    return max(earlier) if earlier else None


def is_week_end(calendar: SessionCalendar | None, day: date) -> bool:
    """`day` is a trading day and no later weekday of its week can be one: each is a known
    non-trading day of the calendar. A weekday the calendar does not have yet counts as a
    trading day, so a calendar that runs out does not make every day a week end."""
    if not reads.is_trading_day(calendar, day) or calendar is None:
        return False
    for offset in range(1, 5 - day.weekday()):
        row = calendar.days.get(day + timedelta(days=offset))
        if row is None or row.is_trading:
            return False
    return True


def _start_of(day: date) -> datetime:
    return day_bounds(day)[0]


class Reports:
    def __init__(
        self,
        client: BotClient,
        session_factory: sessionmaker[Session] | Callable[[], Session],
        *,
        links: reads.LoginLinks | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client
        self._factory = session_factory
        self._links = links
        self._clock = clock

    def _send(self, session: Session, kind: str, ref_id: uuid.UUID | None, text: str) -> None:
        try:
            message_id = self._client.send(text)
        except TelegramError as exc:
            raise JobError(SEND_FAILED) from exc
        store.record_message(
            session,
            chat_id=self._client.chat_id,
            kind=kind,
            ref_id=ref_id,
            message_id=message_id,
            sent_at=self._clock(),
        )
        session.commit()

    # --- login prompts (HAT-TG-021) -----------------------------------------------------------

    def login_prompt(self, *, repeat: bool) -> JobResult:
        """08:30: a link for every active account; 09:05 (`repeat`): only for the accounts with
        no live `hat_broker_session` today. Nothing on a non-trading day or when every account
        is logged in."""
        now = self._clock()
        day = trade_date_of(now)
        with self._factory() as session:
            if not reads.is_trading_day(reads.session_calendar(session), day):
                return JobResult(rows=0)
            accounts = reads.logged_out(session, day) if repeat else reads.accounts(session)
            if not accounts:
                return JobResult(rows=0)
            links = [
                (reads.account_label(a), self._links.link(a) if self._links is not None else None)
                for a in accounts
            ]
            kind = "login_reprompt" if repeat else "login_prompt"
            self._send(session, kind, None, render.login_text(links, repeat=repeat))
        return JobResult(calls=1, rows=len(accounts))

    # --- after the close (HAT-TG-024) ----------------------------------------------------------

    def rejection_digest(self) -> JobResult:
        day = trade_date_of(self._clock())
        with self._factory() as session:
            if not reads.is_trading_day(reads.session_calendar(session), day):
                return JobResult(rows=0)
            rows = reads.rejection_rows(session, day)
            self._send(session, "rejections", None, render.rejection_digest_text(day, rows))
        return JobResult(calls=1, rows=len(rows))

    def _gtt_night(self, session: Session, night: date | None, day: date) -> render.GttNight:
        if night is None:
            return render.GttNight()
        runs = session.scalars(
            select(HatJobRun).where(
                HatJobRun.job.like(f"{GTT_VERIFY_JOB}:%"), HatJobRun.trade_date == night
            )
        ).all()
        refreshed = session.scalar(
            select(func.count())
            .select_from(HatEvent)
            .where(
                HatEvent.event_type == "gtt.refreshed",
                HatEvent.occurred_at >= as_utc(datetime.combine(night, SESSION_CLOSE, tzinfo=IST)),
                HatEvent.occurred_at < _start_of(day),
            )
        )
        return render.GttNight(
            runs=len(runs),
            failed=sum(1 for r in runs if r.status == "failed"),
            verified=sum(r.rows or 0 for r in runs if r.status == "succeeded"),
            refreshed=int(refreshed or 0),
        )

    def _close_gaps(
        self, session: Session, night: date | None, day: date
    ) -> list[render.CloseGapRow]:
        if night is None:
            return []
        rows = []
        for event in reads.events_between(
            session, ("exit.close_gap",), _start_of(night), _start_of(day + timedelta(days=1))
        ):
            payload = event.payload or {}
            if payload.get("trade_date") != night.isoformat():
                continue
            position = session.get(HatPosition, event.aggregate_id)
            rows.append(
                render.CloseGapRow(
                    symbol=reads.symbol_of(session, position.instrument_id) if position else "?",
                    live_price=str(payload.get("live_price")),
                    official_close=str(payload.get("official_close")),
                    gap_pct=str(payload.get("gap_pct")),
                )
            )
        return rows

    def daily_report(self) -> JobResult:
        """15:40: per bot open risk, realised R and equity vs peak from its latest
        `hat_daily_bot_snapshot`; the last night's `gtt.verify` runs; the close gaps (live
        price at 15:25 against the official close) recorded for the previous trading day."""
        day = trade_date_of(self._clock())
        with self._factory() as session:
            calendar = reads.session_calendar(session)
            if not reads.is_trading_day(calendar, day):
                return JobResult(rows=0)
            night = previous_trading_day(calendar, day)
            bots = reads.daily_bots(session, day)
            text = render.daily_report_text(
                day,
                bots,
                self._gtt_night(session, night, day),
                self._close_gaps(session, night, day),
            )
            self._send(session, "daily_report", None, text)
        return JobResult(calls=1, rows=len(bots))

    def veto_digest(self) -> JobResult:
        """On the week's last trading day: avoided and missed R of the week's closed veto
        trades (HAT-EXEC-130) and, after 50 decisions, the verdict (HAT-EXEC-131)."""
        day = trade_date_of(self._clock())
        with self._factory() as session:
            if not is_week_end(reads.session_calendar(session), day):
                return JobResult(rows=0)
            monday = day - timedelta(days=day.weekday())
            score = approvals.veto_score(
                approvals.skipped_results(
                    session, _start_of(monday), _start_of(day + timedelta(days=1))
                )
            )
            verdict = approvals.veto_verdict(*approvals.veto_history(session))
            view = (
                None
                if verdict is None
                else render.VetoVerdictView(
                    verdict.decisions, verdict.skipped_mean, verdict.taken_mean, verdict.verdict
                )
            )
            text = render.veto_digest_text(
                monday, day, score.avoided_r, score.missed_r, score.trades, view
            )
            self._send(session, "veto_digest", None, text)
        return JobResult(calls=1, rows=score.trades)

    # --- after the nightly scan (HAT-TG-022) ---------------------------------------------------

    def send_watchlist_digest(self, bot_id: uuid.UUID, day: date) -> None:
        """The digest of one bot's watchlist for `day` against its previous one."""
        with self._factory() as session:
            bot = session.get(HatBot, bot_id)
            if bot is None:
                return
            digest = reads.watchlist_digest(session, bot, day)
            self._send(session, "watchlist", bot_id, render.watchlist_digest_text(digest))


def telegram_jobs(reports: Reports) -> tuple[ScheduledJob, ...]:
    """The scheduled Telegram jobs (registered with the daily cycle by PR 17)."""
    return (
        ScheduledJob(
            "telegram.login_prompt",
            LOGIN_PROMPT_AT,
            lambda: reports.login_prompt(repeat=False),
            until=LOGIN_REPROMPT_AT,
        ),
        ScheduledJob(
            "telegram.login_reprompt",
            LOGIN_REPROMPT_AT,
            lambda: reports.login_prompt(repeat=True),
            until=SESSION_CLOSE,
        ),
        ScheduledJob("telegram.rejections", REJECTIONS_AT, reports.rejection_digest),
        ScheduledJob("telegram.daily_report", DAILY_REPORT_AT, reports.daily_report),
        ScheduledJob("telegram.veto_digest", VETO_DIGEST_AT, reports.veto_digest),
    )

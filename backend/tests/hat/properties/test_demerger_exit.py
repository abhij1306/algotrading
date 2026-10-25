"""The forced exit before a demerger (CA8): the exit session, the `bc` rows that become notices,
the reviewed announced-on file, its journalled write (one in-memory row) and the backtest's
count of the exits. Plain examples; no bars."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hat.hat_support import memory_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.backtest.data import demerger_exits
from app.hat.backtest.report import DEMERGER, TradeRow, forced_exits
from app.hat.data.ca_bc import FeedRow, read_bc
from app.hat.data.demerger_notice import (
    EVENT_ANNOUNCED,
    AnnouncedFileError,
    Demerger,
    apply_announced,
    demerger_rows,
    exit_day,
    plan_announced,
    read_announced,
)
from app.hat.models import HatCorporateAction, HatEvent

# HEG's demerger: ex-date (and, under T+1, record date) Monday 2026-09-07
EX = date(2026, 9, 7)
SESSIONS = [date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4), EX, date(2026, 9, 8)]


def test_exit_is_the_last_session_before_the_ex_date() -> None:
    assert exit_day(SESSIONS, EX, date(2026, 7, 7)) == date(2026, 9, 4)


def test_a_holiday_before_the_ex_date_moves_the_exit_back() -> None:
    # Friday the 4th a holiday: the exit is Thursday's close
    sessions = [d for d in SESSIONS if d != date(2026, 9, 4)]
    assert exit_day(sessions, EX, date(2026, 9, 1)) == date(2026, 9, 3)


def test_announced_the_session_before_the_exit_session_exits() -> None:
    assert exit_day(SESSIONS, EX, date(2026, 9, 3)) == date(2026, 9, 4)


# on the exit session itself is too late: live reads the file that night, after the close
@pytest.mark.parametrize(
    "announced", [date(2026, 9, 4), date(2026, 9, 5), EX, date(2026, 9, 8), None]
)
def test_announced_too_late_or_never_dated_holds_through(announced: date | None) -> None:
    assert exit_day(SESSIONS, EX, announced) is None


def test_no_session_before_the_ex_date_holds_through() -> None:
    assert exit_day(SESSIONS, date(2026, 9, 2), date(2026, 8, 1)) is None


BC = (
    "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
    "EQ,HEG,HEG Ltd,2026-09-07,,,2026-09-07,,,DEMERGER\n"
    "BL,HEG,HEG Ltd,2026-09-07,,,2026-09-07,,,DEMERGER\n"
    "EQ,WIMPLAST,Wim Plast Ltd.,2026-06-09,,,,,,MERGER DEMERGER\n"
    "EQ,ABC,Abc Ltd,2026-09-08,,,2026-09-08,,,FVSPLT FRM RS 10 TO RS 2\n"
    "EQ,DEF,Def Ltd,2026-09-08,,,2026-09-08,,,BONUS 1:1\n"
    "EQ,GHI,Ghi Ltd,2026-09-08,,,2026-09-08,,,RIGHTS 1:5 @ PREMIUM RS 90\n"
    "BE,KMSUGAR,K.M.Sugar Mills Limited,2026-10-02,,,2026-10-01,,,DEMERGER\n"
)


def test_only_dated_demergers_become_notices_splits_bonuses_and_rights_do_not() -> None:
    rows = demerger_rows(read_bc(BC))
    assert [(r.symbol, r.ex_date) for r in rows] == [
        ("HEG", EX),
        ("KMSUGAR", date(2026, 10, 1)),
    ]


def test_one_notice_per_symbol_and_ex_date_across_series() -> None:
    rows = [FeedRow("HEG", "", s, EX, "demerger") for s in ("EQ", "BE")]
    assert len(demerger_rows(rows)) == 1


HEADER = "isin\tex_date\tannounced_on\tsource_url\tnote\n"


def test_the_announced_file_reads_and_refuses() -> None:
    rows = read_announced(HEADER + "INE545A01024\t2026-09-07\t2026-07-06\thttps://x\tok\n")
    assert rows[0].announced_on == date(2026, 7, 6)
    with pytest.raises(AnnouncedFileError, match="bad_header"):
        read_announced("isin\tex_date\n")
    with pytest.raises(AnnouncedFileError, match="no_source"):
        read_announced(HEADER + "INE545A01024\t2026-09-07\t2026-07-06\t\tok\n")
    with pytest.raises(AnnouncedFileError, match="bad_date"):
        read_announced(HEADER + "INE545A01024\t2026-09-07\tsoon\thttps://x\tok\n")
    line = "INE545A01024\t2026-09-07\t2026-07-06\thttps://x\tok\n"
    with pytest.raises(AnnouncedFileError, match="duplicate"):
        read_announced(HEADER + line + line)


def test_the_plan_dates_only_undated_rows_and_lists_the_rest() -> None:
    stored = [
        Demerger("a", "HEGAM", "INE545A01024", EX, None),
        Demerger("b", "TMPV", "INE155A01022", date(2025, 10, 14), date(2025, 9, 1)),
    ]
    rows = read_announced(
        HEADER
        + "INE545A01024\t2026-09-07\t2026-07-06\thttps://x\tok\n"
        + "INE155A01022\t2025-10-14\t2025-09-02\thttps://y\tlater\n"
        + "INE000000000\t2025-01-01\t2024-12-01\thttps://z\tunknown\n"
    )
    plan = plan_announced(stored, rows)
    assert {k: r.announced_on for k, r in plan.dated.items()} == {"a": date(2026, 7, 6)}
    assert [d.action_id for d, _ in plan.differing] == ["b"]
    assert [r.isin for r in plan.unmatched] == ["INE000000000"]
    assert not plan.replaced


def test_replace_takes_only_an_earlier_date() -> None:
    stored = [
        Demerger("a", "SINTEX", "INE429C01035", date(2017, 5, 25), date(2017, 5, 25)),
        Demerger("b", "TMPV", "INE155A01022", date(2025, 10, 14), date(2025, 9, 1)),
        Demerger("c", "HEGAM", "INE545A01024", EX, None),
    ]
    rows = read_announced(
        HEADER
        + "INE429C01035\t2017-05-25\t2017-05-16\thttps://x\tReg 42 letter\n"
        + "INE155A01022\t2025-10-14\t2025-09-02\thttps://y\tlater: kept\n"
        + "INE545A01024\t2026-09-07\t2026-07-06\thttps://z\tundated: dated\n"
    )
    plan = plan_announced(stored, rows, replace=True)
    assert {k: r.announced_on for k, (_, r) in plan.replaced.items()} == {"a": date(2017, 5, 16)}
    assert [d.action_id for d, _ in plan.differing] == ["b"]
    assert list(plan.dated) == ["c"]


def test_apply_replaces_the_planned_date_and_journals_it() -> None:
    engine = memory_engine(HatCorporateAction, HatEvent)
    with Session(engine) as session:
        row = HatCorporateAction(
            instrument_id=uuid.uuid4(), ex_date=date(2017, 5, 25), action_type="other",
            adjustment_factor=Decimal("0.9"), raw_text="demerger", source="ca_research",
            demerger=True, announced_on=date(2017, 5, 25),
        )  # fmt: skip
        session.add(row)
        session.flush()
        stored = [Demerger(str(row.id), "SINTEX", "INE429C01035", row.ex_date, row.announced_on)]
        line = "INE429C01035\t2017-05-25\t2017-05-16\thttps://x\tReg 42 letter\n"
        plan = plan_announced(stored, read_announced(HEADER + line), replace=True)
        now = datetime(2026, 10, 25, tzinfo=UTC)
        assert apply_announced(session, plan, now=now) == 1
        assert row.announced_on == date(2017, 5, 16)
        (event,) = session.scalars(select(HatEvent)).all()
        assert event.event_type == EVENT_ANNOUNCED
        assert event.payload["before"] == "2017-05-25"
        assert event.payload["source_url"] == "https://x"
        # a rerun of the same plan finds the date changed and writes nothing
        assert apply_announced(session, plan, now=now) == 0


def test_backtest_exits_under_the_chain_within_the_period() -> None:
    parent, chain = uuid.uuid4(), uuid.uuid4()
    rows = [
        (parent, EX, date(2026, 7, 7)),  # exits on the 4th, under its chain's id
        (parent, date(2026, 9, 8), None),  # never dated: holds through
    ]
    found = demerger_exits(rows, {str(parent): str(chain)}, SESSIONS, date(2026, 9, 1), EX)
    assert found == frozenset({(str(chain), date(2026, 9, 4))})
    assert not demerger_exits(rows, {}, SESSIONS, date(2026, 9, 5), EX)


def _trade(reason: str, pnl: str, r: str) -> TradeRow:
    day = date(2026, 9, 4)
    return TradeRow(
        day, day, "pb", "i", 1, Decimal(1), Decimal(1), reason, Decimal(pnl), Decimal(0),
        Decimal(r), Decimal(0), Decimal(0), 1, uuid.uuid4(), uuid.uuid4(),
    )  # fmt: skip


def test_the_metrics_count_the_demerger_exits_apart() -> None:
    trades = [
        _trade(DEMERGER, "-120.5", "-0.4"),
        _trade(DEMERGER, "30", "0.1"),
        _trade("x", "9", "1"),
    ]
    assert forced_exits(trades, DEMERGER) == {
        "trades": 2,
        "net_pnl": "-90.5",
        "sum_r": "-0.300000",
    }

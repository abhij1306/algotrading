"""`ca.adjust`'s demerger step without a readable `bc` file: the stored notices are still checked
and the night is alerted. The database calls are stubbed at the module's seams."""

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest

from app.hat.clock import SYSTEM_CLOCK
from app.hat.daily import nightly_ca
from app.hat.notify import RecordingNotifier

DAY = date(2026, 10, 23)


class Session:
    committed = False

    def commit(self) -> None:
        self.committed = True


def test_unreadable_bc_file_still_checks_the_stored_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[date] = []
    monkeypatch.setattr(nightly_ca, "announce_stored", lambda session: 0)
    monkeypatch.setattr(nightly_ca, "session_calendar", lambda session: None)
    monkeypatch.setattr(
        nightly_ca,
        "flag_positions",
        lambda session, day, sessions, now: checked.append(day) or (0, 0),
    )
    session, notifier = Session(), RecordingNotifier()
    deps = SimpleNamespace(
        session_factory=lambda: nullcontext(session), notifier=notifier, clock=SYSTEM_CLOCK
    )
    nightly_ca.CaAdjust(deps)._demergers(DAY, "NOT,A,BC,FILE\n1,2,3,4\n")  # type: ignore[arg-type]
    assert checked == [DAY]
    assert session.committed
    assert notifier.alerts == [(nightly_ca.ALERT_DEMERGERS_UNCHECKED, DAY, {})]

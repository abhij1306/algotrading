"""Plain tests of the pure seams of the 5-minute history import: the label convention and the
pre-open rows, the actions the history carries, the instrument of a day and the ISIN of a symbol."""

from datetime import date, datetime
from decimal import Decimal

from app.hat.data.bars import BarBuilder
from app.hat.data.bars5m_import import carried, chains_of, isin_of, owner_of, start_epoch
from app.hat.data.candles import aggregate, parse_candles, screen
from app.hat.time import IST


def _epoch(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).timestamp())


def _starts(day: date, labels: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The bar starts (IST hour, minute) the import keeps of rows labelled `labels` on `day`."""
    rows = [[start_epoch(_epoch(day, h, m)), 10.0, 11.0, 9.0, 10.5, 100] for h, m in labels]
    bars = aggregate("X", screen(parse_candles(rows)), BarBuilder())
    return [(b.start.hour, b.start.minute) for b in bars]


def test_end_labels_shift_to_the_bar_start_and_pre_open_rows_are_dropped() -> None:
    """Through 2022-01-21 a label is the bar's end: 09:10 and 09:15 are the pre-open, 09:20 the
    opening bar and 15:30 the last; after it the label is the start, 09:05 and 09:10 are the
    pre-open and a 15:30 row is after the close."""
    day = date(2021, 12, 22)
    assert _starts(day, [(9, 10), (9, 15), (9, 20), (15, 30)]) == [(9, 15), (15, 25)]
    later = date(2022, 1, 24)
    assert _starts(later, [(9, 5), (9, 10), (9, 15), (15, 25), (15, 30)]) == [(9, 15), (15, 25)]
    assert start_epoch(_epoch(date(2022, 1, 21), 15, 30)) == _epoch(date(2022, 1, 21), 15, 25)


def test_flat_zero_volume_rows_in_the_session_are_kept_and_bad_rows_refused() -> None:
    """A zero-volume row breaks no `REJECT_RULES` rule and is kept; a row whose low is above
    its open is refused under `ohlc` and leaves no bar."""
    day = date(2024, 3, 4)
    rows = [
        [_epoch(day, 9, 15), 10.0, 10.0, 10.0, 10.0, 0],
        [_epoch(day, 9, 20), 10.0, 11.0, 10.5, 10.6, 50],
    ]
    parsed = parse_candles(rows)
    assert [r.rule for r in parsed.rejected] == ["ohlc"]
    bars = aggregate("X", screen(parsed))
    assert [(b.start.minute, b.volume, b.high) for b in bars] == [(15, 0, Decimal("10.0000"))]


def test_a_day_belongs_to_the_one_chain_member_with_its_daily_bar() -> None:
    day = date(2023, 5, 2)
    daily = {("old", day): object(), ("new", date(2023, 5, 3)): object()}
    assert owner_of(day, ("new", "old"), daily) == "old"
    assert owner_of(date(2023, 5, 3), ("new", "old"), daily) == "new"
    assert owner_of(date(2023, 5, 4), ("new", "old"), daily) is None
    both = {("old", day): object(), ("new", day): object()}
    assert owner_of(day, ("new", "old"), both) is None  # ambiguous: fails closed


def test_chains_list_the_current_id_first() -> None:
    assert chains_of({"a": "c", "b": "c", "x": "y"}) == {"c": ("c", "a", "b"), "y": ("y", "x")}


def test_isin_of_needs_one_agreeing_isin() -> None:
    universe = {"ABC": "INE000A01011"}
    assert isin_of("ABC", universe, {}) == "INE000A01011"
    assert isin_of("ABC", universe, {"ABC": {"INE000A01011"}}) == "INE000A01011"
    assert isin_of("ABC", universe, {"ABC": {"INE999A01011"}}) is None
    assert isin_of("XYZ", universe, {"XYZ": {"INE111A01011"}}) == "INE111A01011"
    assert isin_of("XYZ", universe, {"XYZ": {"INE111A01011", "INE222A01011"}}) is None
    assert isin_of("NONE", universe, {}) is None


def test_carried_keeps_an_action_unless_the_sessions_before_fit_better_without_it() -> None:
    """The history carries the 2:1 split (the days before it fit only with its factor) and not
    the demerger (they fit only without it); a tie keeps the stored assumption."""
    split, demerger, quiet = date(2024, 6, 3), date(2023, 2, 1), date(2022, 5, 2)
    steps = {split: Decimal("0.5"), demerger: Decimal("0.9"), quiet: Decimal("0.99")}
    days = [date(2022, 4, 28), date(2023, 1, 30), date(2023, 1, 31), date(2024, 5, 30)]

    def fits(day: date, actions: list[tuple[date, Decimal]]) -> bool:
        has = {ex for ex, _ in actions if day < ex}
        if day >= demerger:
            return has == {split}
        if day >= quiet:
            return has == {split}
        return True  # a tiny factor both ways: a tie

    assert carried(steps, days, fits) == [(split, Decimal("0.5")), (quiet, Decimal("0.99"))]

"""DB2's pure seam: the store coverage diff (`data.coverage.coverage_gaps`) and what its alert
names. The nightly check itself is verified on the real store, never here."""

from datetime import date

from app.hat.data.coverage import ALERT_DAYS, Coverage, coverage_gaps


def _d(day: int) -> date:
    return date(2026, 10, day)


def test_gaps_lists_missing_and_extra_days_from_each_datasets_first_day() -> None:
    trading = [_d(d) for d in (1, 3, 6, 7, 8, 9, 22, 23, 26)]
    bars = [_d(d) for d in (3, 6, 7, 8, 9, 11, 23)]  # from the 3rd; the 22nd missing, 11th extra
    index = [_d(d) for d in (7, 9, 22)]  # from the 7th
    found = coverage_gaps(trading, bars, index, through=_d(23))
    assert found == Coverage(
        bars_missing=(_d(22),),
        bars_extra=(_d(11),),
        index_missing=(_d(8), _d(23)),  # the 26th is after `through`, the 1st-6th before the 7th
    )


def test_gaps_of_an_empty_dataset_are_none() -> None:
    assert coverage_gaps([_d(1), _d(5)], [], [], through=_d(31)) == Coverage()


def test_acknowledged_index_days_drop_out_bar_days_never() -> None:
    found = Coverage((_d(22),), (_d(11),), (_d(8), _d(23)))
    assert found.without_index({_d(8), _d(22)}) == Coverage((_d(22),), (_d(11),), (_d(23),))


def test_payload_round_trips_and_the_alert_names_the_first_days() -> None:
    index = tuple(date(2015, 3, d) for d in range(1, ALERT_DAYS + 3))
    found = Coverage((_d(22),), (), index)
    assert Coverage.from_payload(found.payload()) == found
    detail = found.alert_detail()
    assert detail["bars_missing"] == 1
    assert detail["bars_missing_01"] == "2026-10-22"
    assert detail["bars_extra"] == 0
    assert detail["index_missing"] == len(index)
    assert detail[f"index_missing_{ALERT_DAYS:02d}"] == index[ALERT_DAYS - 1].isoformat()
    assert f"index_missing_{ALERT_DAYS + 1:02d}" not in detail

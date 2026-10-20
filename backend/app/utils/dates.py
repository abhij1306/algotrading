"""
Request date parsing: one owner for turning a YYYY-MM-DD string into a `date`.

Invalid input raises `InvalidDateError`, which the app's SmartTrader handler answers with
a fixed 400 naming the field.
"""

from datetime import date, datetime

from ..exceptions import InvalidDateError

ISO_DATE_FORMAT = "%Y-%m-%d"


def parse_iso_date(value: str, *, field: str) -> date:
    """The calendar date in `value` (YYYY-MM-DD), or InvalidDateError naming `field`.

    Empty strings, surrounding whitespace and datetime strings are rejected.
    """
    try:
        return datetime.strptime(value, ISO_DATE_FORMAT).date()
    except (TypeError, ValueError) as exc:
        raise InvalidDateError(field) from exc


def parse_optional_iso_date(value: str | None, *, field: str) -> date | None:
    """None for a missing or empty value, else `parse_iso_date(value, field=field)`."""
    return parse_iso_date(value, field=field) if value else None

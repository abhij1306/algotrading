"""The owner's correction list: what `parse_list` reads and what it refuses (no database)."""

from datetime import date
from decimal import Decimal

import pytest

from app.hat.data.ca_corrections import CorrectionListError, parse_list

HEADER = "op\tisin\tex_date\ttype\tfactor\tnew_ex_date\tnote\n"


def test_parse_list() -> None:
    items = parse_list(
        HEADER
        + "# a comment\n"
        + "add\tINE280A01010\t2011-06-23\tbonus\t0.5\t\tTITAN bonus 1:1 leg missing\n"
        + "replace\tINE258A01016\t2022-09-08\tother\t0.7961\t\tmarket gap\n"
        + "delete\tINE477B01010\t2021-08-05\tbonus\t\t\tpreference-share bonus\n"
        + "move\tINE000X01010\t2020-01-01\tsplit\t\t2020-01-03\tdate per NSE\n"
    )
    assert [i.op for i in items] == ["add", "replace", "delete", "move"]
    assert items[0].factor == Decimal("0.5")
    assert items[0].line == 3
    assert items[2].factor is None
    assert items[3].new_ex_date == date(2020, 1, 3)
    assert items[3].factor is None
    assert not any(i.demerger for i in items)


def test_parse_list_reads_a_demerger_add_as_a_flagged_other() -> None:
    line = "add\tINE000X01010\t2024-09-20\tdemerger\t0.7108\t\tmarket gap\n"
    (item,) = parse_list(HEADER + line)
    assert (item.action_type, item.demerger) == ("other", True)
    other = parse_list(HEADER + line.replace("demerger", "other"))[0]
    assert (other.action_type, other.demerger) == ("other", False)


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("replace\tINE1\t2020-01-01\tsplit\t\t\tn\n", "bad_factor@2"),
        ("delete\tINE1\t2020-01-01\tsplit\t0.5\t\tn\n", "bad_factor@2"),
        ("add\tINE1\t2020-01-01\tsplit\tx\t\tn\n", "bad_factor@2"),
        ("move\tINE1\t2020-01-01\tsplit\t\t\tn\n", "bad_new_ex_date@2"),
        ("replace\tINE1\t2020-01-01\tsplit\t0.5\t2020-01-02\tn\n", "bad_new_ex_date@2"),
        ("rewrite\tINE1\t2020-01-01\tsplit\t0.5\t\tn\n", "bad_op@2"),
        ("replace\tINE1\t2020-13-01\tsplit\t0.5\t\tn\n", "bad_date@2"),
        ("replace\tINE1\t2020-01-01\tsplit\t0.5\n", "bad_columns@2"),
        ("replace\tINE1\t2020-01-01\tdemerger\t0.5\t\tn\n", "bad_type@2"),
    ],
)
def test_parse_list_refuses(line: str, code: str) -> None:
    with pytest.raises(CorrectionListError) as exc:
        parse_list(HEADER + line)
    assert exc.value.code == code


def test_parse_list_needs_the_header() -> None:
    with pytest.raises(CorrectionListError) as exc:
        parse_list("add\tINE1\t2020-01-01\tbonus\t0.5\t\tn\n")
    assert exc.value.code == "bad_header"


def test_parse_list_reads_a_dividend_cash_column() -> None:
    items = parse_list(
        HEADER.replace("note\n", "note\tcash\n")
        + "replace\tINE154A01025\t2024-06-04\tdividend\t1\t\tfeed said 6.52\t6.25\n"
        + "add\tINE002A01018\t2024-08-19\tdividend\t1\t\tmissed, NSE file\t10\n"
        + "delete\tINE477B01010\t2021-08-05\tbonus\t\t\tpreference-share bonus\t\n"
    )
    assert [i.cash for i in items] == [Decimal("6.25"), Decimal(10), None]


@pytest.mark.parametrize(
    "line",
    [
        "replace\tINE1\t2024-06-04\tdividend\t1\t\tn\t0\n",  # not positive
        "replace\tINE1\t2024-06-04\tdividend\t1\t\tn\t6.2.5\n",
        "delete\tINE1\t2024-06-04\tdividend\t\t\tn\t6.25\n",  # a delete takes none
        "move\tINE1\t2024-06-04\tdividend\t\t2024-06-05\tn\t6.25\n",
    ],
)
def test_parse_list_refuses_a_bad_cash(line: str) -> None:
    text = HEADER.replace("note\n", "note\tcash\n") + line
    with pytest.raises(CorrectionListError) as refused:
        parse_list(text)
    assert refused.value.code == "bad_cash@2"


def test_parse_list_reads_the_optional_known_on_column() -> None:
    header = HEADER.rstrip("\n") + "\tknown_on\n"
    items = parse_list(
        header
        + "add\tINE847C01020\t2024-09-20\tother\t0.2892\t\tmarket gap\t2024-09-26\n"
        + "replace\tINE258A01016\t2022-09-08\tother\t0.7961\t\tmarket gap\t\n"
    )
    assert [i.known_on for i in items] == [date(2024, 9, 26), None]
    assert parse_list(HEADER + "add\tINE1\t2020-01-01\tbonus\t0.5\t\tn\n")[0].known_on is None
    with pytest.raises(CorrectionListError) as exc:
        parse_list(header + "add\tINE1\t2020-01-01\tbonus\t0.5\t\tn\n")
    assert exc.value.code == "bad_columns@2"
    both = HEADER.rstrip("\n") + "\tcash\tknown_on\n"
    item = parse_list(both + "add\tINE1\t2024-08-19\tdividend\t1\t\tNSE file\t10\t2024-08-30\n")[0]
    assert (item.cash, item.known_on) == (Decimal(10), date(2024, 8, 30))

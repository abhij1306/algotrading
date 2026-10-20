"""Paper position math and fill pricing: exact Decimal results, quantized to paise."""

from decimal import Decimal

import pytest

from app.services.execution.paper_book import (
    PaperPosition,
    estimated_charges,
    fill_price,
    money_float,
    to_decimal,
    to_money,
)


def book(*fills: tuple[str, int, str]) -> PaperPosition:
    position = PaperPosition()
    for side, qty, price in fills:
        position.apply_fill(side, qty, Decimal(price))
    return position


def state(position: PaperPosition) -> tuple[int, Decimal, Decimal]:
    return position.net_qty, position.entry_price, position.realized_pnl


# --- Position math -----------------------------------------------------------------


def test_buy_opens_a_long_at_the_fill_price() -> None:
    assert state(book(("BUY", 10, "100.50"))) == (10, Decimal("100.50"), Decimal("0"))


def test_sell_from_flat_opens_a_short() -> None:
    assert state(book(("SELL", 4, "250"))) == (-4, Decimal("250"), Decimal("0"))


def test_adding_to_a_long_re_averages_the_entry() -> None:
    position = book(("BUY", 10, "100"), ("BUY", 20, "103"))

    assert state(position) == (30, Decimal("102"), Decimal("0"))


def test_partial_close_realises_pnl_and_keeps_the_entry() -> None:
    position = book(("BUY", 10, "100"), ("BUY", 20, "103"), ("SELL", 10, "110"))

    assert state(position) == (20, Decimal("102"), Decimal("80"))


def test_full_close_goes_flat_with_zero_entry() -> None:
    position = book(("BUY", 10, "100"), ("SELL", 10, "95.25"))

    assert state(position) == (0, Decimal("0"), Decimal("-47.50"))


def test_short_cover_realises_the_drop_as_profit() -> None:
    position = book(("SELL", 5, "200"), ("BUY", 2, "190"))

    assert state(position) == (-3, Decimal("200"), Decimal("20"))


def test_long_reverses_to_short_through_zero() -> None:
    position = book(("BUY", 10, "100"), ("SELL", 25, "90"))

    # The 10 long close at a 10 loss each; the other 15 open a short at 90.
    assert state(position) == (-15, Decimal("90"), Decimal("-100"))


def test_short_reverses_to_long_through_zero() -> None:
    position = book(("SELL", 3, "50"), ("BUY", 5, "45"))

    assert state(position) == (2, Decimal("45"), Decimal("15"))


def test_closing_a_repeating_average_realises_the_exact_difference() -> None:
    # Entry is 302/3 = 100.666..., which has no finite decimal form.
    position = book(("BUY", 1, "100"), ("BUY", 2, "101"), ("SELL", 3, "101"))

    assert state(position) == (0, Decimal("0"), Decimal("1"))


def test_unrealised_pnl_marks_the_open_quantity() -> None:
    long = book(("BUY", 10, "100"), ("BUY", 20, "103"))
    short = book(("SELL", 4, "250"))

    assert (long.unrealized_pnl(Decimal("101.5")), short.unrealized_pnl(Decimal("240"))) == (
        Decimal("-15.0"),
        Decimal("40"),
    )


def test_many_small_fills_sum_exactly() -> None:
    # Float drift: 0.1 + 0.1 + 0.1 == 0.30000000000000004 in binary floating point.
    assert 0.1 + 0.1 + 0.1 != 0.3
    position = PaperPosition()
    position.apply_fill("BUY", 3, to_decimal(0.1))
    for _ in range(3):
        position.apply_fill("SELL", 1, to_decimal(0.2))

    assert state(position) == (0, Decimal("0"), Decimal("0.3"))


def test_a_thousand_fills_of_one_paisa_edge_sum_exactly() -> None:
    position = PaperPosition()
    for _ in range(1000):
        position.apply_fill("BUY", 1, to_decimal(10.1))
    for _ in range(1000):
        position.apply_fill("SELL", 1, to_decimal(10.2))

    assert state(position) == (0, Decimal("0"), Decimal("100.0"))


@pytest.mark.parametrize(("side", "qty"), [("HOLD", 1), ("BUY", 0), ("SELL", -2)])
def test_invalid_fill_is_refused(side: str, qty: int) -> None:
    with pytest.raises(ValueError, match="Invalid paper fill"):
        PaperPosition().apply_fill(side, qty, Decimal("1"))


# --- Fill pricing and rounding -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("100.005"), Decimal("100.01")),
        (Decimal("100.004999"), Decimal("100.00")),
        (Decimal("-0.005"), Decimal("-0.01")),
        (Decimal("2.675"), Decimal("2.68")),
    ],
)
def test_to_money_rounds_half_up_to_paise(value: Decimal, expected: Decimal) -> None:
    assert to_money(value) == expected


def test_to_money_rounds_2_675_up_where_float_round_does_not() -> None:
    assert (round(2.675, 2), money_float(to_decimal(2.675))) == (2.67, 2.68)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.1, Decimal("0.1")), ("12.30", Decimal("12.30")), (None, Decimal("0")), ("x", Decimal("0"))],
)
def test_to_decimal_reads_floats_through_their_shortest_repr(
    value: object, expected: Decimal
) -> None:
    assert to_decimal(value) == expected


@pytest.mark.parametrize(
    ("side", "ref", "limit", "expected"),
    [
        ("BUY", "100", None, Decimal("100.05")),
        ("SELL", "100", None, Decimal("99.95")),
        ("BUY", "100", "100.02", Decimal("100.02")),
        ("SELL", "100", "99.99", Decimal("99.99")),
        # 333.33 * 1.0005 = 333.496665 -> 333.50
        ("BUY", "333.33", None, Decimal("333.50")),
    ],
)
def test_fill_price_slips_against_the_trader_and_respects_the_limit(
    side: str, ref: str, limit: str | None, expected: Decimal
) -> None:
    limit_price = Decimal(limit) if limit is not None else None

    assert fill_price(side, Decimal(ref), Decimal("5"), limit_price) == expected


@pytest.mark.parametrize(
    ("side", "ref", "slippage_bps", "limit", "expected"),
    [
        # Capped at an off-paisa limit: BUY rounds down, SELL rounds up.
        ("BUY", "810.40", "5", "810.405", Decimal("810.40")),
        ("SELL", "810.41", "5", "810.405", Decimal("810.41")),
        # Uncapped, but half up would cross the limit (810.4050 -> 810.41 > 810.4051).
        ("BUY", "810.4050", "0", "810.4051", Decimal("810.40")),
        ("SELL", "810.4049", "0", "810.4049", Decimal("810.41")),
        # A limit already on a paisa is kept exactly.
        ("BUY", "810.40", "5", "810.40", Decimal("810.40")),
    ],
)
def test_capped_fill_never_crosses_the_limit(
    side: str, ref: str, slippage_bps: str, limit: str, expected: Decimal
) -> None:
    fill = fill_price(side, Decimal(ref), Decimal(slippage_bps), Decimal(limit))

    assert fill == expected
    assert fill <= Decimal(limit) if side == "BUY" else fill >= Decimal(limit)


def test_estimated_charges_are_quantized_on_the_notional() -> None:
    # 100.05 * 10 * 3.5 / 10000 = 0.350175
    assert estimated_charges(Decimal("100.05"), 10, Decimal("3.5")) == Decimal("0.35")
    assert estimated_charges(Decimal("100.05"), 0, Decimal("3.5")) == Decimal("0.00")

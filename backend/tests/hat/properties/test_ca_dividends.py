"""The dividend import's and the total-return series' pure seams (CA3): the purpose-text parser,
the bonus 1% rule, the dividend factor, the cash a held position earns on an ex-date and the
rescale of a price to a day's scale. Plain examples from real feed rows; no database, no bars."""

from datetime import date
from decimal import Decimal

from app.hat.backtest.data import rescaled
from app.hat.backtest.loop import dividend_credit
from app.hat.data.ca_dividends import (
    KIND_FINAL,
    KIND_INTERIM,
    KIND_SPECIAL,
    FeedRow,
    Payout,
    bonus_counts,
    combine,
    over_yield,
    payouts,
)
from app.hat.data.lineage import chain_dividends
from app.hat.data.store import DividendCash, dividend_factor


def _p(kind: str, amount: str) -> Payout:
    return Payout(kind, Decimal(amount))


def test_payouts_reads_one_amount_per_kind() -> None:
    assert payouts("Interim Dividend - Rs 5 Per Share") == (_p(KIND_INTERIM, "5"),)
    assert payouts("Annual General Meeting/Dividend Rs 4 Per Share") == (_p(KIND_FINAL, "4"),)
    assert payouts("Interim Dividend - Rs 5 Per Share / Special Dividend - Rs 10 Per Share") == (
        _p(KIND_INTERIM, "5"),
        _p(KIND_SPECIAL, "10"),
    )


def test_payouts_never_guesses() -> None:
    assert payouts("Interim Dividend") == "no_amount"
    assert payouts("Interim Dividend @ 12% On Equity Shares.") == "percent_of_face_value"
    assert payouts("Bonus 1:1") == "no_dividend"
    assert payouts("Dividend On Preference Shares Rs 2 Per Share") == "preference_dividend"


def test_combine_counts_a_repeated_row_once_and_adds_kinds() -> None:
    day = date(2024, 2, 9)

    def row(subject: str) -> FeedRow:
        return FeedRow("ITC", "INE154A01025", "EQ", day, subject)

    once = row("Interim Dividend - Rs 6.25 Per Share")
    assert combine([once, once]) == (_p(KIND_INTERIM, "6.25"),)
    assert combine([once, row("Special Dividend - Rs 2 Per Share")]) == (
        _p(KIND_INTERIM, "6.25"),
        _p(KIND_SPECIAL, "2"),
    )


def test_over_yield_sends_more_than_half_the_close_to_review() -> None:
    assert not over_yield(Decimal(50), Decimal(100))
    assert over_yield(Decimal("50.01"), Decimal(100))
    assert over_yield(Decimal(1), Decimal(0))


def test_bonus_counts_from_one_percent_of_the_close() -> None:
    # NTPC 2015: Rs 12.50 of debentures on a close of 154.75 (8.08%) is cash
    assert bonus_counts(Decimal("12.5"), Decimal("154.75"))
    # BRITANNIA 2021: Rs 29 on 3416 (0.85%) stays price-neutral
    assert not bonus_counts(Decimal(29), Decimal(3416))
    assert bonus_counts(Decimal(1), Decimal(100))
    assert not bonus_counts(Decimal(1), Decimal(0))


def test_dividend_factor_is_one_minus_the_yield() -> None:
    ntpc = dividend_factor(Decimal("12.5"), Decimal("154.75"))  # 40 digits
    assert ntpc is not None
    assert round(ntpc, 10) == Decimal("0.9192245557")
    assert dividend_factor(Decimal(5), Decimal(100)) == Decimal("0.95")
    assert dividend_factor(Decimal(0), Decimal(100)) is None
    assert dividend_factor(Decimal(100), Decimal(100)) is None
    assert dividend_factor(Decimal(1), Decimal(0)) is None


def test_chain_dividends_keeps_the_copy_nearest_the_current_isin() -> None:
    day = date(2023, 3, 24)
    heads = {"old": ("new", 1), "new": ("new", 0)}
    found = chain_dividends([("old", day, Decimal(2)), ("new", day, Decimal(3))], heads)
    assert found == [DividendCash("new", day, Decimal(3))]


def test_dividend_credit_pays_the_shares_held_into_the_ex_date() -> None:
    # 120 shares of ITC held into a Rs 6.25 interim: Rs 750 into cash on the ex-date
    assert dividend_credit(120, Decimal("6.25")) == Decimal("750.00")
    # a split after the ex-date: the cash per basis share is the amount times the factor
    assert dividend_credit(3, Decimal("10") * Decimal("0.2")) == Decimal("6.00")
    assert dividend_credit(7, Decimal("0.333333")) == Decimal("2.33")


def test_rescaled_prices_a_bar_on_the_day_scale() -> None:
    # 154.75 before NTPC's ex-date, on the ex-date's scale (factor 0.9192): 142.2502
    assert rescaled(1_547_500, 0.9192245557350566) == Decimal("142.2500")
    assert rescaled(1_547_500, 1.0) == Decimal("154.75")

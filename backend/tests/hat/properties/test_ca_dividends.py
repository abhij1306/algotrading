"""The dividend import's and the total-return series' pure seams (CA3): the purpose-text parser,
the bonus 1% rule, the dividend factor, the cash a held position earns on an ex-date and the
rescale of a price to a day's scale. Plain examples from real feed rows; no database, no bars."""

from datetime import date
from decimal import Decimal

import pytest

from app.hat.backtest.data import rescaled
from app.hat.data.ca_dividends import (
    KIND_FINAL,
    KIND_INTERIM,
    KIND_SPECIAL,
    T_NIGHTLY,
    FeedRow,
    Payout,
    bc_subject,
    bonus_counts,
    combine,
    feed_proposals,
    nightly_window,
    over_yield,
    payouts,
    read_bc,
)
from app.hat.data.ca_import import CaFileError
from app.hat.data.lineage import chain_dividends
from app.hat.data.store import DividendCash, dividend_factor
from app.hat.money import dividend_credit


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


# --- NSE's daily corporate-action file (CA4) ----------------------------------------------------

BC_HEADER = (
    "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
)


@pytest.mark.parametrize(
    ("purpose", "parts"),
    [
        # rows of NSE's bc files of June to October 2026, as listed
        ("INTDIV - RS 7.50 PER SH", [(KIND_INTERIM, "7.50")]),
        ("DIV - RE 0.006 PER SH", [(KIND_FINAL, "0.006")]),
        ("DIV - RS5 PER SH", [(KIND_FINAL, "5")]),
        ("DIV - RS. 8 PER SH", [(KIND_FINAL, "8")]),
        ("SPLDIV - RS 10 PER SH", [(KIND_SPECIAL, "10")]),
        ("DIV-RS 8/SPL DIV-RS 2", [(KIND_FINAL, "8"), (KIND_SPECIAL, "2")]),
        ("DIV-RE 1/SPLDIV-RE 0.5", [(KIND_FINAL, "1"), (KIND_SPECIAL, "0.5")]),
        ("DIV/SPDV RS 5/RS 3", [(KIND_FINAL, "5"), (KIND_SPECIAL, "3")]),
        ("DIV/SPDV-RS 5 & RS 3", [(KIND_FINAL, "5"), (KIND_SPECIAL, "3")]),
        ("DIVSPDV - RS 5 & 2", [(KIND_FINAL, "5"), (KIND_SPECIAL, "2")]),
        ("INTDVSPDVRS 4 & 6", [(KIND_INTERIM, "4"), (KIND_SPECIAL, "6")]),
        ("DIV - RS 2/FVSPLT FRM RS 10 TO RS 2", [(KIND_FINAL, "2")]),
    ],
)
def test_bc_purpose_reads_as_the_feed(purpose: str, parts: list[tuple[str, str]]) -> None:
    assert payouts(bc_subject(purpose)) == tuple(Payout(k, Decimal(a)) for k, a in parts)


def test_bc_purpose_without_a_readable_amount_is_never_guessed() -> None:
    assert payouts(bc_subject("DIV - RE PER 0.125 SH")) == "no_amount"  # JTLIND 2026-09-09
    assert payouts(bc_subject("BONUS 2:1")) == "no_dividend"
    assert payouts(bc_subject("FVSPLT FRM RS 10 TO RS 2")) == "no_dividend"


def test_read_bc_keeps_the_equity_rows_of_the_night() -> None:
    rows = read_bc(
        BC_HEADER
        + "EQ,TECHM,TECH MAHINDRA LIMITED,2026-10-23,,,2026-10-23,,,INTDIV - RS 15 PER SH\n"
        + "BE,TECHM,TECH MAHINDRA LIMITED,2026-10-23,,,2026-10-23,,,INTDIV - RS 15 PER SH\n"
        + "T0,TECHM,TECH MAHINDRA LIMITED,2026-10-23,,,2026-10-23,,,INTDIV - RS 15 PER SH\n"
        + "N0,1140AFIL28,AFIL 11.40% 2028 Sr A1,2026-10-26,,,2026-10-26,,,INTEREST PAYMENT\n"
        + "EQ,LTTS,L&T TECHNOLOGY SER. LTD.,2026-10-27,,,2026-10-27,,,INTDIV - RS 20 PER SH\n"
        + "EQ,ANGELONE,ANGEL ONE LIMITED,2026-10-22,,,2026-10-22,,,INTDIV - RS 1.40 PER SH\n"
        + "EQ,AIFL,ASHOKA FINANCE LTD,2026-10-26,,,,,,CAPITAL REDUCTION\n"
    )
    assert [r.series for r in rows] == ["EQ", "BE", "EQ", "EQ", "EQ"]
    night = nightly_window(rows, date(2026, 10, 23))  # LTTS goes ex later, AIFL names no date
    proposals = feed_proposals(night, T_NIGHTLY)
    assert [(p.symbol, p.ex_date, p.parts) for p in proposals] == [
        ("ANGELONE", date(2026, 10, 22), (Payout(KIND_INTERIM, Decimal("1.40")),)),
        ("TECHM", date(2026, 10, 23), (Payout(KIND_INTERIM, Decimal(15)),)),
    ]


def test_read_bc_refuses_a_file_without_its_columns() -> None:
    with pytest.raises(CaFileError) as refused:
        read_bc("SYMBOL,PURPOSE\nTECHM,INTDIV - RS 15 PER SH\n")
    assert refused.value.code == "bc_columns"


def test_an_amount_before_its_label_is_read() -> None:
    assert payouts("Rs 50/- Per Share (Special Dividend)") == (Payout(KIND_SPECIAL, Decimal(50)),)


def test_grouped_figures_read_whole_and_a_bad_comma_is_review() -> None:
    assert payouts("Special Dividend - Rs 1,000 Per Share") == (
        Payout(KIND_SPECIAL, Decimal(1000)),
    )
    assert payouts("Dividend - Rs 1,00,000 Per Share") == (Payout(KIND_FINAL, Decimal(100000)),)
    assert payouts(bc_subject("DIV - RS 1,250.50 PER SH")) == (
        Payout(KIND_FINAL, Decimal("1250.50")),
    )
    assert payouts("Dividend - Rs 2,50 Per Share") == "bad_figure"


def test_a_kind_without_an_amount_is_never_a_partial_sum() -> None:
    def row(subject: str) -> FeedRow:
        return FeedRow("X", "INE000X01010", "EQ", date(2024, 1, 2), subject)

    assert (
        combine([row("Interim Dividend - Rs 5 Per Share"), row("Special Dividend")]) == "no_amount"
    )
    # the same kind without its figure is a revision the other row settles
    assert combine([row("Interim Dividend"), row("Interim Dividend - Rs 5 Per Share")]) == (
        Payout(KIND_INTERIM, Decimal(5)),
    )

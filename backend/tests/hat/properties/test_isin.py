"""The ISIN predicates (`data/isin.py`) over real NSE ISINs."""

import pytest

from app.hat.data.isin import etf_unit, ordinary_equity, rights_entitlement, voting_equity

# isin, ordinary equity, a company's own equity class, rights entitlement, ETF unit
CASES = [
    ("INE002A01018", True, True, False, False),  # RELIANCE
    ("INE0CU601026", True, True, False, False),
    ("IN9155A01020", True, False, False, False),  # TATAMTRDVR, a DVR class
    ("INE002A20018", False, False, True, False),  # RIL-RE, a rights entitlement
    ("INE932X20018", False, False, True, False),
    ("INF204KB14I2", False, False, False, True),  # an ETF
    ("INE183W23014", False, False, False, False),  # an InvIT
    ("INE041025011", False, False, False, False),  # a REIT
    ("INE002A0101", False, False, False, False),  # too short
]


@pytest.mark.parametrize(("isin", "ordinary", "voting", "rights", "etf"), CASES)
def test_isin_predicates(isin: str, ordinary: bool, voting: bool, rights: bool, etf: bool) -> None:
    assert ordinary_equity(isin) is ordinary
    assert voting_equity(isin) is voting
    assert rights_entitlement(isin) is rights
    assert etf_unit(isin) is etf


def test_no_isin_is_no_equity() -> None:
    assert not ordinary_equity(None)
    assert not voting_equity(None)

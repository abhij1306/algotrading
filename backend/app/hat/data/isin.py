"""What an Indian ISIN says about its security, by its characters alone: `IN`, the issuer kind
(`E` a company, `9` a company's DVR or partly paid shares, `F` a fund), four issuer characters,
a two-digit security type (`01` equity shares, `20` rights entitlements, `23` and `25` InvIT and
REIT units), a serial and a check digit. The ISIN is in every bar, so each predicate is point in
time and needs no reference data. Each caller names the one it means.
"""

from __future__ import annotations

import re

# The ranked universe (D257): a company's (`E`) or its DVR (`9`) equity shares (`01`). The
# features state records this text, so a change to it rebuilds every date.
ORDINARY_EQUITY = r"^IN[E9][0-9A-Z]{4}01[0-9A-Z]{2}[0-9]$"
_ORDINARY_EQUITY = re.compile(ORDINARY_EQUITY)


def ordinary_equity(isin: str | None) -> bool:
    """An ordinary equity share (`ORDINARY_EQUITY`): not an ETF or fund unit, a rights
    entitlement, InvIT or REIT units or a debt security."""
    return isin is not None and _ORDINARY_EQUITY.match(isin) is not None


def voting_equity(isin: str | None) -> bool:
    """An ordinary equity share of a company itself (`INE`, type `01`): the class a resulting
    company lists, never the DVR or partly paid class (`IN9`) a parent issued before."""
    return ordinary_equity(isin) and isin is not None and isin.startswith("INE")


def rights_entitlement(isin: str) -> bool:
    """A rights entitlement (type `20`, the `-RE` symbols)."""
    return len(isin) == 12 and isin[7:9] == "20"


def etf_unit(isin: str) -> bool:
    """An ETF or mutual fund unit (`INF`)."""
    return isin.startswith("INF")


def trust_unit(isin: str) -> bool:
    """A REIT or InvIT unit (types `25` and `23`)."""
    return len(isin) == 12 and isin[7:9] in ("23", "25")

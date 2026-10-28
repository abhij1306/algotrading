"""The canonical items of the fundamentals store, and how each source's raw names map into them.
Pure tables and two classifiers (docs/fundamentals.md lists the items and their meaning).

An NSE element maps to an item only in the document's main contexts (`OneD` the quarter, `FourD`
the year to date, `OneI`/`FourI` the period end); a dimensional context (a segment, a line of
other expenses) keeps its raw element only. Where several elements carry one item, the first one
present in the context wins (a bank tags `InterestEarned` where others tag
`RevenueFromOperations`; an NBFC tags both and the first wins). Banks, NBFCs and insurers use
other taxonomies: what has a counterpart is mapped, the rest keeps its raw element and the
filing's `family` flags it.
"""

from __future__ import annotations

import re

# statements
PL = "pl"
BS = "bs"
CF = "cf"
SEGMENT = "segment"
OTHER = "other"
RATIO = "ratio"
SHAREHOLDING = "shareholding"
SNAPSHOT = "snapshot"
STATEMENTS = (PL, BS, CF, SEGMENT, OTHER, RATIO, SHAREHOLDING, SNAPSHOT)

# period kinds
QUARTER = "quarter"
YTD = "ytd"  # the financial year to date, shorter than a year (from `period_start`)
ANNUAL = "annual"
INSTANT = "instant"  # as at `period_end` (a balance sheet, a shareholding pattern, a ratio)
PERIOD_KINDS = (QUARTER, YTD, ANNUAL, INSTANT)

# units: every money value is in rupees
INR = "inr"
INR_PER_SHARE = "inr_per_share"
SHARES = "shares"
PURE = "pure"  # a ratio as the source states it (an XBRL `xbrli:pure`)
FRACTION = "fraction"  # a percentage divided by 100
DAYS = "days"
COUNT = "count"
UNITS = (INR, INR_PER_SHARE, SHARES, PURE, FRACTION, DAYS, COUNT)

XBRL_UNITS = {
    "iso4217:inr": INR,
    "iso4217:inr/xbrli:shares": INR_PER_SHARE,
    "xbrli:shares": SHARES,
    "xbrli:pure": PURE,
}

# --- NSE XBRL ----------------------------------------------------------------------------------

# item -> (statement, elements in priority order)
NSE_ITEMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "revenue": (PL, ("RevenueFromOperations", "TotalRevenueFromOperations", "InterestEarned")),
    "other_income": (PL, ("OtherIncome",)),
    "total_income": (PL, ("Income", "TotalIncome")),
    "material_cost": (PL, ("CostOfMaterialsConsumed",)),
    "purchases_stock": (PL, ("PurchasesOfStockInTrade",)),
    "inventory_change": (PL, ("ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",)),
    "employee_cost": (PL, ("EmployeeBenefitExpense", "EmployeesCost")),
    "finance_cost": (PL, ("FinanceCosts", "InterestExpended")),
    "depreciation": (PL, ("DepreciationDepletionAndAmortisationExpense",)),
    "other_expenses": (PL, ("OtherExpenses", "OtherOperatingExpenses")),
    # a bank's is before provisions and contingencies (`provisions`)
    "total_expenses": (PL, ("Expenses", "ExpenditureExcludingProvisionsAndContingencies")),
    "provisions": (PL, ("ProvisionsOtherThanTaxAndContingencies",)),
    "exceptional_items": (PL, ("ExceptionalItemsBeforeTax", "ExceptionalItems")),
    "pbt": (PL, ("ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax")),
    "tax": (PL, ("TaxExpense",)),
    "net_profit": (PL, ("ProfitLossForPeriod", "ProfitLossForThePeriod")),
    "net_profit_owners": (
        PL,
        (
            "ProfitOrLossAttributableToOwnersOfParent",
            "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
        ),
    ),
    "eps_basic": (
        PL,
        (
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsPerShareAfterExtraordinaryItems",
            "BasicEarningsPerShareBeforeExtraordinaryItems",
        ),
    ),
    "eps_diluted": (
        PL,
        (
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "DilutedEarningsLossPerShareFromContinuingOperations",
            "DilutedEarningsPerShareAfterExtraordinaryItems",
            "DilutedEarningsPerShareBeforeExtraordinaryItems",
        ),
    ),
    "paid_up_capital": (OTHER, ("PaidUpValueOfEquityShareCapital",)),
    "face_value": (OTHER, ("FaceValueOfEquityShareCapital",)),
    "gross_npa": (OTHER, ("GrossNonPerformingAssets",)),
    "net_npa": (OTHER, ("NonPerformingAssets",)),
    "gross_npa_pct": (OTHER, ("PercentageOfGrossNpa",)),
    "net_npa_pct": (OTHER, ("PercentageOfNpa",)),
    "cet1_ratio": (OTHER, ("CET1Ratio",)),
    # the balance sheet (instant contexts)
    "total_assets": (BS, ("Assets", "CapitalAndLiabilities")),
    "total_liabilities": (BS, ("Liabilities",)),
    "equity": (BS, ("Equity",)),
    "equity_owners": (BS, ("EquityAttributableToOwnersOfParent",)),
    "share_capital": (BS, ("EquityShareCapital", "Capital")),
    "reserves": (BS, ("OtherEquity", "ReservesAndSurplus")),
    "minority_interest": (BS, ("NonControllingInterest",)),
    "borrowings": (BS, ("Borrowings",)),
    "borrowings_noncurrent": (BS, ("BorrowingsNoncurrent",)),
    "borrowings_current": (BS, ("BorrowingsCurrent",)),
    "debt_securities": (BS, ("DebtSecurities",)),
    "subordinated_liabilities": (BS, ("SubordinatedLiabilities",)),
    "deposits": (BS, ("Deposits",)),
    "cash": (BS, ("CashAndCashEquivalents", "CashAndBalancesWithReserveBankOfIndia")),
    "inventories": (BS, ("Inventories",)),
    "receivables": (BS, ("TradeReceivablesCurrent", "TradeReceivables")),
    "payables": (BS, ("TradePayablesCurrent",)),
    "current_assets": (BS, ("CurrentAssets",)),
    "current_liabilities": (BS, ("CurrentLiabilities",)),
    "fixed_assets": (BS, ("PropertyPlantAndEquipment", "FixedAssets")),
    "cwip": (BS, ("CapitalWorkInProgress",)),
    "investments": (BS, ("Investments",)),
    "investments_noncurrent": (BS, ("NoncurrentInvestments",)),
    "investments_current": (BS, ("CurrentInvestments",)),
    "advances": (BS, ("Advances", "Loans")),
    # the cash flow statement (the year-to-date context of a half-year or year-end document)
    "cfo": (CF, ("CashFlowsFromUsedInOperatingActivities",)),
    "cfi": (CF, ("CashFlowsFromUsedInInvestingActivities",)),
    "cff": (CF, ("CashFlowsFromUsedInFinancingActivities",)),
    "capex_ppe": (CF, ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",)),
    "capex_intangibles": (CF, ("PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",)),
    "dividends_paid": (CF, ("DividendsPaidClassifiedAsFinancingActivities",)),
    "net_cash_flow": (
        CF,
        (
            "IncreaseDecreaseInCashAndCashEquivalents",
            "IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges",
        ),
    ),
}

ELEMENT_ITEM: dict[str, str] = {
    element: item for item, (_s, elements) in NSE_ITEMS.items() for element in elements
}
ITEM_STATEMENT: dict[str, str] = {item: statement for item, (statement, _e) in NSE_ITEMS.items()}

# A duration element of the cash flow statement: the Ind AS and bank templates name every line
# of it with one of these.
_CASH_FLOW = re.compile(
    r"^(CashFlows|AdjustmentsFor|OtherAdjustments|Proceeds|Repayments|Payments|CashReceipts"
    r"|CashPayments|CashAdvances|OtherCash|IncomeTaxesPaid|OtherInflowsOutflows"
    r"|IncreaseDecreaseInCash|EffectOfExchangeRate|CashAndCashEquivalentsCashFlowStatement"
    r"|ShareOfProfitAndLossFromPartnership|Purchase(?!sOfStockInTrade))"
    r"|ClassifiedAs(Operating|Investing|Financing)Activities$"
)
_SEGMENT = re.compile(r"Segment|UnAllocable", re.IGNORECASE)


def nse_statement(element: str, unit: str, instant: bool, dimensional: bool) -> str:
    """The statement of an NSE fact (a mapped element's own statement in a main context)."""
    if _SEGMENT.search(element):
        return SEGMENT
    if dimensional:
        return OTHER
    if element in ELEMENT_ITEM:
        return ITEM_STATEMENT[ELEMENT_ITEM[element]]
    if instant:
        return BS
    if _CASH_FLOW.search(element):
        return CF
    return OTHER if unit != INR and unit != INR_PER_SHARE else PL


def nse_items(elements: set[str]) -> dict[str, str]:
    """The item each element present in one main context maps to: per item, the first of its
    elements in priority order that is present."""
    out: dict[str, str] = {}
    for item, (_statement, candidates) in NSE_ITEMS.items():
        for element in candidates:
            if element in elements:
                out[element] = item
                break
    return out


# --- the secondary source (screener.in) ----------------------------------------------------------

# (section, normalised label) -> item; a label not here is stored with its raw text only
SCREENER_ITEMS: dict[tuple[str, str], str] = {
    **{
        (section, label): item
        for section in ("quarters", "profit-loss")
        for label, item in (
            ("sales", "revenue"),
            ("revenue", "revenue"),
            ("expenses", "operating_expenses"),
            ("operating profit", "operating_profit"),
            ("financing profit", "financing_profit"),
            ("opm %", "opm"),
            ("financing margin %", "financing_margin"),
            ("other income", "other_income"),
            ("interest", "finance_cost"),
            ("depreciation", "depreciation"),
            ("profit before tax", "pbt"),
            ("tax %", "tax_rate"),
            ("net profit", "net_profit"),
            ("eps in rs", "eps_basic"),
            ("dividend payout %", "dividend_payout"),
            ("gross npa %", "gross_npa_pct"),
            ("net npa %", "net_npa_pct"),
        )
    },
    ("balance-sheet", "equity capital"): "share_capital",
    ("balance-sheet", "reserves"): "reserves",
    ("balance-sheet", "borrowings"): "borrowings",
    ("balance-sheet", "borrowing"): "borrowings",
    ("balance-sheet", "deposits"): "deposits",
    ("balance-sheet", "other liabilities"): "other_liabilities",
    ("balance-sheet", "total liabilities"): "total_equity_and_liabilities",
    ("balance-sheet", "fixed assets"): "fixed_assets",
    ("balance-sheet", "cwip"): "cwip",
    ("balance-sheet", "investments"): "investments",
    ("balance-sheet", "other assets"): "other_assets",
    ("balance-sheet", "total assets"): "total_assets",
    ("cash-flow", "cash from operating activity"): "cfo",
    ("cash-flow", "cash from investing activity"): "cfi",
    ("cash-flow", "cash from financing activity"): "cff",
    ("cash-flow", "net cash flow"): "net_cash_flow",
    ("cash-flow", "free cash flow"): "free_cash_flow",
    ("ratios", "debtor days"): "debtor_days",
    ("ratios", "inventory days"): "inventory_days",
    ("ratios", "days payable"): "payable_days",
    ("ratios", "cash conversion cycle"): "cash_conversion_cycle",
    ("ratios", "working capital days"): "working_capital_days",
    ("ratios", "roce %"): "roce",
    ("ratios", "roe %"): "roe",
    ("shareholding", "promoters"): "promoters_pct",
    ("shareholding", "fiis"): "fii_pct",
    ("shareholding", "diis"): "dii_pct",
    ("shareholding", "government"): "government_pct",
    ("shareholding", "public"): "public_pct",
    ("shareholding", "others"): "others_pct",
    ("shareholding", "no. of shareholders"): "shareholders",
    ("top-ratios", "market cap"): "market_cap",
    ("top-ratios", "current price"): "price",
    ("top-ratios", "stock p/e"): "pe",
    ("top-ratios", "book value"): "book_value_per_share",
    ("top-ratios", "dividend yield"): "dividend_yield",
    ("top-ratios", "face value"): "face_value",
}

SCREENER_STATEMENT = {
    "quarters": PL,
    "profit-loss": PL,
    "balance-sheet": BS,
    "cash-flow": CF,
    "ratios": RATIO,
    "shareholding": SHAREHOLDING,
    "top-ratios": SNAPSHOT,
}


def screener_label(text: str) -> str:
    """A row label as the maps above key it: lower case, without the expand marker '+' and the
    explanation some labels carry after the name."""
    label = text.replace("\xa0", " ").replace("+", " ").strip().lower()
    label = label.split(" share of ")[0]  # 'CFO/EBITDA Share of operating profit ...'
    return " ".join(label.split())

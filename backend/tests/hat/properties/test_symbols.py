"""PBT symbols.* (HAT-DATA-024, K2): symbol conversion is total, pure, and round-trips between the
database ticker and the Fyers symbol; indexes and equities never swap series; aliases and renames
resolve to one ticker."""

from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.data.symbols import (
    FYERS_PATTERN,
    INDEX_SYMBOLS,
    SYMBOL_ALIASES,
    SymbolMaster,
    symbol_master,
)

tickers = st.from_regex(r"[A-Z][A-Z0-9&]{0,11}", fullmatch=True).filter(
    lambda t: t not in SYMBOL_ALIASES and t not in INDEX_SYMBOLS
)
real_tickers = st.sampled_from(["SBIN", "M&M", "L&TFH", "BAJAJ-AUTO", "TCS", "A.B", "20MICRONS"])
anything = st.text(max_size=30)
formats = st.sampled_from(["{t}", "NSE:{t}-EQ", "nse:{t}-eq", " {t} ", "BSE:{t}-EQ"])


@example(text="-")
@example(text="NSE:BAJAJ-AUTO-EQ")
@given(text=anything)
def test_conversion_is_total_and_valid(text: str) -> None:
    """For any text `to_db` never raises and is idempotent, and `to_fyers` either raises
    `ValueError` or returns a well-formed Fyers symbol that converts back to the same ticker."""
    ticker = symbol_master.to_db(text)
    assert symbol_master.to_db(ticker) == ticker
    try:
        fyers = symbol_master.to_fyers(text)
    except ValueError:
        return
    assert FYERS_PATTERN.match(fyers)
    assert symbol_master.to_db(fyers) == ticker
    assert symbol_master.to_fyers(fyers) == fyers


@given(ticker=st.one_of(tickers, real_tickers), template=formats)
def test_equities_roundtrip_through_any_format(ticker: str, template: str) -> None:
    """A database ticker written in any accepted format (bare, Fyers with any case, padded, another
    series or exchange) converts to the same `NSE:<ticker>-EQ` and back to that ticker."""
    text = template.format(t=ticker)
    assert symbol_master.to_db(text) == ticker
    assert symbol_master.to_fyers(text) == f"NSE:{ticker}-EQ"
    assert symbol_master.to_db(symbol_master.to_fyers(text)) == ticker
    assert symbol_master.to_db(f"NSE:{ticker}-BE") == ticker  # other series name the same stock


@given(index=st.sampled_from(sorted(INDEX_SYMBOLS)))
def test_indexes_use_the_index_series(index: str) -> None:
    """An index ticker is always an `-INDEX` symbol, on BSE for SENSEX and NSE otherwise, with the
    Fyers name from the table, and never an `-EQ` symbol; it converts back to a table ticker."""
    fyers = symbol_master.to_fyers(index)
    exchange = "BSE" if index == "SENSEX" else "NSE"
    assert fyers == f"{exchange}:{INDEX_SYMBOLS[index]}-INDEX"
    assert not fyers.endswith("-EQ")
    assert symbol_master.to_db(fyers) in INDEX_SYMBOLS
    assert symbol_master.to_fyers(fyers) == fyers


@given(alias=st.sampled_from(sorted(SYMBOL_ALIASES)), pad=st.sampled_from(["", " ", "  "]))
def test_aliases_resolve_to_one_ticker(alias: str, pad: str) -> None:
    """Every alias or renamed ticker (`LTIM`, `NIFTY BANK`) resolves, in any case and padding, to
    its current ticker, and that ticker is stable (no alias chains)."""
    target = SYMBOL_ALIASES[alias]
    assert SYMBOL_ALIASES["LTIM"] == "LTM"  # the 2025 rename of LTIMindtree
    for text in (alias, alias.lower()):
        assert symbol_master.to_db(f"{pad}{text}{pad}") == target
    assert symbol_master.to_db(target) == target
    assert target not in SYMBOL_ALIASES
    assert symbol_master.to_fyers(f"{pad}{alias}{pad}") == symbol_master.to_fyers(target)


@given(text=anything, other=anything)
def test_conversion_is_pure(text: str, other: str) -> None:
    """The answer for a symbol never depends on other calls or on which instance is asked: no state
    to carry between conversions."""
    fresh = SymbolMaster()
    expected = fresh.to_db(text)
    symbol_master.to_db(other)
    symbol_master.is_db_format(other)
    assert symbol_master.to_db(text) == expected == SymbolMaster().to_db(text)

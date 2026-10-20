"""Index membership lives in IndexUniverseLoader; SymbolMaster only converts formats."""

import subprocess
import sys
from pathlib import Path

import pytest

from app.services.index_universe_loader import IndexUniverseLoader
from app.services.symbol_master import symbol_master

BACKEND_DIR = Path(__file__).resolve().parents[1]

NIFTY50_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "State Bank of India,Financial Services,SBIN,EQ,INE062A01020\n"
    "Mahindra & Mahindra Ltd.,Automobile and Auto Components,M&M,EQ,INE101A01026\n"
)
NIFTYBANK_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "State Bank of India,Financial Services,SBIN,EQ,INE062A01020\n"
    "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
)


@pytest.fixture
def loader(tmp_path: Path) -> IndexUniverseLoader:
    (tmp_path / "ind_nifty50list.csv").write_text(NIFTY50_CSV, encoding="utf-8")
    (tmp_path / "ind_niftybanklist.csv").write_text(NIFTYBANK_CSV, encoding="utf-8")
    return IndexUniverseLoader(data_path=tmp_path)


def test_get_index_symbols_returns_constituents_in_file_order(
    loader: IndexUniverseLoader,
) -> None:
    assert loader.get_index_symbols("NIFTY50") == ["SBIN", "M&M"]
    assert loader.get_index_symbols("NIFTYBANK") == ["SBIN", "HDFCBANK"]


def test_get_index_symbols_for_an_unknown_index_is_empty(loader: IndexUniverseLoader) -> None:
    assert loader.get_index_symbols("NIFTYIT") == []


@pytest.mark.parametrize("symbol", ["SBIN", "NSE:SBIN-EQ", "sbin"])
def test_is_symbol_in_index_accepts_any_symbol_format(
    loader: IndexUniverseLoader, symbol: str
) -> None:
    assert loader.is_symbol_in_index(symbol, "NIFTY50") is True
    assert loader.is_symbol_in_index(symbol, "NIFTYBANK") is True


def test_is_symbol_in_index_is_false_for_a_non_member(loader: IndexUniverseLoader) -> None:
    assert loader.is_symbol_in_index("NSE:HDFCBANK-EQ", "NIFTY50") is False
    assert loader.is_symbol_in_index("SBIN", "NIFTYIT") is False


def test_get_symbol_indices_and_constituent_from_fyers_symbol(
    loader: IndexUniverseLoader,
) -> None:
    assert sorted(loader.get_symbol_indices("NSE:SBIN-EQ")) == ["NIFTY50", "NIFTYBANK"]

    constituent = loader.get_constituent("NSE:M&M-EQ", "NIFTY50")
    assert constituent is not None
    assert constituent.company_name == "Mahindra & Mahindra Ltd."
    assert constituent.industry == "Automobile and Auto Components"
    assert constituent.isin == "INE101A01026"


@pytest.mark.parametrize("symbol", ["LTM", "LTIM", "NSE:LTM-EQ", "NSE:LTIM-EQ"])
def test_legacy_csv_ticker_is_stored_under_its_canonical_symbol(
    tmp_path: Path, symbol: str
) -> None:
    (tmp_path / "ind_niftyitlist.csv").write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "LTIMindtree Ltd.,Information Technology,LTIM,EQ,INE214T01019\n"
        "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n",
        encoding="utf-8",
    )
    loader = IndexUniverseLoader(data_path=tmp_path)

    assert loader.get_index_symbols("NIFTYIT") == ["LTM", "INFY"]
    assert loader.is_symbol_in_index(symbol, "NIFTYIT") is True
    assert loader.get_symbol_indices(symbol) == ["NIFTYIT"]
    constituent = loader.get_constituent(symbol, "NIFTYIT")
    assert constituent is not None
    assert (constituent.symbol, constituent.company_name) == ("LTM", "LTIMindtree Ltd.")


def test_symbol_master_no_longer_exposes_index_membership() -> None:
    for removed in ("get_index_symbols", "is_index_constituent", "to_fyers_list"):
        assert not hasattr(symbol_master, removed), removed


def test_symbol_master_get_info_keeps_format_metadata() -> None:
    nifty = symbol_master.get_info("NSE:NIFTY50-INDEX")
    assert (nifty.ticker, nifty.series, nifty.fyers_format, nifty.lot_size) == (
        "NIFTY50",
        "INDEX",
        "NSE:NIFTY50-INDEX",
        50,
    )

    sbin = symbol_master.get_info("NSE:SBIN-EQ")
    assert (sbin.ticker, sbin.series, sbin.fyers_format, sbin.lot_size) == (
        "SBIN",
        "EQ",
        "NSE:SBIN-EQ",
        1,
    )


_IMPORT_ORDER_SCRIPT = """
import importlib
import sys

for name in sys.argv[1:]:
    importlib.import_module(name)

from app.services.index_universe_loader import index_universe_loader
from app.services.symbol_master import symbol_master

assert index_universe_loader is not None
assert symbol_master.to_fyers("SBIN") == "NSE:SBIN-EQ"
print("ok")
"""

SYMBOL_MASTER = "app.services.symbol_master"
LOADER = "app.services.index_universe_loader"


def _run_in_fresh_interpreter(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Fixed argv: this test's own interpreter and a script defined in this file.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("order", [(SYMBOL_MASTER, LOADER), (LOADER, SYMBOL_MASTER)])
def test_both_symbol_modules_import_in_either_order_in_a_fresh_interpreter(
    order: tuple[str, str],
) -> None:
    result = _run_in_fresh_interpreter(_IMPORT_ORDER_SCRIPT, *order)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_symbol_master_does_not_import_the_universe_layer() -> None:
    script = (
        "import sys\n"
        "from app.services.symbol_master import symbol_master\n"
        "symbol_master.get_info('NSE:SBIN-EQ')\n"
        "symbol_master.to_provider('SBIN')\n"
        f"print({LOADER!r} in sys.modules)\n"
    )
    result = _run_in_fresh_interpreter(script)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"

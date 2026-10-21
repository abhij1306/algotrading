"""CONF check HAT-OVR-040: HAT owns one fake broker and one risk authority; it never adds a
parallel version."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]

# name -> the one file of HAT that may define a class of that name (relative to backend/)
AUTHORITIES = {
    "FakeBroker": "tests/hat/fakes/broker.py",
}
# name -> the one function HAT's risk authority is (relative to backend/)
FUNCTION_AUTHORITIES = {
    "evaluate_signal": "app/hat/risk/service.py",
}
# Legacy classes that still exist beside HAT's: name -> the one file that may define them. The
# legacy suite keeps its own `FakeBroker` until K4/K5 delete it, and the legacy `RiskManager` and
# `LiveMarketService` stay until K4/K5 (K3 for the live service). Each entry goes with its file;
# the list only shrinks.
LEGACY_COPIES = {
    "FakeBroker": "tests/fakes/broker.py",
    "RiskManager": "app/services/risk_manager.py",
    "LiveMarketService": "app/services/live_market_service.py",
}


def _definitions(kinds: tuple[type, ...]) -> list[tuple[str, str]]:
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("venv/", ".venv/")) or "site-packages" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        found.extend((node.name, rel) for node in ast.walk(tree) if isinstance(node, kinds))
    return found


def test_no_duplicate_fakes_or_authorities() -> None:
    definitions = _definitions((ast.ClassDef,))
    for name, home in AUTHORITIES.items():
        where = sorted(rel for cls, rel in definitions if cls == name)
        expected = sorted({home, LEGACY_COPIES[name]} if name in LEGACY_COPIES else {home})
        assert where == expected, (name, where)
    for name, legacy_home in LEGACY_COPIES.items():
        if name not in AUTHORITIES:
            where = sorted(rel for cls, rel in definitions if cls == name)
            assert where == [legacy_home], (name, where)
    for name, home in FUNCTION_AUTHORITIES.items():
        where = sorted(
            rel for fn, rel in _definitions((ast.FunctionDef, ast.AsyncFunctionDef)) if fn == name
        )
        assert where == [home], (name, where)
    # No other class whose name says it is a risk authority or the live-tick service. (Subclasses of
    # the shared fake, such as a crashing variant, extend it in place and are allowed.)
    lookalikes = sorted(
        (cls, rel)
        for cls, rel in definitions
        if cls not in AUTHORITIES
        and cls not in LEGACY_COPIES
        and (cls.endswith("RiskManager") or cls.endswith("LiveMarketService"))
    )
    assert lookalikes == [], lookalikes

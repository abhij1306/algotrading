"""CONF check: one fake broker and one risk authority; no parallel version."""

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
        assert where == [home], (name, where)
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
        and (cls.endswith("RiskManager") or cls.endswith("LiveMarketService"))
    )
    assert lookalikes == [], lookalikes

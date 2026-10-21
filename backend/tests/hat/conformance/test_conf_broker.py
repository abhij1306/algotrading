"""CONF checks of the broker interface, the Fyers adapter and its secrets (HAT-BRK-080, -091,
HAT-DEC-I7, HAT-DEC-I8, HAT-DEC-BD2, HAT-OPS-051)."""

import inspect
import re
from pathlib import Path

from hat_conf_model import hat_tables
from hat_conf_repo import APP_ROOT, build_graph, reaches
from hat_conf_spec import SPEC_DIR

from app.hat.execution import accounts
from app.hat.execution.broker import HatBroker
from app.hat.execution.fyers_adapter import FYERS_CAPABILITIES, FyersHatBroker

HAT_ROOT = APP_ROOT / "hat"
BROKER_FORBIDDEN = ("app.brokers", "fyers_apiv3", "app.hat.execution.fyers_adapter")


def _spec_methods() -> set[str]:
    """The method names of the §8 table in 07-execution-and-brokers.md."""
    text = (SPEC_DIR / "07-execution-and-brokers.md").read_text(encoding="utf-8")
    section = text[text.index("## §8 Broker interface") : text.index("#### HAT-BRK-080")]
    methods = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            first_cell = line.split("|")[1]
            methods.update(re.findall(r"`(\w+)(?:\([^`]*\))?`", first_cell))
    return methods


def _protocol_methods() -> set[str]:
    # typing.get_protocol_members needs Python 3.13; the project supports 3.11 and up.
    return {
        name
        for name, member in vars(HatBroker).items()
        if not name.startswith("_") and callable(member)
    }


def test_brk__interface_methods() -> None:
    """HAT-BRK-080: the protocol's method set equals the §8 table."""
    expected = _spec_methods()
    assert len(expected) == 17, sorted(expected)
    assert _protocol_methods() == expected


def test_broker_interface_methods() -> None:
    """HAT-DEC-I7: the protocol is exactly §8 and the Fyers adapter implements every method."""
    assert _protocol_methods() == _spec_methods()
    for name in _protocol_methods():
        assert callable(getattr(FyersHatBroker, name, None)), name
    for name in _protocol_methods():
        proto = inspect.signature(getattr(HatBroker, name))
        impl = inspect.signature(getattr(FyersHatBroker, name))
        assert list(proto.parameters) == list(impl.parameters), name
    assert isinstance(FyersHatBroker(broker=object()), HatBroker)  # type: ignore[arg-type]


def _violations(graph: dict[str, set[str]], prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    found = {}
    for module in graph:
        if any(module == p or module.startswith(p + ".") for p in prefixes):
            chain = reaches(graph, module, BROKER_FORBIDDEN)
            if not chain:
                chain = next(
                    ([module, target] for target in graph[module] if "openalgo" in target.lower()),
                    [],
                )
            if chain:
                found[module] = chain
    return found


def test_brk__import_boundary() -> None:
    """HAT-BRK-080: engine, risk, data and the router depend on `execution.broker` only: no import
    of `app.brokers`, `fyers_apiv3`, an OpenAlgo package or the Fyers adapter, directly or
    transitively; the interface module itself imports none of them either."""
    graph = build_graph()
    prefixes = (
        "app.hat.engine",
        "app.hat.risk",
        "app.hat.data",
        "app.hat.execution.router",
        "app.hat.execution.broker",
    )
    assert _violations(graph, prefixes) == {}
    # the checker can fail: a synthetic router that imports the adapter and one that imports the SDK
    bad = {
        "app.hat.execution.router": {"app.hat.execution.helper"},
        "app.hat.execution.helper": {"app.brokers.plugins.fyers"},
        "app.hat.engine.x": {"fyers_apiv3.fyersModel"},
        "app.hat.risk.y": {"openalgo_client"},
    }
    assert set(_violations(bad, prefixes)) == {
        "app.hat.execution.router",
        "app.hat.engine.x",
        "app.hat.risk.y",
    }


def test_brk__fyers_capabilities() -> None:
    """HAT-BRK-091: in the MVP build the adapter reports tag length 20, a static IP requirement
    and no GTT for delivery (until the owner verifies it on a live account)."""
    capabilities = FyersHatBroker(broker=object()).capabilities()  # type: ignore[arg-type]
    assert capabilities == FYERS_CAPABILITIES
    assert capabilities.tag_max_len == 20
    assert capabilities.static_ip_required is True
    assert capabilities.gtt_delivery is False
    assert capabilities.products == ("CNC",)
    assert capabilities.to_json()["gtt_delivery"] is False


# The only HAT modules that may import the Fyers SDK (HAT-DEC-BD2), and the one that may call its
# GTT endpoints. Every other module reaches Fyers through them.
SDK_IMPORTERS = {"execution/fyers_session.py", "execution/fyers_auth.py"}
GTT_CALLERS = {"execution/fyers_broker.py"}
SDK_GTT_CALLS = ("place_gtt_order", "modify_gtt_order", "cancel_gtt_order", "gtt_orderbook")


def _sdk_offenders(files: dict[str, str]) -> list[str]:
    """Where `files` (path under app/hat -> text) reach the Fyers SDK outside the modules
    allowed to."""
    offenders = []
    for rel, text in sorted(files.items()):
        if rel not in SDK_IMPORTERS and re.search(
            r"^\s*(import|from)\s+fyers_apiv3", text, re.MULTILINE
        ):
            offenders.append(f"{rel}: imports the SDK")
        if rel not in GTT_CALLERS:
            offenders.extend(f"{rel}: {call}" for call in SDK_GTT_CALLS if call in text)
        if re.search(r"^\s*(import|from)\s+app\.brokers", text, re.MULTILINE):
            offenders.append(f"{rel}: imports app.brokers")
    return offenders


def test_fyers_adapter_wraps_existing() -> None:
    """HAT-DEC-BD2: the adapter reaches the Fyers SDK only through FyersBroker, FyersSession and
    FyersAuthService (K2: all in `app/hat/execution`); the SDK is imported only by the session and
    the login service, its GTT calls are made only by the broker, and nothing under app/hat
    imports the legacy `app.brokers` (the checker is shown to catch offenders)."""
    adapter = (HAT_ROOT / "execution" / "fyers_adapter.py").read_text(encoding="utf-8")
    for name in ("FyersBroker", "FyersSession", "FyersAuthService"):
        assert re.search(rf"^\s+{name},?$|import .*{name}", adapter, re.MULTILINE), name
    files = {
        path.relative_to(HAT_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in HAT_ROOT.rglob("*.py")
    }
    assert _sdk_offenders(files) == []
    assert "place_gtt_order" in files["execution/fyers_broker.py"]
    assert "import fyersModel" in files["execution/fyers_session.py"]
    # the checker can fail
    assert _sdk_offenders({"execution/gtt.py": "import fyers_apiv3\n"}) == [
        "execution/gtt.py: imports the SDK"
    ]
    assert _sdk_offenders({"data/x.py": "client.place_gtt_order(data)\n"}) == [
        "data/x.py: place_gtt_order"
    ]
    assert _sdk_offenders({"data/x.py": "from app.brokers.plugins import fyers\n"}) == [
        "data/x.py: imports app.brokers"
    ]


FYERS_SECRET_WORDS = re.compile(
    r"[\"']FYERS_(APP_ID|SECRET_KEY|CLIENT_ID|TOKEN)|fyers_token|access_token|secret_key"
    r"|client_secret|app_secret|os\.environ|getenv",
    re.IGNORECASE,
)
# Secret-shaped column names that are not secrets: the approval token's hash (HAT-SCH-056) and
# the exchange's instrument token.
NOT_SECRETS = {("hat_approval", "token_hash"), ("hat_broker_symbol", "broker_token")}


# The only modules that read a Fyers credential, the token file or an environment variable (K2: the
# token store, the login service and the session, all in `app/hat/execution`).
SECRET_MODULES = {
    "execution/fyers_auth.py",
    "execution/fyers_tokens.py",
    "execution/fyers_session.py",
}


def _secret_offenders(files: dict[str, str]) -> list[str]:
    offenders = []
    for rel, text in sorted(files.items()):
        if rel in SECRET_MODULES:
            continue
        if rel == "settings.py":  # the HAT settings read HAT_* variables only
            assert "FYERS" not in text.upper().replace("FYERS_ADAPTER", ""), rel
            continue
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.match(r"\s*(from|import)\s", code):
                continue  # importing the token-store module is not reading the token
            if FYERS_SECRET_WORDS.search(code) and "HAT_" not in code:
                offenders.append(f"{rel}:{number}: {line.strip()}")
    return offenders


def test_ops__single_fyers_secret_store() -> None:
    """HAT-OPS-051: only the token store, the login service and the session read a Fyers
    credential, the token file or an environment variable (K2 moved them into app/hat/execution;
    the legacy modules re-export them), nothing else under app/hat does, and no HAT table has a
    column that holds a broker secret."""
    files = {
        path.relative_to(HAT_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in HAT_ROOT.rglob("*.py")
    }
    assert _secret_offenders(files) == []
    for rel in SECRET_MODULES:
        assert rel in files, rel
    # the checker can fail
    assert _secret_offenders({"execution/gtt.py": "_X = os.environ\n"}) == [
        "execution/gtt.py:1: _X = os.environ"
    ]
    assert _secret_offenders({"api/x.py": "token = data['access_token']\n"})

    for name, table in hat_tables().items():
        for column in table.columns:
            if re.search(r"token|secret|password|api_key|credential", column.name, re.I):
                assert (name, column.name) in NOT_SECRETS, (name, column.name)
    broker_account = hat_tables()["hat_broker_account"]
    assert {c.name for c in broker_account.columns} == {
        "id",
        "user_id",
        "broker",
        "account_ref",
        "openalgo_ref",
        "capabilities",
        "status",
    }


def test_ops__one_login_one_token_store() -> None:
    """HAT-OPS-051, HAT-DEC-BD2: one Fyers login and one token store. The login service, the
    session and the legacy store re-export all use HAT's `token_store`; the legacy client, store and
    broker inherit from HAT's classes (no second implementation); the legacy login router is gone;
    the HAT router has exactly the four login routes."""
    from app.hat.api import fyers_auth as routes
    from app.hat.execution import fyers_auth, fyers_session, fyers_tokens

    def source(relative: str) -> str:
        return (APP_ROOT / relative).read_text(encoding="utf-8")

    assert fyers_auth.fyers_auth.store is fyers_tokens.token_store
    assert fyers_session.FyersSession.__init__.__kwdefaults__ is not None
    assert "token_store" in inspect.getsource(fyers_session.FyersSession.__init__)
    # The legacy classes are subclasses or re-exports of HAT's (read as text: HAT tests never
    # import legacy modules).
    assert re.search(
        r"^class FyersClient\(FyersSession\)", source("services/fyers_client.py"), re.M
    )
    assert re.search(
        r"^class FyersBroker\(HatFyersBroker", source("brokers/plugins/fyers.py"), re.M
    )
    assert re.search(
        r"^class SymbolMaster\(HatSymbolMaster\)", source("services/symbol_master.py"), re.M
    )
    legacy_store = source("services/fyers/token_store.py")
    assert "from app.hat.execution.fyers_tokens import" in legacy_store
    assert "class TokenStore" not in legacy_store
    assert not (APP_ROOT / "routers" / "auth.py").exists()
    found = {(m, r.path) for r in routes.router.routes for m in getattr(r, "methods", set())}
    base = "/api/hat/auth/fyers"
    assert found == {
        ("GET", f"{base}/status"),
        ("GET", f"{base}/url"),
        ("POST", f"{base}/token"),
        ("POST", f"{base}/disconnect"),
    }


def test_mvp_seed_scope() -> None:
    """HAT-DEC-I8 (the broker half of the seed scope): every adapter registered in the MVP is the
    Fyers adapter, and onboarding accepts no other broker. PR 8 extends this check to the seeded
    strategies, markets and `HAT_USER_ID` ownership (HAT-OVR-002)."""
    assert set(accounts.ADAPTER_FACTORIES) == {"fyers"}
    assert Path(accounts.__file__).exists()

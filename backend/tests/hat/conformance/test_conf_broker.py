"""CONF checks of the broker interface, the Fyers adapter and its secrets (HAT-BRK-080, -091,
HAT-DEC-I7, HAT-DEC-I8, HAT-DEC-BD2, HAT-OPS-051)."""

import inspect
import re
import typing
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
    return set(typing.get_protocol_members(HatBroker))


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


def test_fyers_adapter_wraps_existing() -> None:
    """HAT-DEC-BD2: the adapter reaches the Fyers SDK only through FyersBroker and FyersClient
    (and FyersBroker's GTT calls); nothing under app/hat imports the SDK or builds a client."""
    adapter = (HAT_ROOT / "execution" / "fyers_adapter.py").read_text(encoding="utf-8")
    for name in ("FyersBroker", "FyersClient", "FyersAuthService"):
        assert re.search(rf"^\s+{name},?$|import .*{name}", adapter, re.MULTILINE), name
    for path in HAT_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+fyers_apiv3", text, re.MULTILINE), path
        assert "FyersModel" not in text, path
        for sdk_call in (
            "place_gtt_order",
            "modify_gtt_order",
            "cancel_gtt_order",
            "gtt_orderbook",
        ):
            assert sdk_call not in text, (path, sdk_call)
    plugin = (APP_ROOT / "brokers" / "plugins" / "fyers.py").read_text(encoding="utf-8")
    for sdk_call in ("place_gtt_order", "modify_gtt_order", "cancel_gtt_order", "gtt_orderbook"):
        assert sdk_call in plugin, sdk_call


FYERS_SECRET_WORDS = re.compile(
    r"[\"']FYERS_(?!CAPABILITIES)|fyers_token|access_token|secret_key|client_secret|app_secret|os\.environ|getenv",
    re.IGNORECASE,
)
# Secret-shaped column names that are not secrets: the approval token's hash (HAT-SCH-056) and
# the exchange's instrument token.
NOT_SECRETS = {("hat_approval", "token_hash"), ("hat_broker_symbol", "broker_token")}


def test_ops__single_fyers_secret_store() -> None:
    """HAT-OPS-051: nothing in app/hat reads a Fyers credential, token file or environment
    variable of its own (the adapter goes through the existing auth service and client), and no HAT
    table has a column that holds a broker secret."""
    offenders = []
    for path in HAT_ROOT.rglob("*.py"):
        if path.name == "settings.py":  # the HAT settings read HAT_* variables only
            text = path.read_text(encoding="utf-8")
            assert "FYERS" not in text.upper().replace("FYERS_ADAPTER", ""), path
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if FYERS_SECRET_WORDS.search(code) and "HAT_" not in code:
                offenders.append(f"{path.relative_to(HAT_ROOT)}:{number}: {line.strip()}")
    assert offenders == []

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


def test_mvp_seed_scope() -> None:
    """HAT-DEC-I8 (the broker half of the seed scope): every adapter registered in the MVP is the
    Fyers adapter, and onboarding accepts no other broker. PR 8 extends this check to the seeded
    strategies, markets and `HAT_USER_ID` ownership (HAT-OVR-002)."""
    assert set(accounts.ADAPTER_FACTORIES) == {"fyers"}
    assert Path(accounts.__file__).exists()

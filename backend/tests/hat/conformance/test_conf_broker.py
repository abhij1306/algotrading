"""CONF checks of the broker interface, the Fyers adapter and its secrets, and the default
execution mode."""

import ast
import inspect
import json
import re
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from hat_conf_model import hat_tables
from hat_conf_repo import APP_ROOT, build_graph, reaches

from app.hat.engine import entries
from app.hat.engine.strategy.config import StrategyConfig
from app.hat.engine.strategy.versions import VersionError, create_bot
from app.hat.execution import accounts, modes
from app.hat.execution.broker import GttRequest, HatBroker
from app.hat.execution.fyers_adapter import FYERS_CAPABILITIES, FyersHatBroker
from app.hat.execution.fyers_payloads import gtt_payload, parse_gtt_row, parse_order_book
from app.hat.execution.tags import encode_tag
from app.hat.models import HatBot
from app.hat.risk.modes import Mode

HAT_ROOT = APP_ROOT / "hat"
DATA_SOCKET_MODULE = "app.hat.data.provider.fyers_websocket"
BROKER_FORBIDDEN = ("app.brokers", "fyers_apiv3", "app.hat.execution.fyers_adapter")


def _protocol_methods() -> set[str]:
    # typing.get_protocol_members needs Python 3.13; the project supports 3.11 and up.
    return {
        name
        for name, member in vars(HatBroker).items()
        if not name.startswith("_") and callable(member)
    }


def test_broker_interface_methods() -> None:
    """The Fyers adapter implements every method of the protocol with its signature."""
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
    """Engine, risk, data and the router depend on `execution.broker` only: no import
    of `app.brokers`, `fyers_apiv3`, an OpenAlgo package or the Fyers adapter, directly or
    transitively; the interface module itself imports none of them either."""
    graph = build_graph()
    # The one exception: HAT's data socket module imports the SDK socket and the
    # Fyers session for its token (and the session's history request for the minute-history
    # download); `test_imports__no_second_websocket` confines the socket to it.
    graph[DATA_SOCKET_MODULE] = set()
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
    # the exception is that one module and nothing else
    assert set(_violations({"app.hat.data.other": {"fyers_apiv3.FyersWebsocket"}}, prefixes)) == {
        "app.hat.data.other"
    }


def test_brk__fyers_capabilities() -> None:
    """The adapter reports tag length 20, a static IP requirement and GTT for
    delivery (verified on the live account on 2026-10-22, `fyers_live_writes.json`)."""
    capabilities = FyersHatBroker(broker=object()).capabilities()  # type: ignore[arg-type]
    assert capabilities == FYERS_CAPABILITIES
    assert capabilities.tag_max_len == 20
    assert capabilities.static_ip_required is True
    assert capabilities.gtt_delivery is True
    assert capabilities.products == ("CNC",)
    assert capabilities.to_json()["gtt_delivery"] is True


# The only HAT modules that may import the Fyers SDK, each with the one SDK module it may import
# and the one that may call its GTT endpoints. Every other module reaches Fyers
# through them. The SDK's HTTP client (`fyersModel`: orders, GTTs, history, quotes and the login
# exchange) is imported only by `fyers_ipv4`, which installs the IPv4-only hook first
# the session and the login service take it from `fyers_ipv4.sdk()`.
SDK_IMPORTERS = {
    "execution/fyers_ipv4.py": "fyersModel",
    "data/provider/fyers_websocket.py": "FyersWebsocket",
}
GTT_CALLERS = {"execution/fyers_broker.py"}
SDK_GTT_CALLS = ("place_gtt_order", "modify_gtt_order", "cancel_gtt_order", "gtt_orderbook")


def _sdk_offenders(files: dict[str, str]) -> list[str]:
    """Where `files` (path under app/hat -> text) reach the Fyers SDK outside the modules
    allowed to."""
    offenders = []
    for rel, text in sorted(files.items()):
        imports = re.findall(r"^\s*(?:import|from)\s+fyers_apiv3\b.*$", text, re.MULTILINE)
        allowed = SDK_IMPORTERS.get(rel)
        if any(allowed is None or not re.search(rf"\b{allowed}\b", line) for line in imports):
            offenders.append(f"{rel}: imports the SDK")
        if rel not in GTT_CALLERS:
            offenders.extend(f"{rel}: {call}" for call in SDK_GTT_CALLS if call in text)
        if re.search(r"^\s*(import|from)\s+app\.brokers", text, re.MULTILINE):
            offenders.append(f"{rel}: imports app.brokers")
    return offenders


def test_fyers_adapter_wraps_existing() -> None:
    """The adapter reaches the Fyers SDK only through FyersBroker,
    FyersSession and FyersAuthService (K2: all in `app/hat/execution`); the SDK's HTTP client is
    imported only by `fyers_ipv4`, after it installs the IPv4 hook, and taken from there by the
    session and the login service; the data socket module imports only the websocket; its
    GTT calls are made only by the broker, and nothing under app/hat imports the legacy
    `app.brokers` (the checker is shown to catch offenders)."""
    adapter = (HAT_ROOT / "execution" / "fyers_adapter.py").read_text(encoding="utf-8")
    for name in ("FyersBroker", "FyersSession", "FyersAuthService"):
        assert re.search(rf"^\s+{name},?$|import .*{name}", adapter, re.MULTILINE), name
    files = {
        path.relative_to(HAT_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in HAT_ROOT.rglob("*.py")
    }
    assert _sdk_offenders(files) == []
    assert "place_gtt_order" in files["execution/fyers_broker.py"]
    ipv4 = files["execution/fyers_ipv4.py"]
    sdk = ipv4[ipv4.index("def sdk(") :]
    assert sdk.index("install()") < sdk.index("from fyers_apiv3 import fyersModel")
    for rel in ("execution/fyers_session.py", "execution/fyers_auth.py"):
        assert "from .fyers_ipv4 import sdk" in files[rel], rel
    # the checker can fail
    assert _sdk_offenders({"execution/gtt.py": "import fyers_apiv3\n"}) == [
        "execution/gtt.py: imports the SDK"
    ]
    assert _sdk_offenders(
        {"execution/fyers_session.py": "from fyers_apiv3 import fyersModel\n"}
    ) == ["execution/fyers_session.py: imports the SDK"]
    assert _sdk_offenders(
        {"data/provider/fyers_websocket.py": "    from fyers_apiv3 import fyersModel\n"}
    ) == ["data/provider/fyers_websocket.py: imports the SDK"]
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
# Secret-shaped column names that are not secrets: the approval token's hash and
# the exchange's instrument token.
NOT_SECRETS = {("hat_approval", "token_hash"), ("hat_broker_symbol", "broker_token")}


# The only modules that read a Fyers credential, the token file or an environment variable (K2: the
# token store, the login service and the session, all in `app/hat/execution`; K3: the data socket
# module, which reads the session's token).
SECRET_MODULES = {
    "execution/fyers_auth.py",
    "execution/fyers_tokens.py",
    "execution/fyers_session.py",
    "data/provider/fyers_websocket.py",  # builds the data socket from the session's token
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
    """Only the token store, the login service and the session read a Fyers
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
        "ddpi_confirmed",
        "ddpi_confirmed_at",
        "ddpi_note",
    }


def test_ops__one_login_one_token_store() -> None:
    """One Fyers login and one token store. The login service and the
    session use HAT's `token_store`; the legacy client, symbol master, login hooks, token-store
    re-export, broker package and login router are gone (no second implementation); the HAT
    router has exactly the four login routes."""
    from app.hat.api import fyers_auth as routes
    from app.hat.execution import fyers_auth, fyers_session, fyers_tokens

    assert fyers_auth.fyers_auth.store is fyers_tokens.token_store
    assert fyers_session.FyersSession.__init__.__kwdefaults__ is not None
    assert "token_store" in inspect.getsource(fyers_session.FyersSession.__init__)
    # No second implementation: the legacy client, symbol master and login hooks are deleted
    # .
    assert not (APP_ROOT / "services" / "fyers_client.py").exists()
    assert not (APP_ROOT / "services" / "symbol_master.py").exists()
    assert not (APP_ROOT / "services" / "fyers").exists()
    assert not (APP_ROOT / "brokers").exists()
    assert not (APP_ROOT / "routers" / "auth.py").exists()
    found = {(m, r.path) for r in routes.router.routes for m in getattr(r, "methods", set())}
    base = "/api/hat/auth/fyers"
    assert found == {
        ("GET", f"{base}/status"),
        ("GET", f"{base}/url"),
        ("POST", f"{base}/token"),
        ("POST", f"{base}/disconnect"),
    }


def _seed_scope_strategies_and_owner() -> None:
    """The seeds are exactly S1 and S2 on NSE, and every row they create is owned by one user."""
    import uuid

    from hat.hat_support import memory_engine
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.hat.engine.strategy.versions import SEED_NAMES, seed_default_bots
    from app.hat.models import HatBot, HatMarket, HatStrategy
    from app.hat.schema import seed_hat_reference

    engine = memory_engine()
    seed_hat_reference(engine)
    owner = uuid.uuid4()
    with Session(engine) as session:
        seed_default_bots(session, user_id=owner, broker_account_id=uuid.uuid4())
        strategies = session.scalars(select(HatStrategy)).all()
        assert (
            {s.name for s in strategies}
            == set(SEED_NAMES)
            == {
                "stage2-contraction-breakout",
                "momentum-breakout-ep",
            }
        )
        assert [m.code for m in session.scalars(select(HatMarket)).all()] == ["NSE"]
        for model in (HatStrategy, HatBot):
            owners = {row.user_id for row in session.scalars(select(model)).all()}
            assert owners == {owner}, model


def test_mvp_seed_scope() -> None:
    """(the broker half of the seed scope): every adapter registered in the MVP is the
    Fyers adapter, and onboarding accepts no other broker. PR 8 extends this check to the seeded
    strategies, markets and `HAT_USER_ID` ownership."""
    assert set(accounts.ADAPTER_FACTORIES) == {"fyers"}
    _seed_scope_strategies_and_owner()
    assert Path(accounts.__file__).exists()


SEEDS_DIR = HAT_ROOT / "engine" / "strategy" / "seeds"


class _NoSession:
    """Takes whatever a bot creation writes, so a creation that is not refused shows up as a
    missing refusal rather than a database error."""

    def add(self, row: object) -> None:
        pass

    def flush(self) -> None:
        pass


def test_exec__default_mode_confirm() -> None:
    """A bot asks for confirm unless the owner changes it,
    and runs shadow until every gate passes. The bot row defaults to confirm in the model and in
    the database, a new bot cannot be created in another mode, the entry path routes shadow
    unless told otherwise, a mode outside the three reads as shadow, and confirm with any gate
    missing reads as shadow. The strategy config's `execution_mode` is not read for a bot's mode;
    its seeds still say shadow (note in 07 )."""
    seeds = sorted(SEEDS_DIR.glob("*.yaml"))
    assert seeds
    for path in seeds:
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["execution_mode"] == "shadow", path
    assert StrategyConfig.model_fields["execution_mode"].default == "shadow"
    column = HatBot.__table__.c.execution_mode
    assert column.default is not None
    assert column.default.arg == "confirm"
    assert column.server_default is not None
    assert column.server_default.arg == "confirm"
    for mode in ("shadow", "auto"):
        no_session, user_id, strategy_id, account_id = (
            _NoSession(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
        )
        with pytest.raises(VersionError):
            create_bot(
                no_session,  # type: ignore[arg-type] - refused before the session is touched
                user_id=user_id,
                strategy_id=strategy_id,
                broker_account_id=account_id,
                execution_mode=mode,
            )
    assert inspect.signature(entries.open_entry).parameters["mode"].default == Mode.SHADOW
    ready = modes.Gates(
        "active", {"gtt_delivery": True}, ddpi_confirmed=True, weights_confirmed=True
    )
    for unknown in ("", "live", "AUTO"):
        assert modes.capability_gated(unknown, ready) == Mode.SHADOW
    for missing in ("ddpi_confirmed", "weights_confirmed"):
        gates = replace(ready, **{missing: False})
        assert modes.capability_gated(Mode.CONFIRM, gates) == Mode.SHADOW


WRITES_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fyers_live_writes.json"
# A PAN (5 letters, 4 digits, a letter), Fyers' "C-" RMS client code, an IPv4 address
_ACCOUNT_IDS = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b|\bC-[A-Z0-9]{5,}|\b\d{1,3}(\.\d{1,3}){3}\b")


def _recorded() -> list[dict]:
    return json.loads(WRITES_FIXTURE.read_text(encoding="utf-8"))["calls"]


def test_brk__recorded_writes() -> None:
    """The Fyers write replies recorded live on 2026-10-22 hold:
    the GTT place payload the adapter builds for the recorded request is the one Fyers accepted
    (with HAT's `orderTag`); the place, modify and cancel replies read as placed and ok; the GTT
    book rows (live keys `tran_side`, `product_type`, `ordertag` "1:GTT<tag>" or "2:GTTUntagged")
    read as resting GTTs with their tag, and cancelled ones are left out; two orders under one
    tag were both accepted with ids and both refused by RMS (MX-F6: Fyers does not enforce tag
    uniqueness), and each reads as a rejection. The fixture carries no account id (masked)."""
    calls = _recorded()
    text = WRITES_FIXTURE.read_text(encoding="utf-8")
    assert [m.group(0) for m in _ACCOUNT_IDS.finditer(text)] == ["XXXXX0000X"] * text.count(
        "XXXXX0000X"
    )
    by = lambda name: [c for c in calls if c["method"] == name]  # noqa: E731
    [untagged, tagged] = by("place_gtt_order")
    assert tagged["args"]["orderTag"] == "HATTEST1"
    assert "orderTag" not in untagged["args"]
    stop_tag = encode_tag(uuid.UUID(int=24), "stop_gtt", 0)
    for call in (untagged, tagged):
        sdk = RecordingSdk(call["reply"])
        leg = call["args"]["orderInfo"]["leg1"]
        request = GttRequest(
            "SBIN",
            leg["qty"],
            Decimal(str(leg["triggerPrice"])),
            Decimal(str(leg["price"])),
            tag=stop_tag,
        )
        symbol = call["args"]["symbol"]
        payload = gtt_payload(request, lambda _symbol, symbol=symbol: symbol)
        # the payload Fyers accepted, with HAT's own tag (the recording used a test tag or none)
        assert payload == {**call["args"], "orderTag": stop_tag}
        reply = broker_over(sdk).place_gtt(payload)
        assert sdk.calls == [("place_gtt_order", payload)]
        assert (reply["status"], reply["order_id"]) == ("SUBMITTED", call["reply"]["id"])
    adapter = FyersHatBroker(broker_over(RecordingSdk()), instruments=lambda: [])
    for name in ("modify_gtt_order", "cancel_gtt_order"):
        assert all(c["reply"]["s"] == "ok" for c in by(name))
    # the GTT book: the tag Fyers shows, put on a HAT tag
    tag = encode_tag(uuid.UUID(int=25), "stop_gtt", 0)
    books = [c["reply"]["orderBook"] for c in by("gtt_orderbook")]
    shown = {row["ordertag"] for book in books for row in book}
    assert shown == {"2:GTTUntagged", "1:GTTHATTEST1"}
    for book in books:
        rows = [{**r, "ordertag": r["ordertag"].replace("HATTEST1", tag)} for r in book]
        read = adapter._read(lambda rows=rows: rows, lambda rs: list(map(parse_gtt_row, rs)))
        for row, gtt in zip(rows, read, strict=True):
            if row["ord_status"] == 1:  # cancelled
                assert gtt is None
                continue
            assert gtt is not None
            assert gtt.side.value == "sell"
            assert gtt.qty == row["qty"]
            assert gtt.tag == (tag if "GTT" + tag in row["ordertag"] else None)
    # MX-F6: one tag, two orders, both accepted with an id and refused by RMS
    [first, second] = by("place_order")
    assert first["args"] == second["args"]
    assert first["args"]["orderTag"] == "HATTEST2"
    assert first["reply"]["id"] != second["reply"]["id"]
    for call in (first, second):
        assert call["reply"]["s"] == "error"
        assert call["reply"]["id"]
    book = by("orderbook")[-1]["reply"]["orderBook"]
    assert sorted(r["id"] for r in book) == sorted(c["reply"]["id"] for c in (first, second))
    orders = parse_order_book(book)
    assert {(o.tag, o.status) for o in orders} == {("HATTEST2", "rejected")}
    assert len(orders) == 2
    assert all(c["reply"]["code"] == -52 for c in by("cancel_order"))


# The broker-account status and broker-name values live once, in the models leaf; every reader
# imports them from there.
ACCOUNT_CONSTANTS = {"ACCOUNT_ACTIVE", "ACCOUNT_FAILED", "ACCOUNT_PENDING", "BROKER_FYERS"}
CONSTANTS_HOME = HAT_ROOT / "models" / "strategy.py"
# the model, and any name for an account a reader uses (`account`, `acct`, `broker_account`...)
ACCOUNT_OWNER = re.compile(r"^HatBrokerAccount$|acc(oun)?t", re.IGNORECASE)


def _account_column(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in ("status", "broker")
        and isinstance(node.value, ast.Name)
        and ACCOUNT_OWNER.search(node.value.id) is not None
    )


def account_literal_violations(source: str) -> list[str]:
    """What breaks the one-copy rule in a module's source: a module-level `ACCOUNT_*` status
    constant or `BROKER_FYERS` of its own, or a broker account's `status` or `broker` compared
    with a string, written out or held in a module-level copy under another name."""
    tree = ast.parse(source)
    copies = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    found = [
        f"defines {target.id}"
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name) and target.id in ACCOUNT_CONSTANTS
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            literal = any(
                (isinstance(s, ast.Constant) and isinstance(s.value, str))
                or (isinstance(s, ast.Name) and s.id in copies)
                for s in sides
            )
            if literal and any(_account_column(s) for s in sides):
                found.append(f"line {node.lineno}: an account column compared with a string")
    return found


def test_brk__account_constants_one_copy() -> None:
    """CONF brk.account_constants_one_copy: no module but
    `models/strategy.py` defines a broker-account status constant or the broker name, and none
    compares an account's status or broker with a string of its own. Harm: a renamed status
    leaves a stale copy behind, and a login, a routing gate, the dashboard or a broker-symbol
    read uses a value no row has any more."""
    found = {
        str(path.relative_to(HAT_ROOT)): bad
        for path in sorted(HAT_ROOT.rglob("*.py"))
        if path != CONSTANTS_HOME
        and (bad := account_literal_violations(path.read_text(encoding="utf-8")))
    }
    assert found == {}
    # the checker can fail: a copy of a constant, a copy under another name, and compared
    # literals on the model and on any account name
    bad = (
        'ACCOUNT_ACTIVE = "active"\n'
        'LIVE = "active"\n'
        "def f(session, account, acct):\n"
        '    session.scalars(select(HatBrokerAccount).where(HatBrokerAccount.status == "active"))\n'
        "    session.scalars(select(HatBrokerAccount).where(HatBrokerAccount.status == LIVE))\n"
        '    return account.broker != "fyers" or acct.status == "active"\n'
    )
    assert len(account_literal_violations(bad)) == 5

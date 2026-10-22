"""CONF checks of the deployment and the runbook (HAT-OPS-040, HAT-OPS-090): the Compose file
brings up only Postgres and the backend (the frontend under a profile) on loopback ports with no
inline secret, and docs/specs/hat/10-ops.md §9 has a procedure for every OPS check and every §4
failure case."""

import re

import yaml
from hat_conf_spec import REPO_ROOT, SPEC_DIR

COMPOSE = REPO_ROOT / "deploy" / "hat" / "docker-compose.yml"
ENV_FILE = "../../.env"
CORE_SERVICES = {"postgres", "backend"}
OPTIONAL_SERVICES = {"frontend"}
LOOPBACK = "127.0.0.1"
POSTGRES_MAJOR = "18"
SECRET_KEY = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|CHAT_ID", re.IGNORECASE)
INTERPOLATED = re.compile(r"^\$\{[A-Z_]+(:?[?-][^}]*)?\}$")
OPS_NAME = re.compile(r"\bOPS `([^`]+)`")
FAILURE_BULLET = re.compile(r"^- \*\*(.+?)\*\* →", re.MULTILINE)
PROCEDURE = re.compile(r"^### (.+)$", re.MULTILINE)
STEP = re.compile(r"^(\d+\.|```)", re.MULTILINE)
CHECK_LOG = "Check log"  # the table of performed checks, not a procedure


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _host_ip(port: object) -> str:
    """The host address of a published port, short (`ip:host:container`) or long syntax."""
    if isinstance(port, dict):
        return str(port.get("host_ip", ""))
    parts = str(port).split(":")
    return parts[0] if len(parts) == 3 else ""


def _environment(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.partition("=")[::2] for item in env)
    return {str(key): str(value) for key, value in env.items()}


def test_compose_services() -> None:
    """HAT-OPS-040: exactly postgres and backend (frontend only under a profile), no other
    service, Postgres 18, every published port on 127.0.0.1, the backend reading the repo .env
    through env_file, no secret written inline, and the backend starting HAT after a healthy
    database.
    Harm: an open port exposes the trading box, or a stray service holds broker credentials."""
    services = _compose()["services"]
    assert set(services) - OPTIONAL_SERVICES == CORE_SERVICES
    for name in CORE_SERVICES:
        assert "profiles" not in services[name], name
    for name in set(services) & OPTIONAL_SERVICES:
        assert services[name].get("profiles"), name
    image = services["postgres"]["image"]
    assert image.split(":")[0] == "postgres"
    assert image.split(":")[1].split(".")[0] == POSTGRES_MAJOR
    for name, service in services.items():
        for port in service.get("ports", []):
            assert _host_ip(port) == LOOPBACK, (name, port)
        for key, value in _environment(service).items():
            if SECRET_KEY.search(key):
                assert INTERPOLATED.match(value), (name, key)
    assert ENV_FILE in services["backend"]["env_file"]
    _check_backend(services["backend"])


def _check_backend(backend: dict) -> None:
    env = _environment(backend)
    assert env["HAT_ENABLED"] == "true"
    assert env["DB_HOST"] == "postgres"
    assert backend["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert backend["healthcheck"]["test"]
    mounts = {str(volume).split(":")[1] for volume in backend.get("volumes", [])}
    assert env["HAT_DATA_ROOT"] in mounts


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def _ops_names() -> set[str]:
    names = set()
    for path in SPEC_DIR.glob("*.md"):
        names.update(OPS_NAME.findall(path.read_text(encoding="utf-8")))
    return names


def _procedures(runbook: str) -> dict[str, str]:
    """`###` heading -> its body, within the runbook section."""
    heads = list(PROCEDURE.finditer(runbook))
    bodies = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(runbook)
        bodies[head.group(1)] = runbook[head.end() : end]
    bodies.pop(CHECK_LOG, None)
    return bodies


def test_runbook_complete() -> None:
    """HAT-OPS-090: 10-ops §9 has a procedure heading naming every `OPS` check used anywhere in
    docs/specs/hat/ and one for every failure bullet of §4, and every procedure has steps.
    Harm: at 09:20 on a bad day the operator has no tested procedure to follow."""
    text = (SPEC_DIR / "10-ops.md").read_text(encoding="utf-8")
    runbook = _section(text, "## §9 Runbook")
    procedures = _procedures(runbook)
    headings = " ".join(procedures)
    names = _ops_names()
    assert names >= {"ops.host_checklist", "ops.deploy_window", "ops.backup_restore_drill"}
    missing = sorted(name for name in names if f"`{name}`" not in headings)
    assert missing == []
    failures = FAILURE_BULLET.findall(_section(text, "## §4 Failure handling"))
    assert len(failures) >= 6
    uncovered = sorted(f for f in failures if f.lower() not in headings.lower())
    assert uncovered == []
    empty = sorted(head for head, body in procedures.items() if not STEP.search(body))
    assert empty == []

"""CONF checks of the verification harness itself (HAT-VER-010, 011, 030..034, 040, 050, 060,
070..072; HAT-SCH-102; HAT-OVR-050)."""

import ast
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import yaml
from hat_conf_skills import (
    PLACEHOLDER,
    REQUIRED_SECTIONS,
    break_ids,
    covered_files,
    digest,
    parse_skill,
    property_ids,
    skill_files,
)
from hat_conf_spec import REPO_ROOT, SPEC_DIR, requirements

TLA_DIR = REPO_ROOT / "verification" / "tla"
HAT_TESTS = REPO_ROOT / "backend" / "tests" / "hat"
METHODS = {"TLA", "PBT", "DB", "CONF", "OPS"}
# Pull requests merged so far; a later PR raises this when it merges.
MERGED_PRS = {1, 2, 3}


def _models() -> list[Path]:
    return sorted(TLA_DIR.glob("*.tla"))


# --- TLA+ toolchain -------------------------------------------------------------------------


def test_tla_toolchain_pinned(tmp_path: Path) -> None:
    """HAT-VER-030: a cfg per model; run.sh pins v1.7.4 by URL and sha256 and refuses a jar
    whose hash differs (exercised offline with a fake curl)."""
    assert _models()
    for model in _models():
        assert model.with_suffix(".cfg").exists(), model.name
    script = (TLA_DIR / "run.sh").read_text(encoding="utf-8")
    assert re.search(r'^TLC_VERSION="v1\.7\.4"$', script, re.MULTILINE)
    assert "releases/download/${TLC_VERSION}/tla2tools.jar" in script
    assert re.search(r'^TLC_SHA256="[0-9a-f]{64}"$', script, re.MULTILINE)
    assert (TLA_DIR / "run.sh").stat().st_mode & stat.S_IXUSR

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        '#!/usr/bin/env bash\nwhile [[ $# -gt 0 ]]; do [[ "$1" == --output ]] && out="$2"; '
        'shift; done\necho "not the real jar" > "$out"\n'
    )
    curl.chmod(0o755)
    cache = tmp_path / "cache"
    (cache / "v1.7.4").mkdir(parents=True)
    (cache / "v1.7.4" / "tla2tools.jar").write_text("a different jar")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TLA_CACHE_DIR": str(cache)}
    bash = shutil.which("bash") or "bash"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [bash, str(TLA_DIR / "run.sh")], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 2
    assert "refusing to run" in result.stderr
    assert not (cache / "v1.7.4" / "tla2tools.jar").exists()  # the mismatching jar is deleted


def _header(model: Path) -> str:
    text = model.read_text(encoding="utf-8")
    start = text.index("MODULE")
    end = re.search(r"^(EXTENDS|CONSTANTS?|VARIABLES?)\b", text, re.MULTILINE).start()
    assert start < end, model.name
    return text[start:end]


def _cfg_constants(cfg: Path) -> list[str]:
    names = []
    in_block = False
    for line in cfg.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("CONSTANTS"):
            in_block = True
            continue
        if in_block:
            match = re.match(r"^(\w+)\s*=", stripped)
            if match and line.startswith((" ", "\t")):
                names.append(match.group(1))
            else:
                in_block = False
    return names


def test_tla_bounds_declared() -> None:
    """HAT-VER-033: Bounds: and Assumptions: in the first comment block; every cfg constant is
    named under Bounds:."""
    for model in _models():
        header = _header(model)
        assert re.search(r"Bounds:", header), model.name
        assert re.search(r"Assumptions:", header), model.name
        bounds = header[header.index("Bounds:") : header.index("Assumptions:")]
        constants = _cfg_constants(model.with_suffix(".cfg"))
        assert constants, model.name
        for cfg in [
            model.with_suffix(".cfg"),
            *sorted((TLA_DIR / "broken").glob(f"{model.stem}__*.cfg")),
        ]:
            for name in _cfg_constants(cfg):
                assert re.search(rf"\b{name}\b", bounds), (cfg.name, name)


def _operators(text: str) -> dict[str, str]:
    """name -> the comment lines immediately above its definition."""
    lines = text.splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(\w+)(?:\([^)]*\))?\s*==", line)
        if match:
            above = []
            cursor = index - 1
            while cursor >= 0 and lines[cursor].startswith("\\*"):
                above.insert(0, lines[cursor])
                cursor -= 1
            found[match.group(1)] = "\n".join(above)
    return found


def _next_actions(text: str) -> list[str]:
    body = re.search(r"^Next ==\n(.*?)\n\n", text, re.DOTALL | re.MULTILINE)
    assert body, "no Next relation"
    lines = text.splitlines()
    actions = set()
    for index, line in enumerate(lines):
        match = re.match(r"^(\w+)(?:\([^)]*\))?\s*==", line)
        if not match:
            continue
        block = []
        for follow in lines[index:]:
            if not follow.strip() and block:
                break
            block.append(follow)
        if "'" in "\n".join(block):  # an action assigns primed variables
            actions.add(match.group(1))
    names = re.findall(r"\b(\w+)\b", body.group(1))
    return sorted({n for n in names if n in actions and n != "Next"})


def _function_exists(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name == name
        for node in ast.walk(tree)
    )


def test_tla_actions_mapped() -> None:
    """HAT-VER-031: every action of Next names `backend/app/hat/...py:function` that exists."""
    for model in _models():
        text = model.read_text(encoding="utf-8")
        actions = _next_actions(text)
        assert len(actions) >= 3, model.name
        operators = _operators(text)
        for action in actions:
            match = re.search(r"\\\* code: (backend/app/hat/[\w/]+\.py):(\w+)", operators[action])
            assert match, f"{model.name}: action {action} has no code comment"
            target = REPO_ROOT / match.group(1)
            assert target.exists(), (action, match.group(1))
            assert _function_exists(target, match.group(2)), (action, match.group(2))


def _cfg_properties(cfg: Path) -> list[str]:
    return re.findall(
        r"^(?:INVARIANT|PROPERTY)\s+(\w+)\s*$", cfg.read_text(encoding="utf-8"), re.MULTILINE
    )


TYPING_ONLY = {
    "TypeOK"
}  # a type-correctness invariant: a promise nobody makes, so no broken variant


def test_tla_broken_variants_fail() -> None:
    """HAT-VER-032 (static half; the job runs them): every property of a cfg has a broken cfg
    that expects it, switches a different BrokenMode on, and keeps the other constants."""
    for model in _models():
        base = model.with_suffix(".cfg")
        properties = set(_cfg_properties(base)) - TYPING_ONLY
        text = model.read_text(encoding="utf-8")
        expected = {}
        for cfg in sorted((TLA_DIR / "broken").glob(f"{model.stem}__*.cfg")):
            body = cfg.read_text(encoding="utf-8")
            match = re.search(r"^\\\* EXPECT: (\w+)\s*$", body, re.MULTILINE)
            assert match, cfg.name
            prop = match.group(1)
            assert prop in _cfg_properties(cfg), cfg.name  # the broken cfg checks what it expects
            mode = re.search(r'BrokenMode = "(\w+)"', body)
            assert mode, cfg.name
            assert mode.group(1) != "none", cfg.name
            assert f'"{mode.group(1)}"' in text  # the model knows this variant
            assert mode.group(1) == cfg.stem.split("__", 1)[1], cfg.name
            expected.setdefault(prop, []).append(cfg.name)
            for name in _cfg_constants(base):
                assert name in _cfg_constants(cfg), (cfg.name, name)
        assert properties <= set(expected), properties - set(expected)
    script = (TLA_DIR / "run.sh").read_text(encoding="utf-8")
    assert "--broken" in script
    assert "is violated" in script
    assert "Temporal properties were violated" in script


# --- Hypothesis -----------------------------------------------------------------------------


def test_hypothesis_profile_registered() -> None:
    """HAT-VER-040: the `ci` profile, its loading, the dependency, and real-code properties."""
    from hypothesis import settings

    profile = settings.get_profile("ci")
    assert profile.derandomize is True
    assert profile.max_examples == 500
    assert profile.deadline is None
    assert profile.print_blob is True
    conftest = (HAT_TESTS / "conftest.py").read_text(encoding="utf-8")
    assert re.search(r'load_profile\("ci" if os\.environ\.get\("CI"\)', conftest)
    dev = (REPO_ROOT / "backend" / "requirements-dev.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^hypothesis==([\d.]+)", dev, re.MULTILINE)
    assert pinned
    lock = (REPO_ROOT / "backend" / "requirements-ci.lock").read_text(encoding="utf-8")
    assert f"hypothesis=={pinned.group(1)} " in lock
    modules = sorted((HAT_TESTS / "properties").glob("test_*.py"))
    assert modules
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = [
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        assert any(m == "app.hat" or m.startswith("app.hat.") for m in imported), module.name


def test_pbt_names_follow_the_convention() -> None:
    """HAT-VER-040 naming: every properties module is test_<area>.py and holds `def test_...`."""
    for module in (HAT_TESTS / "properties").glob("test_*.py"):
        assert re.search(r"^def test_\w+", module.read_text(encoding="utf-8"), re.MULTILINE), module


# --- CI job ---------------------------------------------------------------------------------


def test_ci_verification_job() -> None:
    """HAT-VER-060: the job runs every model, every broken variant, the properties under the
    `ci` profile and the conformance checks inside 15 minutes."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["verification"]
    assert job["timeout-minutes"] <= 15
    steps = job["steps"]
    runs = [step.get("run", "") for step in steps]
    uses = [step.get("uses", "") for step in steps]
    assert any(u.startswith("actions/setup-java@") for u in uses)
    java = next(s for s in steps if s.get("uses", "").startswith("actions/setup-java@"))
    assert int(java["with"]["java-version"]) >= 17
    assert any(r.strip() == "verification/tla/run.sh" for r in runs)
    assert any(r.strip() == "verification/tla/run.sh --broken" for r in runs)
    assert any("pytest tests/hat/properties" in r for r in runs)
    assert any("pytest tests/hat/conformance" in r for r in runs)
    assert str(job["env"]["CI"]).lower() == "true"  # selects the `ci` Hypothesis profile
    for step in steps:
        assert "continue-on-error" not in step
    assert "continue-on-error" not in job


# --- database check coverage ----------------------------------------------------------------


def _requirement_prs(pr_text: str) -> list[int]:
    return [int(n) for n in re.findall(r"\b(\d+)\b", re.sub(r"\([^)]*\)", "", pr_text))]


def _db_names() -> dict[str, list[int]]:
    """DB check name -> PR numbers of the requirements that name it."""
    names: dict[str, list[int]] = {}
    for req in requirements():
        for name in re.findall(r"DB `([\w.]+)`", req.verification):
            names.setdefault(name, []).extend(_requirement_prs(req.pr))
    return names


def _db_tests() -> dict[str, list[tuple[str, bool]]]:
    """function name -> [(file, marked postgres)] for every test_ function under tests/hat/db."""
    found: dict[str, list[tuple[str, bool]]] = {}
    for path in (HAT_TESTS / "db").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_marked = "pytestmark" in path.read_text(encoding="utf-8")
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                marked = module_marked or any(
                    "postgres" in ast.unparse(d) for d in node.decorator_list
                )
                found.setdefault(node.name, []).append((path.name, marked))
    return found


def test_db_checks_match_governance_table() -> None:
    """HAT-VER-050: each DB name is in 03-schema.md; once its PR has merged, exactly one
    postgres-marked check carries it."""
    schema = (SPEC_DIR / "03-schema.md").read_text(encoding="utf-8")
    tests = _db_tests()
    for name, prs in _db_names().items():
        assert name in schema, name
        if min(prs) in MERGED_PRS:
            function = "test_" + name.replace(".", "__")
            assert len(tests.get(function, [])) == 1, name
            assert tests[function][0][1], f"{function} is not marked postgres"


ENSURE_STEP_CHECKS = {"migration.ensure_idempotent", "events.partitions_ahead"}


def test_verification_db_rules_covered() -> None:
    """HAT-SCH-102: every DB check of a merged PR is in the postgres suite and has a broken
    variant that the check must fail (test_db_broken_variants.py, run by backend-postgres)."""
    broken = (HAT_TESTS / "db" / "test_db_broken_variants.py").read_text(encoding="utf-8")
    variants = set(re.findall(r'^    "([\w.]+)": \[', broken, re.MULTILINE))
    for name, prs in _db_names().items():
        if min(prs) in MERGED_PRS:
            # The two ensure-step checks take the step as an argument; their broken variants
            # are separate tests (a faulty ensure step substituted for the real one).
            assert name in variants or name in ENSURE_STEP_CHECKS, name
    assert "test_migration_check_fails_when_ensure_is_not_idempotent" in broken
    assert "test_partitions_check_fails_when_ensure_is_broken" in broken
    assert "test_concurrent_signal_check_fails_without_the_unique_key" in broken


# --- the spec's own rules -------------------------------------------------------------------


def test_spec_requirements_well_formed() -> None:
    """HAT-VER-010."""
    reqs = requirements()
    assert len(reqs) > 100
    for req in reqs:
        methods = set(re.findall(r"\b(TLA|PBT|DB|CONF|OPS)\b", req.verification))
        assert req.verification, req.id
        assert methods, req.id
        assert methods <= METHODS, req.id
        bullets = re.findall(r"^\s+- Property: .*$", req.block, re.MULTILINE)
        assert bullets, req.id
        assert any("Harm:" in b for b in bullets), req.id


# Finding: no requirement in 01-10 cites `PLAN C6` (the VCP module stays intact in the MVP;
# its row in 00 §5 and in HAT-OVR-050 carries no PR). The list only shrinks.
UNTRACED_RESOLUTIONS = {6}


def test_conflict_resolutions_traced() -> None:
    """HAT-OVR-050: every PLAN C1..C12 is cited in a Source line of a requirement in 01-10."""
    cited = set()
    for req in requirements():
        if not req.file.startswith(("01", "02", "03", "04", "05", "06", "07", "08", "09", "10")):
            continue
        source = re.search(r"^- \*\*Source:\*\* (.*)$", req.block, re.MULTILINE)
        if source:
            cited.update(int(n) for n in re.findall(r"PLAN C(\d+)", source.group(1)))
    assert set(range(1, 13)) - cited == UNTRACED_RESOLUTIONS, sorted(set(range(1, 13)) - cited)


README_ROW = re.compile(
    r"^\| (HAT-[\w-]+) \| \[[^\]]*\]\([^)]*\) \| ([^|]*) \| ([^|]*) \|$", re.MULTILINE
)

# Names a README row whose PRs have all merged gives that no check implements. Each is a
# finding reported in the PR description, not a pass. The test fails when an entry starts to
# exist, so the list only shrinks. (Rows that also list a later PR, such as "2, 8", are not
# due yet.)
KNOWN_GAPS = {
    "PBT criteria.fundamentals_point_in_time",  # HAT-DEC-I5: needs the criteria engine (PR 8)
    "CONF old_docs_removed",  # HAT-DEC-BD4: a PR 1 documentation check nobody built
    # PR 3 (HAT-SCH-051, HAT-SCH-081): the rows name TLA properties of models that 11 §3 assigns
    # to PR 13 and PR 15. Their actions map to code that does not exist yet (the order router and
    # the stop mover), so the models arrive with that code; the database checks
    # `runtime.signal_unique_per_day` and `governance.stop_only_rises` cover the same promises
    # at the database in the meantime.
    "TLA OrderReconciliation!AtMostOneEntryPerSignal",
    "TLA StopPlacement!StopOnlyTightens",
}


def _conf_exists(name: str) -> bool:
    candidates = {
        "test_" + name.replace(".", "__"),
        "test_" + name.replace(".", "_"),
        "test_" + name.split(".")[-1],
    }
    for path in (HAT_TESTS / "conformance").glob("test_*.py"):
        defined = set(
            re.findall(r"^def (test_\w+)", path.read_text(encoding="utf-8"), re.MULTILINE)
        )
        if candidates & defined:
            return True
    return False


def _pbt_exists(area_prop: str) -> bool:
    area, _, prop = area_prop.partition(".")
    path = HAT_TESTS / "properties" / f"test_{area}.py"
    return (
        path.exists()
        and re.search(rf"^def test_{prop}\b", path.read_text(encoding="utf-8"), re.MULTILINE)
        is not None
    )


def _tla_exists(ref: str) -> bool:
    model, _, prop = ref.partition("!")
    tla = TLA_DIR / f"{model}.tla"
    cfg = TLA_DIR / f"{model}.cfg"
    return tla.exists() and cfg.exists() and prop in _cfg_properties(cfg)


def test_traceability_points_to_methods() -> None:
    """HAT-VER-011: every method named by a README row whose PRs have all merged exists."""
    readme = (SPEC_DIR / "README.md").read_text(encoding="utf-8")
    db_functions = _db_tests()
    missing = set()
    for _, pr_text, methods in README_ROW.findall(readme):
        prs = _requirement_prs(pr_text)
        if not prs or not set(prs) <= MERGED_PRS:
            continue
        for kind, name in re.findall(r"\b(TLA|PBT|DB|CONF|OPS) `([^`]+)`", methods):
            if kind == "OPS":
                continue  # host checks (file modes) run outside the repo
            if kind == "CONF":
                ok = _conf_exists(name)
            elif kind == "PBT":
                ok = _pbt_exists(name)
            elif kind == "DB":
                ok = "test_" + name.replace(".", "__") in db_functions
            else:
                ok = _tla_exists(name)
            if not ok:
                missing.add(f"{kind} {name}")
    assert missing == KNOWN_GAPS, {
        "missing_unlisted": missing - KNOWN_GAPS,
        "now_exist": KNOWN_GAPS - missing,
    }


# --- verify skills --------------------------------------------------------------------------


def test_verify_skills_complete() -> None:
    """HAT-VER-070/034/071/072: frontmatter, sections, no placeholders, a Properties bullet and a
    recorded scratch break per check, coverage paths that exist, and a current digest."""
    skills = skill_files()
    assert {p.parent.name for p in skills} >= {"verify-scheduler", "verify-governance"}
    for path in skills:
        skill = parse_skill(path)
        assert skill["meta"].get("name") == path.parent.name, path
        assert len(skill["meta"].get("description", "")) > 40, path
        for section in REQUIRED_SECTIONS:
            assert skill["sections"].get(section, "").strip(), f"{path.parent.name}: {section}"
        assert not PLACEHOLDER.search(skill["text"]), path
        checks = property_ids(skill["sections"]["Properties"])
        assert checks, path
        for bullet in re.findall(r"^- `[^`]+`.*$", skill["sections"]["Properties"], re.MULTILINE):
            assert "Harm:" in bullet or "harm" in bullet.lower(), bullet
        # one recorded break per check (VER-072), each with its failing output
        assert sorted(break_ids(skill["sections"]["Scratch breaks"])) == sorted(checks), path
        for block in re.split(r"^### ", skill["sections"]["Scratch breaks"], flags=re.MULTILINE)[
            1:
        ]:
            assert "Break:" in block, block.splitlines()[0]
            assert "```" in block, block.splitlines()[0]
        run = skill["sections"]["Run"]
        assert "```" in run, path


def test_verify_skill_sync_current() -> None:
    """HAT-VER-071: every covered file exists and the covered digest matches the files now
    (a changed covered file makes the skill stale until it is re-confirmed and the digest
    updated). The sync commit is a 40-hex sha; a squash merge discards the branch commit, so
    the digest is what is checked."""
    for path in skill_files():
        skill = parse_skill(path)
        files = covered_files(skill["sections"]["Coverage"])
        assert files, path
        for rel in files:
            assert (REPO_ROOT / rel).is_file(), (path.parent.name, rel)
        section = skill["sections"]["Sync commit"]
        assert re.search(r"Sync commit: `[0-9a-f]{40}`", section), path
        match = re.search(r"Covered digest: `([0-9a-f]{64})`", section)
        assert match, path
        assert match.group(1) == digest(files), (
            f"{path.parent.name} is stale: run python "
            f"backend/tests/hat/conformance/hat_conf_skills.py "
            f"{path.parent.name} after re-confirming, then update the digest"
        )


def test_verify_skill_break_recorded() -> None:
    test_verify_skills_complete()


def test_verify_skill_findings_recorded() -> None:
    """HAT-VER-034: each skill has a Scope changes section (possibly stating there are none)."""
    for path in skill_files():
        assert parse_skill(path)["sections"].get("Scope changes", "").strip(), path

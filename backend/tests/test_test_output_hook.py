"""The agent test-output hook wraps real test runs only, and its filter keeps TLC's result lines."""

import importlib.util
from types import ModuleType

import pytest

from app import paths

HOOKS = paths.PROJECT_ROOT / ".claude" / "hooks"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load("test_output_hook")
output_filter = _load("test_output_filter")


@pytest.mark.parametrize(
    "command",
    [
        "cat pytest.ini",
        "grep -rn pytest backend/",
        "rg vitest package.json",
        "pip show pytest",
        "git log --grep pytest",
    ],
)
def test_commands_that_only_mention_a_runner_pass_through(command: str) -> None:
    assert not hook.TEST_CMD.search(command)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q tests/test_paths.py",
        "python -m pytest -q",
        "python3.14 -m pytest tests/hat/conformance -q",
        "../venv/bin/python -m pytest -q",
        "../venv/bin/pytest -q",
        "venv/Scripts/python.exe -m pytest -q",
        "cd backend && CI=1 python -m pytest tests/hat/properties/x.py -q",
        "timeout 300 env PYTHONPATH=. pytest -q",
        "ruff check .; pytest -q",
        "(cd backend\npytest -q)",
        "pnpm test",
        "pnpm run vitest",
        "npx vitest run",
        "verification/tla/run.sh",
        "bash verification/tla/run.sh Approval",
    ],
)
def test_real_runner_invocations_are_caught(command: str) -> None:
    assert hook.TEST_CMD.search(command)


def test_tlc_view_keeps_the_counterexample_and_result_lines_only() -> None:
    lines = [
        "TLC2 Version 2.19",
        "Progress(3) at x: 10 states generated (1 s/min), 5 distinct states found, 2 states left.",
        "Error: Invariant Safe is violated.",
        "State 1: <Initial predicate>",
        "12 states generated, 6 distinct states found, 0 states left on queue.",
        "Finished in 00s at (x)",
        "FAIL Foo: TLC reported a problem",
        "== Bar: must pass",
        "Model checking completed. No error has been found.",
        "1 failure(s)",
    ]

    assert output_filter.tlc_view(lines) == [
        "Error: Invariant Safe is violated.",
        "State 1: <Initial predicate>",
        "12 states generated, 6 distinct states found, 0 states left on queue.",
        "FAIL Foo: TLC reported a problem",
        "== Bar: must pass",
        "Model checking completed. No error has been found.",
        "1 failure(s)",
    ]
    assert output_filter.tlc_view(["plain output"]) is None

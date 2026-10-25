"""The backtest job's seams (`backtest.jobs`): the command line it starts, the variants file it
writes, the checks before it starts, how it reads the child's output, and the job wiring with a
stand-in interpreter (a shell script, never the backtest)."""

import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.backtest.jobs import (
    JOB_BUSY,
    MAX_VARIANTS,
    Job,
    Jobs,
    JobVariant,
    check_variants,
    command,
    outcomes_of,
    refusal_of,
    variants_doc,
)
from app.hat.backtest.spec import BacktestError

VERSION = uuid.UUID("3b44c25e-f900-458c-b600-c18bde7e7231")
FILE = Path("variants.json")
BASE = [
    "--strategy-version",
    str(VERSION),
    "--universe",
    "nifty500",
    "--from",
    "2018-01-01",
    "--to",
    "2024-12-31",
]


def _command(variants: list[JobVariant]) -> list[str]:
    return command(VERSION, "nifty500", date(2018, 1, 1), date(2024, 12, 31), variants, FILE)


def test_one_variant_runs_as_a_plain_research_run_with_flags() -> None:
    variant = JobVariant(
        label="run",
        sets=(("exits[2].at_r", 3), ("exits[+]", {"rule": "max_hold", "days": 60})),
        risk_pct=Decimal("0.0050"),
        max_positions=8,
        sleeve_weight=Decimal("0.5"),
        slot_order="score",
    )
    assert _command([variant]) == [
        *BASE,
        "--set",
        "exits[2].at_r=3",
        "--set",
        'exits[+]={"rule": "max_hold", "days": 60}',
        "--risk-pct",
        "0.005",
        "--max-positions",
        "8",
        "--sleeve-weight",
        "0.5",
        "--slot-order",
        "score",
    ]


def test_a_plain_run_without_overrides_takes_the_version_as_it_is() -> None:
    assert _command([JobVariant(label="run")]) == BASE


def test_two_variants_run_as_a_sweep_and_never_for_promotion() -> None:
    args = _command([JobVariant(label="a"), JobVariant(label="b", max_positions=5)])
    assert args == [*BASE, "--variants", str(FILE)]
    assert "--for-promotion" not in args


def test_the_variants_file_names_only_what_each_variant_sets() -> None:
    doc = variants_doc(
        [
            JobVariant(label="base"),
            JobVariant(label="hold60", sets=(("exits[4].days", 60),), risk_pct=Decimal("0.01")),
        ]
    )
    assert doc == [
        {"label": "base"},
        {"label": "hold60", "set": ["exits[4].days=60"], "risk_pct": "0.01"},
    ]


@pytest.mark.parametrize(
    ("variants", "code"),
    [
        ([], "backtest_sweep_bad_variants"),
        (
            [JobVariant(label=str(n)) for n in range(MAX_VARIANTS + 1)],
            "backtest_sweep_bad_variants",
        ),
        ([JobVariant(label="a"), JobVariant(label="a")], "backtest_sweep_bad_variants"),
        ([JobVariant(label="")], "backtest_sweep_bad_variants"),
        ([JobVariant(label="a", sets=(("exits[0", 1),))], "backtest_bad_override_path"),
        ([JobVariant(label="a", slot_order="fastest")], "backtest_bad_slot_order"),
        ([JobVariant(label="a", sleeve_weight=Decimal("1.5"))], "backtest_bad_sleeve_weight"),
    ],
)
def test_check_variants_refuses_what_the_command_line_would(
    variants: list[JobVariant], code: str
) -> None:
    with pytest.raises(BacktestError) as refused:
        check_variants(variants)
    assert refused.value.code == code


def test_check_variants_takes_a_good_sweep() -> None:
    check_variants(
        [
            JobVariant(label="a", sets=(("playbooks[default].stop.max_pct", 10),)),
            JobVariant(label="b", slot_order="random:3", sleeve_weight=Decimal("0.25")),
        ]
    )


def test_a_sweep_reads_one_finished_variant_per_line_and_skips_a_partial_line() -> None:
    text = (
        '{"label": "a", "run_id": "01a1", "status": "completed", "code": null, "trades": 4}\n'
        '{"label": "b", "run_id": null, "status": "refused", "code": "backtest_override_invalid"}\n'
        '{"label": "c", "run_'
    )
    assert outcomes_of(text, sweep=True) == [
        {"label": "a", "run_id": "01a1", "status": "completed", "code": None},
        {"label": "b", "run_id": None, "status": "refused", "code": "backtest_override_invalid"},
    ]


def test_a_plain_run_has_its_outcome_only_once_its_summary_is_complete() -> None:
    summary = '{\n  "run_id": "01a1",\n  "status": "failed",\n  "trades": null\n}\n'
    assert outcomes_of(summary[:20], sweep=False) == []
    assert outcomes_of(summary, sweep=False) == [
        {"label": None, "run_id": "01a1", "status": "failed", "code": None}
    ]


def test_refusal_of_reads_the_code_and_nothing_else() -> None:
    assert refusal_of("noise\nbacktest refused: backtest_no_policy\n") == "backtest_no_policy"
    assert refusal_of("Traceback (most recent call last):\n  ...\nKeyError: 'x'\n") is None


def _fake_python(folder: Path, body: str) -> str:
    """A stand-in for the interpreter: a shell script that ignores its arguments."""
    script = folder / "fake-python"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _finished(registry: Jobs) -> Job:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        (job,) = registry.list()
        if job.status != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_a_job_runs_one_at_a_time_and_records_how_its_child_ended(tmp_path: Path) -> None:
    line = (
        '{"label": "a", "run_id": null, "status": "refused", "code": "backtest_override_invalid"}'
    )
    python = _fake_python(
        tmp_path,
        f"sleep 0.3\necho '{line}'\necho 'backtest refused: backtest_no_policy' >&2\nexit 2",
    )
    registry = Jobs(python=python)
    variants = [JobVariant(label="a"), JobVariant(label="b")]
    window = (date(2020, 1, 1), date(2021, 1, 1))
    job = registry.start(tmp_path, VERSION, "u", *window, variants)
    with pytest.raises(BacktestError) as busy:
        registry.start(tmp_path, VERSION, "u", *window, variants)
    assert busy.value.code == JOB_BUSY
    assert (job.folder / "variants.json").is_file()
    done = _finished(registry)
    assert (done.status, done.code, done.labels) == ("refused", "backtest_no_policy", ("a", "b"))
    assert done.outcomes == [
        {"label": "a", "run_id": None, "status": "refused", "code": "backtest_override_invalid"}
    ]


def test_a_child_that_crashes_is_a_failed_job_without_its_text(tmp_path: Path) -> None:
    python = _fake_python(tmp_path, "echo 'Traceback: password=hunter2' >&2\nexit 1")
    registry = Jobs(python=python)
    registry.start(tmp_path, VERSION, "u", date(2020, 1, 1), date(2021, 1, 1), [JobVariant("run")])
    done = _finished(registry)
    assert (done.status, done.code, done.outcomes) == ("failed", "backtest_job_failed", [])

#!/usr/bin/env python3
"""Reduce test-runner output to the failures plus the final summary.

Reads a test run's combined stdout/stderr on stdin. Recognises pytest and
vitest output; for anything else it keeps the tail. Never hides a failure:
failure sections are kept (capped), and the summary line always survives.
"""

import re
import sys

MAX_LINES = 250  # total lines passed on
MAX_LINE_CHARS = 400  # long lines (byte dumps, big reprs) are clipped
TAIL_UNKNOWN = 60  # lines kept when the format is not recognised

PYTEST_SUMMARY = re.compile(
    r"^(=+ )?.*\b(passed|failed|error|errors|skipped|deselected|no tests ran)\b"
    r".*\bin [\d.]+s\b.*( =+)?$"
)
PYTEST_SECTION = re.compile(r"^=+ (FAILURES|ERRORS) =+$")
PYTEST_SHORT = re.compile(r"^=+ short test summary info =+$")
PYTEST_ANY_BANNER = re.compile(r"^=+ .+ =+$")
VITEST_SUMMARY = re.compile(r"^\s*(Test Files|Tests|Duration)\s")
VITEST_FAIL = re.compile(r"(\bFAIL\b|×|✗|Failed Tests|AssertionError|Error:)")


def clip(line: str) -> str:
    if len(line) > MAX_LINE_CHARS:
        return line[:MAX_LINE_CHARS] + f" …[{len(line) - MAX_LINE_CHARS} chars clipped]"
    return line


def pytest_view(lines: list[str]) -> list[str] | None:
    summary = [line for line in lines if PYTEST_SUMMARY.match(line)]
    if not summary:
        return None
    out: list[str] = []
    keep = False
    for line in lines:
        if PYTEST_SECTION.match(line) or PYTEST_SHORT.match(line):
            keep = True
        elif PYTEST_ANY_BANNER.match(line):
            keep = False
        if keep:
            out.append(line)
    if summary[-1] not in out:
        out.append(summary[-1])
    # Output from commands chained after the test run (git log, echo …) is not test noise.
    last = max(i for i, line in enumerate(lines) if PYTEST_SUMMARY.match(line))
    return out + lines[last + 1 :][-TAIL_UNKNOWN:]


def vitest_view(lines: list[str]) -> list[str] | None:
    if not any(VITEST_SUMMARY.match(line) for line in lines):
        return None
    out: list[str] = []
    in_fail = False
    for line in lines:
        if "Failed Tests" in line or re.match(r"^\s*(FAIL|×|✗)\s", line):
            in_fail = True
        if in_fail or VITEST_FAIL.search(line) or VITEST_SUMMARY.match(line):
            out.append(line)
    # Keep output from commands chained after the test run, as pytest_view does.
    last = max(i for i, line in enumerate(lines) if VITEST_SUMMARY.match(line))
    return out + [line for line in lines[last + 1 :] if line not in out][-TAIL_UNKNOWN:]


def main() -> None:
    text = sys.stdin.read()
    lines = text.splitlines()
    view = pytest_view(lines) or vitest_view(lines)
    note = ""
    if view is None:
        view = lines[-TAIL_UNKNOWN:]
        if len(lines) > TAIL_UNKNOWN:
            note = (
                f"[test-filter: unrecognised format, showing last {TAIL_UNKNOWN} "
                f"of {len(lines)} lines]"
            )
    if len(view) > MAX_LINES:
        head = view[: MAX_LINES - 20]
        tail = view[-20:]
        view = head + [f"[test-filter: {len(view) - MAX_LINES} lines trimmed]"] + tail
    if note:
        sys.stdout.write(note + "\n")
    for line in view:
        sys.stdout.write(clip(line) + "\n")
    sys.stdout.write(
        f"[test-filter: {len(lines)} lines of output reduced to failures + summary; "
        "prefix the command with TEST_FILTER=0 for full output]\n"
    )


if __name__ == "__main__":
    main()

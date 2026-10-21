#!/usr/bin/env python3
"""PreToolUse hook (Bash): pipe test-runner output through test_output_filter.py.

Rewrites `pytest …` / `pnpm test` / `vitest …` commands so only failed cases and
the final summary reach the model. The command's own exit code is preserved.
Opt out per command by including `TEST_FILTER=0`.
"""

import json
import re
import sys
from pathlib import Path

# Forward slashes so the paths also work in Git Bash on Windows.
FILTER = Path(__file__).resolve().with_name("test_output_filter.py").as_posix()
PYTHON = Path(sys.executable).as_posix()
TEST_CMD = re.compile(
    r"(^|[\s;&|(/])(pytest|vitest)\b|\bpnpm\s+(run\s+)?(test|vitest)\b|\bpython3?\s+-m\s+pytest\b"
)
SKIP = re.compile(
    r"TEST_FILTER=0|--collect-only|--watch\b|test_output_filter\.py|\bpytest\s+--version\b"
)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    if payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not TEST_CMD.search(command) or SKIP.search(command):
        return
    wrapped = f'( {command}\n) 2>&1 | "{PYTHON}" "{FILTER}"; ( exit ${{PIPESTATUS[0]}} )'
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": {**tool_input, "command": wrapped},
                }
            }
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
TODO/FIXME Checker

Detects TODO and FIXME comments without GitHub issue references.
Ensures all technical debt is tracked.

Usage:
    python scripts/check_todo_fixme.py [path]

Exit codes:
    0 - No violations found
    1 - Violations found
"""

import re
import sys
from pathlib import Path

# Pattern to detect TODO/FIXME without issue reference
# Valid: TODO(#123), FIXME(#456), TODO: #123
# Invalid: TODO: fix this, FIXME later
# Must have colon or be followed by text to be considered a real TODO
TODO_PATTERN = re.compile(
    r'\b(TODO|FIXME)\s*[:\-](?!\s*#\d+)',
    re.IGNORECASE
)

class TodoViolation:
    """Represents a TODO/FIXME without issue reference."""

    def __init__(self, file: Path, line: int, context: str):
        self.file = file
        self.line = line
        self.context = context.strip()

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line}\n"
            f"  {self.context}\n"
            f"  Add GitHub issue reference: TODO(#123) or FIXME: #456"
        )


def check_file(filepath: Path) -> list[TodoViolation]:
    """Check a single file for TODO/FIXME violations."""

    # Skip test files that contain intentional violations
    if filepath.name == 'test_check_todo_fixme.py':
        return []

    try:
        content = filepath.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
        return []

    violations = []
    lines = content.split('\n')

    for i, line in enumerate(lines, start=1):
        # Skip if line is not a comment
        if not any(marker in line for marker in ['//', '#', '/*', '*']):
            continue

        # Skip lines that are just explaining valid/invalid formats
        if 'Valid:' in line or 'Invalid:' in line:
            continue

        # Check for TODO/FIXME with colon but no issue reference
        if TODO_PATTERN.search(line):
            violations.append(
                TodoViolation(
                    file=filepath,
                    line=i,
                    context=line
                )
            )
        # Also check for TODO/FIXME with parentheses but no issue number
        elif re.search(r'\b(TODO|FIXME)\s*\([^#\)]*\)', line, re.IGNORECASE):
            violations.append(
                TodoViolation(
                    file=filepath,
                    line=i,
                    context=line
                )
            )

    return violations


def check_directory(directory: Path, extensions: list[str]) -> list[TodoViolation]:
    """Recursively check all files with given extensions."""
    violations = []

    # Directories to skip
    skip_dirs = {
        'node_modules', 'venv', '.git', 'dist', 'build',
        '.next', '__pycache__', '.pytest_cache', 'coverage',
        '.turbo', '.vercel'
    }

    for ext in extensions:
        for filepath in directory.rglob(f"*{ext}"):
            # Skip if any parent directory is in skip list
            if any(skip in filepath.parts for skip in skip_dirs):
                continue

            violations.extend(check_file(filepath))

    return violations


def main() -> int:
    """Main entry point."""

    # Determine path to check
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        # Default to current directory
        path = Path.cwd()

    if not path.exists():
        print(f"Error: Path does not exist: {path}", file=sys.stderr)
        return 1

    # File extensions to check
    extensions = ['.py', '.ts', '.tsx', '.js', '.jsx']

    # Check files
    if path.is_file():
        violations = check_file(path)
    else:
        violations = check_directory(path, extensions)

    # Report results
    if violations:
        print(f"\n❌ Found {len(violations)} TODO/FIXME without issue reference:\n")
        for violation in violations:
            print(violation)
            print()
        return 1
    else:
        print("✅ No TODO/FIXME violations found")
        return 0


if __name__ == "__main__":
    sys.exit(main())

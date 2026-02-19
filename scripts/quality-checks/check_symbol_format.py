#!/usr/bin/env python3
"""
Symbol Format Checker - Ruff-Compatible Linter

Detects direct symbol manipulation outside of symbol_master.py.
Enforces use of symbol_master methods for all symbol format conversions.

This tool integrates with Ruff's workflow by providing compatible output format
and can be used in pre-commit hooks, CI pipelines, and IDE integrations.

Violations:
- .replace('NSE:', '') → Use symbol_master.to_db()
- .replace('-EQ', '') → Use symbol_master.to_db()
- .split(':') for symbol parsing → Use symbol_master methods

Usage:
    python scripts/check_symbol_format.py [path] [--format=<format>]

    Formats:
        text    - Human-readable output (default)
        ruff    - Ruff-compatible format (file:line:col: code message)
        json    - JSON output for tool integration
        github  - GitHub Actions annotation format

Exit codes:
    0 - No violations found
    1 - Violations found
    2 - Error during execution
"""

import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# Violation patterns with suggested fixes and error codes
class ViolationType(Enum):
    """Symbol format violation types with unique codes."""
    NSE_REPLACE = ("SYM001", r"\.replace\(['\"]NSE:['\"],\s*['\"]['\"]\)",
                   "Use symbol_master.to_db() instead of .replace('NSE:', '')")
    EQ_REPLACE = ("SYM002", r"\.replace\(['\"]-EQ['\"],\s*['\"]['\"]\)",
                  "Use symbol_master.to_db() instead of .replace('-EQ', '')")
    COLON_SPLIT = ("SYM003", r"\.split\(['\"]:['\"]\)",
                   "Use symbol_master methods for parsing instead of .split(':')")

    def __init__(self, code: str, pattern: str, message: str):
        self.code = code
        self.pattern = pattern
        self.message = message

@dataclass
class SymbolFormatViolation:
    """Represents a symbol format violation with detailed context."""

    file: Path
    line: int
    column: int
    code: str
    message: str
    context: str
    violation_type: ViolationType

    def to_text(self) -> str:
        """Format as human-readable text."""
        return (
            f"\n{self.file}:{self.line}:{self.column}\n"
            f"  {self.code}: {self.message}\n"
            f"  Context: {self.context}"
        )

    def to_ruff(self) -> str:
        """Format as Ruff-compatible output."""
        return f"{self.file}:{self.line}:{self.column}: {self.code} {self.message}"

    def to_github(self) -> str:
        """Format as GitHub Actions annotation."""
        return (
            f"::error file={self.file},line={self.line},col={self.column},"
            f"title={self.code}::{self.message}"
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON output."""
        return {
            "file": str(self.file),
            "line": self.line,
            "column": self.column,
            "code": self.code,
            "message": self.message,
            "context": self.context,
            "type": self.violation_type.name
        }


class OutputFormat(Enum):
    """Supported output formats."""
    TEXT = "text"
    RUFF = "ruff"
    JSON = "json"
    GITHUB = "github"


def find_column(content: str, match_start: int) -> int:
    """Find the column number (1-indexed) for a match position."""
    line_start = content.rfind('\n', 0, match_start) + 1
    return match_start - line_start + 1


def check_file(filepath: Path) -> list[SymbolFormatViolation]:
    """Check a single Python file for symbol format violations.

    Args:
        filepath: Path to the Python file to check

    Returns:
        List of violations found in the file
    """

    # Skip symbol_master.py itself
    if filepath.name == "symbol_master.py":
        return []

    # Skip this checker script itself
    if filepath.name == "check_symbol_format.py":
        return []

    # Skip test files (they may test the violations)
    if filepath.name.startswith("test_") or "tests" in filepath.parts:
        return []

    # Skip test violation files (intentional violations for testing)
    if "test_violations" in filepath.parts:
        return []

    try:
        content = filepath.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
        return []

    violations = []
    lines = content.split('\n')

    for violation_type in ViolationType:
        for match in re.finditer(violation_type.pattern, content):
            line_num = content[:match.start()].count('\n') + 1
            column = find_column(content, match.start())
            context = lines[line_num - 1] if line_num <= len(lines) else ""

            # Skip if this is in a comment or docstring (basic heuristic)
            context_stripped = context.strip()
            if context_stripped.startswith('#'):
                continue
            if context_stripped.startswith('"""') or context_stripped.startswith("'''"):
                continue

            # Skip if the line is just a string literal (error message definition)
            if context_stripped.startswith('"') or context_stripped.startswith("'"):
                # Check if it's a standalone string (not part of code)
                if '=' not in context_stripped and 'return' not in context_stripped:
                    continue

            violations.append(
                SymbolFormatViolation(
                    file=filepath,
                    line=line_num,
                    column=column,
                    code=violation_type.code,
                    message=violation_type.message,
                    context=context.strip(),
                    violation_type=violation_type
                )
            )

    return violations


def check_directory(directory: Path) -> list[SymbolFormatViolation]:
    """Recursively check all Python files in a directory.

    Args:
        directory: Path to the directory to check

    Returns:
        List of all violations found in the directory
    """
    violations = []

    for filepath in directory.rglob("*.py"):
        violations.extend(check_file(filepath))

    return violations


def format_output(violations: list[SymbolFormatViolation],
                  output_format: OutputFormat) -> str:
    """Format violations according to the specified output format.

    Args:
        violations: List of violations to format
        output_format: Desired output format

    Returns:
        Formatted string output
    """
    if not violations:
        if output_format == OutputFormat.JSON:
            return json.dumps({"violations": [], "count": 0}, indent=2)
        return "✅ No symbol format violations found"

    if output_format == OutputFormat.TEXT:
        output = [f"\n❌ Found {len(violations)} symbol format violation(s):\n"]
        output.extend(v.to_text() for v in violations)
        output.append("\n")
        return "\n".join(output)

    elif output_format == OutputFormat.RUFF:
        return "\n".join(v.to_ruff() for v in violations)

    elif output_format == OutputFormat.GITHUB:
        return "\n".join(v.to_github() for v in violations)

    elif output_format == OutputFormat.JSON:
        return json.dumps({
            "violations": [v.to_dict() for v in violations],
            "count": len(violations)
        }, indent=2)

    return ""


def parse_args() -> tuple[Path | None, OutputFormat]:
    """Parse command line arguments.

    Returns:
        Tuple of (path to check, output format)
    """
    path = None
    output_format = OutputFormat.TEXT

    for arg in sys.argv[1:]:
        if arg.startswith("--format="):
            format_str = arg.split("=", 1)[1].lower()
            try:
                output_format = OutputFormat(format_str)
            except ValueError:
                print(f"Error: Invalid format '{format_str}'. "
                      f"Valid formats: {', '.join(f.value for f in OutputFormat)}",
                      file=sys.stderr)
                sys.exit(2)
        elif not arg.startswith("--"):
            path = Path(arg)

    return path, output_format


def main() -> int:
    """Main entry point.

    Returns:
        Exit code: 0 for success, 1 for violations found, 2 for errors
    """

    # Parse arguments
    path, output_format = parse_args()

    # Determine path to check
    if path is None:
        # Default to backend/app directory
        path = Path(__file__).parent.parent / "app"

    if not path.exists():
        print(f"Error: Path does not exist: {path}", file=sys.stderr)
        return 2

    # Check files
    try:
        if path.is_file():
            violations = check_file(path)
        else:
            violations = check_directory(path)
    except Exception as e:
        print(f"Error during checking: {e}", file=sys.stderr)
        return 2

    # Format and output results
    output = format_output(violations, output_format)
    # Handle Windows console encoding issues
    try:
        print(output)
    except UnicodeEncodeError:
        # Fallback to ASCII-safe output on Windows
        print(output.encode('ascii', 'replace').decode('ascii'))

    # Return appropriate exit code
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

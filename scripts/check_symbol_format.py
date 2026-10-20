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

    NSE_REPLACE = (
        "SYM001",
        r"\.replace\(['\"]NSE:['\"],\s*['\"]['\"]\)",
        "Use symbol_master.to_db() instead of .replace('NSE:', '')",
    )
    EQ_REPLACE = (
        "SYM002",
        r"\.replace\(['\"]-EQ['\"],\s*['\"]['\"]\)",
        "Use symbol_master.to_db() instead of .replace('-EQ', '')",
    )
    COLON_SPLIT = (
        "SYM003",
        r"\.split\(['\"]:['\"]\)",
        "Use symbol_master methods for parsing instead of .split(':')",
    )

    def __init__(self, code: str, pattern: str, message: str) -> None:
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
            "type": self.violation_type.name,
        }


class OutputFormat(Enum):
    """Supported output formats."""

    TEXT = "text"
    RUFF = "ruff"
    JSON = "json"
    GITHUB = "github"


def find_column(content: str, match_start: int) -> int:
    """Find the column number (1-indexed) for a match position."""
    line_start = content.rfind("\n", 0, match_start) + 1
    return match_start - line_start + 1


def _is_exempt(filepath: Path) -> bool:
    """symbol_master.py, this checker, and test files (which may test the violations)."""
    if filepath.name in {"symbol_master.py", "check_symbol_format.py"}:
        return True
    if filepath.name.startswith("test_") or "tests" in filepath.parts:
        return True
    # Test violation files hold intentional violations
    return "test_violations" in filepath.parts


def _is_non_code_line(context_stripped: str) -> bool:
    """Comment, docstring or standalone string-literal lines (basic heuristic)."""
    if context_stripped.startswith(("#", '"""', "'''")):
        return True
    # A line that is just a string literal (e.g. an error message definition)
    return (
        context_stripped.startswith(('"', "'"))
        and "=" not in context_stripped
        and "return" not in context_stripped
    )


def check_file(filepath: Path) -> list[SymbolFormatViolation]:
    """Check a single Python file for symbol format violations.

    Args:
        filepath: Path to the Python file to check

    Returns:
        List of violations found in the file
    """
    if _is_exempt(filepath):
        return []

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
        return []

    violations = []
    lines = content.split("\n")

    for violation_type in ViolationType:
        for match in re.finditer(violation_type.pattern, content):
            line_num = content[: match.start()].count("\n") + 1
            context = lines[line_num - 1] if line_num <= len(lines) else ""
            if _is_non_code_line(context.strip()):
                continue

            violations.append(
                SymbolFormatViolation(
                    file=filepath,
                    line=line_num,
                    column=find_column(content, match.start()),
                    code=violation_type.code,
                    message=violation_type.message,
                    context=context.strip(),
                    violation_type=violation_type,
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


def format_output(violations: list[SymbolFormatViolation], output_format: OutputFormat) -> str:
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
        return json.dumps(
            {"violations": [v.to_dict() for v in violations], "count": len(violations)}, indent=2
        )

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
                print(
                    f"Error: Invalid format '{format_str}'. "
                    f"Valid formats: {', '.join(f.value for f in OutputFormat)}",
                    file=sys.stderr,
                )
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
        path = Path(__file__).resolve().parent.parent / "backend" / "app"

    # Only check files inside this repository.
    repo_root = Path(__file__).resolve().parent.parent
    path = path.resolve()
    if not path.is_relative_to(repo_root):
        print(f"Error: Path is outside the repository: {path}", file=sys.stderr)
        return 2

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
        print(output.encode("ascii", "replace").decode("ascii"))

    # Return appropriate exit code
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

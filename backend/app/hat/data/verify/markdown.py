"""The verify report as Markdown: the summary table, then each check's summary and findings."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

EXAMPLES_SHOWN = 5


def _cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def summary_table(doc: Mapping[str, Any]) -> list[str]:
    """One row per check: id, title, status, blocking findings' counts."""
    lines = ["| Check | What | Status | Findings |", "|---|---|---|---|"]
    for check_id, check in (doc.get("checks") or {}).items():
        found = ", ".join(
            f"{f['code']} {f['count']}" for f in check.get("findings", ()) if f.get("count")
        )
        lines.append(
            f"| {check_id} | {_cell(check.get('title', ''))} | {check['status'].upper()} "
            f"| {_cell(found or '-')} |"
        )
    return lines


def _summary_lines(summary: Mapping[str, Any]) -> list[str]:
    lines = []
    for key, value in summary.items():
        if (
            isinstance(value, Mapping)
            and value
            and all(isinstance(v, Mapping) for v in value.values())
        ):
            columns = sorted({c for v in value.values() for c in v})
            lines += ["", f"`{key}`:", "", "| | " + " | ".join(columns) + " |"]
            lines.append("|---" * (len(columns) + 1) + "|")
            for row, cells in value.items():
                lines.append(
                    f"| {_cell(row)} | "
                    + " | ".join(_cell(cells.get(c, "")) for c in columns)
                    + " |"
                )
            lines.append("")
        else:
            lines.append(f"- `{key}`: {_cell(json.dumps(value, default=str))}")
    return lines


def _finding_lines(findings: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for f in findings:
        lines.append(f"- **{f['code']}** ({f['severity']}): {f['count']}")
        extra = {k: v for k, v in f.items() if k not in {"code", "severity", "count", "items"}}
        if extra:
            lines.append(f"  - {_cell(json.dumps(extra, default=str))}")
        lines += [
            f"  - `{_cell(json.dumps(item, default=str))}`"
            for item in list(f.get("items", ()))[:EXAMPLES_SHOWN]
        ]
    return lines


def render(doc: Mapping[str, Any], log: Sequence[str] = ()) -> str:
    window = doc.get("window") or {}
    lines = [
        f"# Data verification {window.get('from')} to {window.get('to')}",
        "",
        f"Status **{str(doc.get('status')).upper()}**, created {doc.get('created')}, "
        f"digest `{doc.get('digest')}`. Full examples (up to 25 per finding) in `verify.json`.",
        "",
        *summary_table(doc),
    ]
    for check_id, check in (doc.get("checks") or {}).items():
        lines += ["", f"## {check_id} {check.get('title', '')}: {check['status'].upper()}", ""]
        lines += _summary_lines(check.get("summary") or {})
        if check.get("findings"):
            lines += ["", "Findings:", "", *_finding_lines(check["findings"])]
    if log:
        lines += ["", "## Notes", "", *(f"- {_cell(line)}" for line in log)]
    return "\n".join(lines) + "\n"

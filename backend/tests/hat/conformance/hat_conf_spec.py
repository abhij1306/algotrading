"""Read the table definitions of docs/specs/hat/03-schema.md and the requirement blocks of the
spec files, for the conformance checks. The spec is the data; nothing here restates a column."""

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SPEC_DIR = REPO_ROOT / "docs" / "specs" / "hat"


@dataclass
class SpecColumn:
    name: str
    logical_type: str  # uuid, text, int, bigint, boolean, date, time, char(3), numeric(18,4) ...
    nullable: bool
    pk: bool = False
    fk_table: str | None = None


@dataclass
class SpecTable:
    name: str
    columns: list[SpecColumn] = field(default_factory=list)
    pk: tuple[str, ...] = ()
    uniques: list[tuple[str, ...]] = field(default_factory=list)


def _split_cols(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(","))


def _column_from_cell(name: str, cell: str) -> SpecColumn:
    """`uuid PK`, `uuid FK hat_market`, `text null`, `numeric(18,4)`, `int` ..."""
    tokens = cell.replace("≥", " ").split()
    logical = tokens[0]
    nullable = "null" in tokens[1:]
    pk = "PK" in tokens[1:]
    fk_table = None
    if "FK" in tokens:
        fk_table = tokens[tokens.index("FK") + 1]
    return SpecColumn(name, logical, nullable and not pk, pk, fk_table)


def _table_constraints(text: str) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    pk: tuple[str, ...] = ()
    uniques = []
    for match in re.finditer(r"\bPK \(([^)]*)\)", text):
        pk = _split_cols(match.group(1))
    for match in re.finditer(r"\bunique \(([^)]*)\)", text):
        uniques.append(_split_cols(match.group(1)))
    return pk, uniques


def _parse_markdown_table(block: str) -> list[SpecColumn]:
    columns = []
    for line in block.splitlines():
        if not line.startswith("|") or line.startswith("| Column") or line.startswith("| ---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue
        columns.append(_column_from_cell(cells[0], cells[1]))
        if "unique" in (cells[2] if len(cells) > 2 else ""):
            columns[-1].fk_table = columns[-1].fk_table  # a unique note is read below
    return columns


def _parse_sql(block: str, table: str) -> SpecTable:
    """The `create table` statement of a table the spec gives as SQL."""
    out = SpecTable(table)
    body = block[block.index("(") + 1 : block.rindex(");")]
    for raw in body.splitlines():
        line = raw.strip().rstrip(",")
        if not line:
            continue
        head = line.split()[0].lower()
        if head == "unique":
            out.uniques.append(_split_cols(re.search(r"\(([^)]*)\)", line).group(1)))
            continue
        if head in ("check", "primary", "constraint", "foreign"):
            continue
        name, logical, *rest = line.split()
        rest_text = " ".join(rest).lower()
        ref = re.search(r"references (\w+)\(", line)
        pk = "primary key" in rest_text
        out.columns.append(
            SpecColumn(
                name,
                logical.lower(),
                nullable=not pk and "not null" not in rest_text,
                pk=pk,
                fk_table=ref.group(1) if ref else None,
            )
        )
        if pk:
            out.pk = (name,)
    return out


def parse_tables() -> dict[str, SpecTable]:
    """Every table of 03-schema.md, keyed by name."""
    text = (SPEC_DIR / "03-schema.md").read_text(encoding="utf-8")
    heading = re.compile(r"^\*\*`(hat_\w+)`\*\*(.*)$", re.MULTILINE)
    matches = list(heading.finditer(text))
    tables: dict[str, SpecTable] = {}
    for index, match in enumerate(matches):
        name, rest = match.group(1), match.group(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        stop = re.search(r"^(#{2,4} )", text[match.end() : end], re.MULTILINE)
        if stop:
            end = match.end() + stop.start()
        block = text[match.end() : end]
        sql = re.search(r"```sql\n(create table " + name + r".*?)```", block, re.DOTALL)
        if sql:
            table = _parse_sql(sql.group(1), name)
        else:
            table = SpecTable(name, _parse_markdown_table(block))
            pk, uniques = _table_constraints(rest + "\n" + block)
            table.pk = pk
            table.uniques = uniques
            for column in table.columns:
                if column.pk:
                    table.pk = table.pk or (column.name,)
        if not table.pk:
            table.pk = tuple(c.name for c in table.columns if c.pk)
        # "id uuid PK" tables have a single-column key; column-level `unique` notes:
        for line in block.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if (
                len(cells) >= 3
                and cells[2].endswith("unique")
                and cells[0] in {c.name for c in table.columns}
            ):
                table.uniques.append((cells[0],))
        tables[name] = table
    return tables


@dataclass
class Requirement:
    id: str
    file: str
    block: str
    verification: str
    pr: str


def requirements() -> list[Requirement]:
    """Every `#### HAT-` requirement block of the spec files 00-11."""
    found = []
    for path in sorted(SPEC_DIR.glob("[01][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        heads = list(re.finditer(r"^#### (HAT-[\w-]+):", text, re.MULTILINE))
        for index, head in enumerate(heads):
            end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
            nxt = re.search(r"^#{1,3} ", text[head.end() : end], re.MULTILINE)
            if nxt:
                end = head.end() + nxt.start()
            block = text[head.start() : end]
            ver = re.search(r"^- \*\*Verification:\*\* (.*)$", block, re.MULTILINE)
            pr = re.search(r"^- \*\*PR:\*\* (.*)$", block, re.MULTILINE)
            found.append(
                Requirement(
                    head.group(1),
                    path.name,
                    block,
                    ver.group(1) if ver else "",
                    pr.group(1) if pr else "",
                )
            )
    return found

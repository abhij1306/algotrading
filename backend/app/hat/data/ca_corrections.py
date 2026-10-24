"""The owner's reviewed list of stored corporate-action corrections (CA2): factors the price
gate shows wrong (a misparsed ratio, a missing leg, a demerger stored from a resulting
company's price where the market gap is the factor), applied as one batch.

The list is a TSV file in the admin folder, one correction per line, with columns
`op isin ex_date type factor new_ex_date note`, and optionally, in this order after them:

- `cash` (CA4): a dividend's rupees per share, on a `replace` or `add` line of type `dividend`
  with factor 1;
- `known_on` (CA5): the date the corrected value was public, which the stored row keeps
  (`correction_known`: never before the ex-date; without it a factor is not dated and its name
  stays factor-pending in a backtest, `ca_known`). A `replace` with the stored factor and a
  `known_on` dates a stored row.


- `replace`: the stored (isin, ex_date, type) row takes `factor` (and `cash`; `correct_action`);
- `delete`: the row is deleted (`correct_action`), e.g. a bonus of preference shares;
- `move`: the row is deleted at `ex_date` and stored again at `new_ex_date`, with `factor` when
  given, else its own (`correct_action`, then `add_action`);
- `add`: a missing leg is stored (`add_action`), e.g. the bonus of a bonus-and-split day, or a
  dividend with its `cash` the imports could not read (a review case).

`note` carries the reason and the bar evidence; it goes into the journal entry of each change
(`corporate_action.corrected`, actor user, via cli). A dry run (`apply_list(..., apply=False)`)
tries each line in a savepoint that is rolled back and reports what it would do or why it is
refused; an apply runs every line in the caller's transaction and refuses the whole list on any
line's refusal, so nothing is written unless all of it is.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import events
from ..errors import HatError
from ..models import HatCorporateAction, HatInstrument
from ..money import exact
from .corporate_actions import (
    EVENT_CORRECTED,
    PRICE_NEUTRAL_TYPES,
    ActionCorrectionError,
    Correction,
    check_correction,
    correct_action,
    correction_known,
    lock_reference_data,
)

SOURCE_OWNER = "owner"
OPS = ("replace", "delete", "move", "add")
COLUMNS = ("op", "isin", "ex_date", "type", "factor", "new_ex_date", "note")
CASH_COLUMN = "cash"
KNOWN_ON = "known_on"  # optional, after `cash` when both are given (CA5)
EXTRA_COLUMNS = ((), (CASH_COLUMN,), (KNOWN_ON,), (CASH_COLUMN, KNOWN_ON))
MAX_LINES = 500


class CorrectionListError(HatError):
    """The list cannot be read; `code` is stable and `line` names the line."""

    def __init__(self, code: str, line: int = 0) -> None:
        super().__init__(code)
        self.code = f"{code}@{line}" if line else code


@dataclass(frozen=True)
class Item:
    """One line of the list."""

    line: int
    op: str
    isin: str
    ex_date: date
    action_type: str
    factor: Decimal | None
    new_ex_date: date | None
    note: str
    cash: Decimal | None = None
    known_on: date | None = None


def _date(raw: str, line: int) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise CorrectionListError("bad_date", line) from None


def parse_list(text: str) -> list[Item]:
    """The list's lines (a header line first, `#` lines ignored). A line that does not read
    refuses the list: a factor where the op takes none or none where it needs one, a move
    without a new date, a bad date or op, a cash amount that is not positive or on an op other
    than `replace` or `add`."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = [(n, r) for n, r in enumerate(reader, start=1) if r and not r[0].startswith("#")]
    header = tuple(c.strip() for c in rows[0][1]) if rows else ()
    if header[: len(COLUMNS)] != COLUMNS or header[len(COLUMNS) :] not in EXTRA_COLUMNS:
        raise CorrectionListError("bad_header")
    if len(rows) - 1 > MAX_LINES:
        raise CorrectionListError("too_many_lines")
    items = []
    for line, row in rows[1:]:
        if len(row) != len(header):
            raise CorrectionListError("bad_columns", line)
        op, isin, ex, kind, factor_raw, new_ex, note, *rest = (c.strip() for c in row)
        extra = dict(zip(header[len(COLUMNS) :], rest, strict=True))
        cash_raw, known_on = extra.get(CASH_COLUMN, ""), extra.get(KNOWN_ON, "")
        if op not in OPS:
            raise CorrectionListError("bad_op", line)
        factor = exact(factor_raw, positive=True) if factor_raw else None
        if factor_raw and factor is None:
            raise CorrectionListError("bad_factor", line)
        needs = {"replace": True, "add": True, "delete": False}.get(op)
        if needs is not None and (factor is not None) != needs:
            raise CorrectionListError("bad_factor", line)
        if (op == "move") != bool(new_ex):
            raise CorrectionListError("bad_new_ex_date", line)
        cash = exact(cash_raw, positive=True) if cash_raw else None
        if cash_raw and (cash is None or op not in ("replace", "add")):
            raise CorrectionListError("bad_cash", line)
        items.append(
            Item(
                line,
                op,
                isin,
                _date(ex, line),
                kind,
                factor,
                _date(new_ex, line) if new_ex else None,
                note,
                cash,
                _date(known_on, line) if known_on else None,
            )
        )
    return items


def add_action(
    session: Session,
    *,
    isin: str,
    ex_date: date,
    action_type: str,
    factor: Decimal,
    note: str,
    chat_id: int,
    user_id: uuid.UUID,
    now: datetime,
    cash: Decimal | None = None,
    known_on: date | None = None,
) -> dict[str, object]:
    """Store one price-changing action the owner adds (a missing leg, the new date of a moved
    one), or a dividend with its `cash` (CA4), known on `correction_known`'s date (CA5), and
    journal it as `corporate_action.corrected` (before: absent). Refused like `correct_action`:
    `instrument_not_found`, `action_key_taken` (the key is stored), or `check_correction`'s codes
    (another price-neutral type, or a dividend without cash, is not added: it changes nothing)."""
    checked = check_correction(Correction(isin, ex_date, action_type, factor, note=note, cash=cash))
    if checked is None or (action_type in PRICE_NEUTRAL_TYPES and cash is None):
        raise ActionCorrectionError("bad_action_type")
    lock_reference_data(session)
    instrument = session.scalar(select(HatInstrument).where(HatInstrument.isin == isin))
    if instrument is None:
        raise ActionCorrectionError("instrument_not_found")
    taken = session.scalar(
        select(HatCorporateAction).where(
            HatCorporateAction.instrument_id == instrument.id,
            HatCorporateAction.ex_date == ex_date,
            HatCorporateAction.action_type == action_type,
        )
    )
    if taken is not None:
        raise ActionCorrectionError("action_key_taken")
    known = correction_known(ex_date, known_on, price_neutral=False)
    session.add(
        HatCorporateAction(
            instrument_id=instrument.id,
            ex_date=ex_date,
            action_type=action_type,
            adjustment_factor=checked[1],
            raw_text=note.strip()[:500],
            source=SOURCE_OWNER,
            cash_amount=cash,
            known_on=known.on,
            known_basis=known.basis,
        )
    )
    payload = {
        "chat_id": chat_id,
        "via": "cli",
        "isin": isin,
        "ex_date": ex_date.isoformat(),
        "before": {"type": action_type, "absent": True},
        "after": {
            "type": action_type,
            "factor": str(checked[1]),
            **({"cash": str(cash)} if cash is not None else {}),
            "added": True,
            "known_on": known.on.isoformat() if known.on else None,
        },
        "note": note.strip(),
    }
    events.record(
        session,
        aggregate_type="instrument",
        aggregate_id=instrument.id,
        event_type=EVENT_CORRECTED,
        actor="user",
        payload=payload,
        user_id=user_id,
        occurred_at=now,
    )
    session.flush()
    return {"symbol": instrument.symbol, **{k: v for k, v in payload.items() if k != "chat_id"}}


def _stored_factor(session: Session, item: Item) -> Decimal:
    row = session.scalar(
        select(HatCorporateAction)
        .join(HatInstrument, HatInstrument.id == HatCorporateAction.instrument_id)
        .where(
            HatInstrument.isin == item.isin,
            HatCorporateAction.ex_date == item.ex_date,
            HatCorporateAction.action_type == item.action_type,
        )
    )
    if row is None:
        raise ActionCorrectionError("action_not_found")
    return Decimal(row.adjustment_factor)


def _one(
    session: Session, item: Item, *, chat_id: int, user_id: uuid.UUID, now: datetime
) -> list[dict[str, object]]:
    def add(ex_date: date, factor: Decimal) -> dict[str, object]:
        return add_action(
            session,
            isin=item.isin,
            ex_date=ex_date,
            action_type=item.action_type,
            factor=factor,
            note=item.note,
            chat_id=chat_id,
            user_id=user_id,
            now=now,
            cash=item.cash if item.op == "add" else None,
            known_on=item.known_on,
        )

    def correct(factor: Decimal | None) -> dict[str, object]:
        cash = item.cash if item.op == "replace" else None
        fix = Correction(
            item.isin,
            item.ex_date,
            item.action_type,
            factor,
            note=item.note,
            cash=cash,
            known_on=item.known_on,
        )
        return correct_action(session, fix, chat_id=chat_id, user_id=user_id, now=now)

    if item.op == "add" and item.factor is not None:
        return [add(item.ex_date, item.factor)]
    if item.op == "move" and item.new_ex_date is not None:
        factor = item.factor or _stored_factor(session, item)
        return [correct(None), add(item.new_ex_date, factor)]
    return [correct(item.factor if item.op == "replace" else None)]


def apply_list(
    session: Session,
    items: Sequence[Item],
    *,
    apply: bool,
    chat_id: int,
    user_id: uuid.UUID,
    now: datetime,
) -> dict[str, object]:
    """Run the list: with `apply`, every line in the caller's transaction (a refused line
    refuses the list, `ActionCorrectionError` with its line); without, each line in a savepoint
    rolled back after it, reporting what it would do or its refusal code."""
    results: list[dict[str, object]] = []
    for item in items:
        if apply:
            try:
                done = _one(session, item, chat_id=chat_id, user_id=user_id, now=now)
            except ActionCorrectionError as exc:
                raise ActionCorrectionError(f"{exc.code}@{item.line}") from None
            results.append({"line": item.line, "op": item.op, "done": done})
            continue
        savepoint = session.begin_nested()
        try:
            done = _one(session, item, chat_id=chat_id, user_id=user_id, now=now)
            results.append({"line": item.line, "op": item.op, "would": done})
        except ActionCorrectionError as exc:
            results.append({"line": item.line, "op": item.op, "refused": exc.code})
        finally:
            savepoint.rollback()
    return {"applied": apply, "lines": len(items), "results": results}

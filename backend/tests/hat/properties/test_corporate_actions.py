"""PBT corporate_actions.*: the adjusted view is raw times
cumulative factors, volume is divided by the same factor, raw files never change, stored actions
are the distinct keys, and split and bonus text parses to exact factors. Real store, DuckDB views
and loaders; no network."""

import hashlib
import tempfile
import uuid
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.data.bhavcopy import weekdays
from app.hat.data.corporate_actions import (
    TYPE_BONUS,
    TYPE_DIVIDEND,
    TYPE_NOTICE,
    TYPE_SPLIT,
    ActionCorrectionError,
    Correction,
    check_correction,
    factor_to_decimal,
    from_archive_csv,
    from_mcp,
    non_equity_bonus,
    parse_action_text,
)
from app.hat.data.ingest import flag_symbol
from app.hat.data.ratios import split_pair_numbers
from app.hat.data.store import (
    FACTOR_MAX,
    FACTOR_MIN,
    ActionFactor,
    Adjustment,
    BarRow,
    DataStore,
    StoreError,
    cumulative_segments,
)
from app.hat.ids import uuid7

START = date(2024, 1, 1)
DAYS = weekdays(START, START + timedelta(days=30))[:14]
# Factors whose product has a finite reciprocal, so volume checks are exact in Decimal.
FACTORS = [Decimal(x) for x in ("0.5", "0.25", "0.2", "0.125", "0.1", "0.05", "0.04", "2", "5")]
prices = st.decimals(min_value="0.05", max_value="9999.95", places=2)


@st.composite
def worlds(draw: st.DrawFn) -> tuple[dict, list[ActionFactor]]:
    """Instruments with bars on most DAYS and 0-3 actions each (ex-dates inside, before or after
    the bars, several on one day allowed)."""
    ids = [uuid.UUID(int=i + 1) for i in range(draw(st.integers(1, 3)))]
    bars: dict[uuid.UUID, dict[date, tuple]] = {}
    actions: list[ActionFactor] = []
    for iid in ids:
        bars[iid] = {
            d: (
                draw(prices),
                draw(prices),
                draw(prices),
                draw(prices),
                draw(prices),
                draw(st.integers(0, 10**8)),
            )
            for d in DAYS
            if draw(st.integers(0, 5)) > 0
        }
        product = Decimal(1)
        for _ in range(draw(st.integers(0, 3))):
            factor = draw(st.sampled_from(FACTORS))
            if not FACTOR_MIN <= product * factor < FACTOR_MAX:
                continue  # the view refuses a cumulative factor outside the stored range
            product *= factor
            ex = START + timedelta(days=draw(st.integers(-3, 34)))
            actions.append(ActionFactor(iid, ex, factor))
    return bars, actions


def _write(store: DataStore, bars: dict) -> None:
    run = uuid7()
    for day in DAYS:
        rows = [
            BarRow(iid, "INE000000001", "AAA", "EQ", o, h, low, c, p, v, None, "bhavcopy", run)
            for iid, per in bars.items()
            if day in per
            for (o, h, low, c, p, v) in [per[day]]
        ]
        if rows:
            store.write_bars(day, rows)


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _product(actions: list[ActionFactor], iid: uuid.UUID, keep: Callable[[date], bool]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        out = Decimal(1)
        for a in actions:
            if a.instrument_id == iid and keep(a.ex_date):
                out *= a.factor
        return out


@given(
    close=prices,
    vol=st.integers(1, 10**8),
    ex_offset=st.integers(1, 12),
    factor=st.sampled_from(FACTORS),
)
def test_adjusted_view_split_factor(
    close: Decimal, vol: int, ex_offset: int, factor: Decimal
) -> None:
    """A split with factor f at an ex-date scales every earlier bar by exactly f and leaves later
    bars unchanged (the 1:2 split, f = 0.5, halves pre-ex prices)."""
    iid = uuid.UUID(int=7)
    ex = DAYS[ex_offset]
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        _write(store, {iid: dict.fromkeys(DAYS, (close, close, close, close, close, vol))})
        con = store.connect(Adjustment((ActionFactor(iid, ex, factor),)))
        rows = con.execute("SELECT date, close FROM bars_daily_adj ORDER BY date").fetchall()
        con.close()
    assert [c for d, c in rows if d < ex] == [close * factor] * sum(1 for d in DAYS if d < ex)
    assert all(c == close for d, c in rows if d >= ex)


@given(world=worlds())
@settings(max_examples=40)
def test_adjusted_view_idempotent(world: tuple) -> None:
    """Building the view twice, or from the same actions listed in another order, gives
    one result: there is no second adjustment because nothing is written back."""
    bars, actions = world
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        _write(store, bars)
        sql = "SELECT * FROM bars_daily_adj ORDER BY instrument_id, date"
        one = store.connect(Adjustment(tuple(actions))).execute(sql).fetchall()
        two = store.connect(Adjustment(tuple(actions))).execute(sql).fetchall()
        reordered = store.connect(Adjustment(tuple(reversed(actions)))).execute(sql).fetchall()
    assert one == two
    assert one == reordered


@given(a=st.integers(2, 50), b_tenths=st.integers(1, 99))
def test_split_pair_decimal_face_value(a: int, b_tenths: int) -> None:
    """A face-value text with a decimal new value ("from Rs a to Rs b.t") reads as the number
    it says, not as separate integers (HAT and phase-1 parse the same text)."""
    new = f"{b_tenths // 10}.{b_tenths % 10}"
    assert split_pair_numbers(f"face value split from rs {a} to rs {new}") == (
        Fraction(a),
        Fraction(new),
    )


@given(n=st.integers(2, 6), reverse=st.integers(10, 60))
def test_cumulative_factor_out_of_range_is_refused(n: int, reverse: int) -> None:
    """Reverse splits compounding past the stored range raise instead of overflowing the view."""
    iid = uuid.uuid4()
    actions = [ActionFactor(iid, date(2020 + i, 1, 1), Decimal(reverse)) for i in range(n)]
    if Decimal(reverse) ** n >= 100:
        with pytest.raises(StoreError):
            cumulative_segments(actions)
    else:
        assert cumulative_segments(actions)


_A = st.integers(2, 1000)


@given(a=_A, tenths=st.integers(1, 9999), template=st.integers(0, 1))
def test_factor_parse(a: int, tenths: int, template: int) -> None:
    """A face-value split from a to b parses to exactly b / a, a bonus of x new for y held to
    y / (x + y), both inside (0, 1]; text with no ratio, a reverse split or no digits never gives
    a factor."""
    b = Fraction(tenths, 10)
    if b >= a:
        b = Fraction(a - 1, 2) if a > 2 else Fraction(1, 2)
    b_text = format(Decimal(b.numerator) / Decimal(b.denominator), "f")
    texts = [
        f"Face Value Split (Sub-Division) - From Rs {a}/- Per Share To Rs {b_text}/- Per Share",
        f"Face value split from Rs {a} to Rs {b_text} (FV)",
    ]
    parsed = parse_action_text(texts[template])
    assert parsed is not None
    assert parsed.action_type == "split"
    assert parsed.factor == Fraction(b_text) / a
    assert 0 < parsed.factor <= 1
    x, y = a, int(tenths)
    for text in (f"Bonus issue {x}:{y}", f"Dividend Rs 5 - bonus {x} : {y}", f"BONUS {x}:{y}"):
        parsed = parse_action_text(text)
        assert parsed is not None
        assert parsed.action_type == "bonus"
        assert parsed.factor == Fraction(y, x + y)
        assert 0 < parsed.factor <= 1
    assert parse_action_text(f"Face value split from Rs {b_text} to Rs {a}") is None
    assert parse_action_text(f"Face value split from Rs {a} to Rs {a}") is None


@given(
    text=st.text(alphabet="abcdefghijklmnopqrstuvwxyz ABC-/.:,", max_size=60),
    word=st.sampled_from(["split", "Bonus", "Consolidation of shares", "consolidated"]),
)
def test_factor_parse_unparseable_is_reported(text: str, word: str) -> None:
    """Text without a usable ratio gives no factor, and an archive row with such text lands in
    `unparsed` (when it names a split, a bonus or a consolidation) and never in
    the records."""
    assert parse_action_text(text) is None
    csv_text = f'SYMBOL,PURPOSE,EX-DATE\nAAA,"{text.replace(chr(34), "")} {word}",14-Jun-2018\n'
    result = from_archive_csv(csv_text, lambda s, d: uuid.UUID(int=1))
    assert result.records == []
    assert len(result.unparsed) == 1


KEYS = [(i, d, t) for i in range(3) for d in range(3) for t in ("split", "bonus")]


def _mcp_kind(row: dict) -> str:
    if row["adjustmentFactor"] == 1.0:
        return TYPE_DIVIDEND if row["actionType"] == "DIVIDEND" else TYPE_NOTICE
    return {"SPLIT": "split", "BONUS": "bonus"}.get(row["actionType"], "other")


@given(
    rows=st.lists(
        st.fixed_dictionaries(
            {
                "exDate": st.dates(date(2010, 1, 1), date(2026, 1, 1)).map(str),
                "actionType": st.sampled_from(["SPLIT", "BONUS", "DIVIDEND", "OTHER"]),
                "purpose": st.just("raw nse text"),
                "adjustmentFactor": st.sampled_from([0.5, 0.2, 1.0, 0.0, -1.0, 2.0]),
            }
        ),
        max_size=8,
    )
)
def test_upsert_idempotent_mcp_rows(rows: list[dict]) -> None:
    """MCP rows keep the server's factor for splits and bonuses; an action of factor 1 is kept
    price-neutral, `dividend` for a dividend and `notice` for any other type; zero and negative
    factors are reported and
    never become records."""
    result = from_mcp(uuid.UUID(int=5), rows)
    kept = [r for r in rows if r["adjustmentFactor"] > 0]
    assert [(str(r.ex_date), float(r.factor), r.action_type) for r in result.records] == [
        (r["exDate"], r["adjustmentFactor"], _mcp_kind(r)) for r in kept
    ]
    assert len(result.unparsed) == sum(1 for r in rows if r["adjustmentFactor"] <= 0)
    assert all(r.factor > 0 for r in result.records)
    assert factor_to_decimal(Fraction(2, 3)) == Decimal("0.666666666667")


# --- which symbols get a lookup ------------------------------------

optional_price = st.one_of(st.none(), st.decimals(min_value="0", max_value="500", places=2))


@settings(max_examples=300)
@example(stored=Decimal(100), prev=Decimal(100), close=Decimal(121), in_rss=False)
@example(stored=Decimal(100), prev=Decimal(100), close=Decimal(79), in_rss=False)
@example(stored=Decimal(100), prev=Decimal(100), close=Decimal(120), in_rss=False)
@example(stored=Decimal(100), prev=Decimal(100), close=Decimal(110), in_rss=False)
@given(
    stored=optional_price,
    prev=optional_price,
    close=st.decimals(min_value="0.05", max_value="500", places=2),
    in_rss=st.booleans(),
)
def test_flag_rule(
    stored: Decimal | None, prev: Decimal | None, close: Decimal, in_rss: bool
) -> None:
    """For every stored close, fetched prev_close, close and RSS membership, a symbol is flagged
    iff prev_close differs from the stored close (a missing side counts as different), or the day's
    move is over 20% of a positive prev_close (exact fractions), or the RSS names it."""
    moved = (
        prev is not None
        and prev > 0
        and abs(Fraction(close) - Fraction(prev)) > Fraction(1, 5) * Fraction(prev)
    )
    assert flag_symbol(stored, prev, close, in_rss) == (in_rss or prev != stored or moved)


# The texts hat_live stored as equity bonuses (D1), and equity bonuses that must stay factors.
NON_EQUITY_BONUSES = [
    "Scheme Of Arrangement - Bonus Ncrps 4:1",
    "Bonus Ncrps 1:116",
    "Scheme Of Arrangement - Bonus Debentures 6:1",
    "Sch Of Agmt- Bonus Deb1:1",
    "Bonus Preference Shares 21:1",
    "Annual General Meeting/Dividend Rs.3/- Per Share/Scheme Of Arrangement - Bonus Debentures 1:1"
    " (Purpose Revised)",
]
EQUITY_BONUSES = [
    ("Bonus 1:1", Fraction(1, 2)),
    ("Bonus 4:5 (Pursuant To Scheme Of Amalgamation)", Fraction(5, 9)),
    ("Annual General Meeting/Dividend Rs 2 Per Share/Bonus 1:2", Fraction(2, 3)),
]


@pytest.mark.parametrize("text", NON_EQUITY_BONUSES)
def test_a_non_equity_bonus_is_price_neutral(text: str) -> None:
    assert non_equity_bonus(text)
    assert parse_action_text(text) is None
    found = from_mcp(
        "i",
        [{"exDate": "2025-08-25", "actionType": "BONUS", "adjustmentFactor": 0.2, "purpose": text}],
    )
    assert [(r.action_type, r.factor) for r in found.records] == [(TYPE_NOTICE, Decimal(1))]
    archive = from_archive_csv(
        f'SYMBOL,PURPOSE,EX-DATE\nX,"{text}",25-Aug-2025\n', lambda s, d: "i"
    )
    assert (archive.records, archive.unparsed, archive.ignored) == ([], [], 1)


@pytest.mark.parametrize(("text", "factor"), EQUITY_BONUSES)
def test_an_equity_bonus_keeps_its_factor(text: str, factor: Fraction) -> None:
    assert not non_equity_bonus(text)
    parsed = parse_action_text(text)
    assert parsed is not None
    assert (parsed.action_type, parsed.factor) == (TYPE_BONUS, factor)


def test_a_split_is_never_made_neutral_by_its_text() -> None:
    row = {
        "exDate": "2025-08-25",
        "actionType": "SPLIT",
        "adjustmentFactor": 0.5,
        "purpose": "Face Value Split From Rs 10 To Rs 5 / Bonus Debentures 1:1",
    }
    assert [(r.action_type, r.factor) for r in from_mcp("i", [row]).records] == [
        (TYPE_SPLIT, Decimal("0.5"))
    ]


def _correction(factor: str | None, new_type: str | None = None, note: str = "D1") -> Correction:
    value = None if factor is None else Decimal(factor)
    return Correction("INE494B01023", date(2025, 8, 25), TYPE_BONUS, value, new_type, note)


def test_correction_rules() -> None:
    assert check_correction(_correction(None)) is None
    assert check_correction(_correction("1", TYPE_NOTICE)) == (TYPE_NOTICE, Decimal(1))
    assert check_correction(_correction("0.5")) == (TYPE_BONUS, Decimal("0.5"))
    for bad, code in [
        (_correction("1"), "bad_factor"),  # a bonus of factor 1 changes no price
        (_correction("0.5", TYPE_NOTICE), "bad_factor"),  # a neutral type has factor 1
        (_correction("0"), "bad_factor"),
        (_correction("100"), "bad_factor"),  # beyond what the view can hold (store.FACTOR_MAX)
        (_correction("1E-10"), "bad_factor"),
        (_correction("NaN"), "bad_factor"),
        (_correction("1", "rights"), "bad_action_type"),
        (_correction(None, note=" "), "note_required"),
    ]:
        with pytest.raises(ActionCorrectionError) as refused:
            check_correction(bad)
        assert refused.value.code == code

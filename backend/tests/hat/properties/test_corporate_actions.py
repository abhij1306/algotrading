"""PBT corporate_actions.* (HAT-DATA-052..055, 094; HAT-DEC-B27): the adjusted view is raw times
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
from hat_ingest_support import TRADE_DAY, Quote, make_world, store_previous, symbols_of
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.data.bhavcopy import weekdays
from app.hat.data.corporate_actions import (
    TYPE_DIVIDEND,
    TYPE_NOTICE,
    ActionRecord,
    factor_to_decimal,
    from_archive_csv,
    from_mcp,
    load_actions,
    parse_action_text,
    stored_factors,
)
from app.hat.data.ingest import flag_symbol, run_ingest
from app.hat.data.ratios import split_pair_ints
from app.hat.data.store import (
    FACTOR_MAX,
    FACTOR_MIN,
    ActionFactor,
    BarRow,
    DataStore,
    StoreError,
    cumulative_segments,
)
from app.hat.ids import uuid7
from app.hat.models import HatCorporateAction, HatInstrument, HatMarket

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


@given(world=worlds())
@settings(max_examples=80)
def test_adjusted_view(world: tuple) -> None:
    """adj price(d) = raw x product of factors with ex_date > d (open, high, low, close);
    adj prev_close(d) = raw x the same product (NSE's ex-date prev_close is already adjusted,
    F14); after the last ex-date adj = raw; and
    the raw files are byte-identical after the view is created and read."""
    bars, actions = world
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        _write(store, bars)
        before = _hashes(store.root)
        con = store.connect(actions)
        rows = con.execute(
            "SELECT instrument_id, date, open, high, low, close, prev_close FROM bars_daily_adj"
        ).fetchall()
        con.close()
        assert _hashes(store.root) == before
    assert len(rows) == sum(len(v) for v in bars.values())
    for iid, day, *adjusted in rows:
        o, h, low, c, p, _ = bars[uuid.UUID(iid)][day]
        cum = _product(actions, uuid.UUID(iid), lambda ex, day=day: ex > day)
        with localcontext() as ctx:
            ctx.prec = 80
            assert adjusted == [o * cum, h * cum, low * cum, c * cum, p * cum]
            if all(a.ex_date <= day for a in actions if a.instrument_id == uuid.UUID(iid)):
                assert adjusted[:4] == [o, h, low, c]


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
        con = store.connect([ActionFactor(iid, ex, factor)])
        rows = con.execute("SELECT date, close FROM bars_daily_adj ORDER BY date").fetchall()
        con.close()
    assert [c for d, c in rows if d < ex] == [close * factor] * sum(1 for d in DAYS if d < ex)
    assert all(c == close for d, c in rows if d >= ex)


@given(world=worlds())
@settings(max_examples=80)
def test_adjusted_volume(world: tuple) -> None:
    """adj volume(d) = raw volume / product of factors with ex_date > d, and adj close x adj
    volume = raw close x raw volume, exactly."""
    bars, actions = world
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        _write(store, bars)
        con = store.connect(actions)
        rows = con.execute(
            "SELECT instrument_id, date, close, volume FROM bars_daily_adj"
        ).fetchall()
        con.close()
    for iid, day, adj_close, adj_volume in rows:
        o, h, low, c, p, volume = bars[uuid.UUID(iid)][day]
        cum = _product(actions, uuid.UUID(iid), lambda ex, day=day: ex > day)
        with localcontext() as ctx:
            ctx.prec = 80
            assert adj_volume == Decimal(volume) / cum
            assert adj_close * adj_volume == c * volume


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
        one = store.connect(actions).execute(sql).fetchall()
        two = store.connect(actions).execute(sql).fetchall()
        reordered = store.connect(list(reversed(actions))).execute(sql).fetchall()
    assert one == two
    assert one == reordered


@given(a=st.integers(2, 50), b_tenths=st.integers(1, 99))
def test_split_pair_decimal_face_value(a: int, b_tenths: int) -> None:
    """A face-value text with a decimal new value ("from Rs a to Rs b.t") reads as the number
    it says, not as separate integers (HAT and phase-1 parse the same text)."""
    new = f"{b_tenths // 10}.{b_tenths % 10}"
    assert split_pair_ints(f"face value split from rs {a} to rs {new}") == (
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
    `unparsed` (when it names a split, a bonus or a consolidation, HAT-EXEC-144) and never in
    the records."""
    assert parse_action_text(text) is None
    csv_text = f'SYMBOL,PURPOSE,EX-DATE\nAAA,"{text.replace(chr(34), "")} {word}",14-Jun-2018\n'
    result = from_archive_csv(csv_text, lambda s, d: uuid.UUID(int=1))
    assert result.records == []
    assert len(result.unparsed) == 1


KEYS = [(i, d, t) for i in range(3) for d in range(3) for t in ("split", "bonus")]


@st.composite
def load_plans(draw: st.DrawFn) -> list[list]:
    n = draw(st.integers(1, 14))
    recs = [
        (
            draw(st.sampled_from(KEYS)),
            draw(st.sampled_from(FACTORS)),
            draw(st.sampled_from(["mcp", "archive"])),
        )
        for _ in range(n)
    ]
    cuts = sorted(draw(st.sets(st.integers(0, n), max_size=3)) | {0, n})
    return [recs[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]


@given(plan=load_plans())
@settings(max_examples=100)
def test_upsert_idempotent(plan: list) -> None:
    """For every sequence of loads (MCP, archive fallback, repeats): the stored set is the set of
    distinct (instrument, ex_date, action_type) keys, a stored factor is the first one loaded and
    never changes, a different later factor is reported as a conflict, and replaying every load
    changes nothing."""
    engine = memory_engine(HatMarket, HatInstrument, HatCorporateAction)
    ids = [uuid.UUID(int=100 + i) for i in range(3)]

    def records(chunk: list) -> list[ActionRecord]:
        return [
            ActionRecord(ids[i], START + timedelta(days=d), t, f, f"text {t}", src)
            for (i, d, t), f, src in chunk
        ]

    expected: dict[tuple, tuple[Decimal, str]] = {}
    conflicts = reported = 0
    with Session(engine) as session:
        for chunk in plan:
            report = load_actions(session, records(chunk))
            for (i, d, t), f, src in chunk:
                key = (ids[i], START + timedelta(days=d), t)
                if key not in expected:
                    expected[key] = (f, src)
                elif expected[key][0] != f:
                    conflicts += 1
            assert report.created + report.unchanged + len(report.conflicts) == len(chunk)
            reported += len(report.conflicts)
        session.commit()
        assert reported == conflicts
        snapshot = {
            (r.instrument_id, r.ex_date, r.action_type): (Decimal(r.adjustment_factor), r.source)
            for r in session.scalars(select(HatCorporateAction))
        }
        assert snapshot == expected
        total = 0
        for chunk in plan:
            again = load_actions(session, records(chunk))
            assert again.created == 0
            total += len(again.conflicts)
        session.commit()
        assert total == sum(
            1
            for chunk in plan
            for (i, d, t), f, _ in chunk
            if expected[(ids[i], START + timedelta(days=d), t)][0] != f
        )
        assert {
            (r.instrument_id, r.ex_date, r.action_type): (Decimal(r.adjustment_factor), r.source)
            for r in session.scalars(select(HatCorporateAction))
        } == expected
        assert {(a.instrument_id, a.ex_date) for a in stored_factors(session)} == {
            (k[0], k[1]) for k in expected
        }


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
    price-neutral, `dividend` for a dividend and `notice` for any other type (PR 47: its
    ex-date is what the morning GTT check needs); zero and negative factors are reported and
    never become records."""
    result = from_mcp(uuid.UUID(int=5), rows)
    kept = [r for r in rows if r["adjustmentFactor"] > 0]
    assert [(str(r.ex_date), float(r.factor), r.action_type) for r in result.records] == [
        (r["exDate"], r["adjustmentFactor"], _mcp_kind(r)) for r in kept
    ]
    assert len(result.unparsed) == sum(1 for r in rows if r["adjustmentFactor"] <= 0)
    assert all(r.factor > 0 for r in result.records)
    assert factor_to_decimal(Fraction(2, 3)) == Decimal("0.666666666667")


# --- PR 7: which symbols get a lookup (HAT-DATA-050, 051) ------------------------------------

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


@settings(max_examples=15)
@given(
    n=st.integers(1, 12),
    data=st.data(),
)
def test_lookup_only_flagged(n: int, data: st.DataObject) -> None:
    """For every universe where some symbols have a changed prev_close, a big move or an RSS
    entry, the nightly ingest calls `get_corporate_actions` for exactly those symbols, once each,
    and for no other."""
    names = [s for s, _, _ in symbols_of(n)]
    changed = data.draw(st.sets(st.sampled_from(names)))
    moved = data.draw(st.sets(st.sampled_from(names)))
    rss = data.draw(st.sets(st.sampled_from(names)))
    with tempfile.TemporaryDirectory() as tmp:
        world = make_world(tmp, symbols_of(n))
        store_previous(world, dict.fromkeys(names, Decimal(100)), TRADE_DAY - timedelta(days=1))
        for symbol in names:
            world.nse.quotes[symbol] = Quote(
                close=Decimal(130 if symbol in moved else 100),
                prev_close=Decimal(99 if symbol in changed else 100),
            )
        world.nse.default = Quote()
        world.rss = set(rss)
        run_ingest(world.deps(), TRADE_DAY, uuid7())
        looked_up = [a["symbol"] for a in world.nse.tools("get_corporate_actions")]
    assert sorted(looked_up) == sorted(changed | moved | rss)

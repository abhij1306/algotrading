"""ISIN lineage (PR 74): the pure linking rules of `data/lineage.py` (same shares vs demerger vs
merger vs ambiguous), the chain factors and the adjusted view over a linked chain. Prices are
the real ones of hat_live where a case names a company."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.backtest.data import chain_industries
from app.hat.data.chains import (
    LINKED,
    REVIEW,
    Action,
    LineageError,
    adjustment,
    chain_factors,
    chain_members,
    chains,
)
from app.hat.data.lineage import (
    AMBIGUOUS,
    FACE_VALUE_CHANGE,
    ISIN_REISSUE,
    LINK,
    LONG_GAP,
    OWNER_DISPLACED,
    OWNER_LINK,
    OWNER_UNLINK,
    PREV_CLOSE_MISMATCH,
    PRICE_JUMP,
    RESTRUCTURED,
    SYMBOL_CHANGE,
    UNLINK,
    Bar,
    Decision,
    Span,
    SymbolChange,
    apply_overrides,
    candidates,
    decide_all,
    exchange_sessions,
    issuer,
    parse_name_changes,
    parse_symbol_changes,
    sessions_between,
)
from app.hat.data.lineage_review import (
    JUMP_ETF,
    JUMP_OTHER,
    JUMP_RIGHTS,
    JUMP_TICK,
    Jump,
    jump_counts,
    jump_kind,
)
from app.hat.data.lineage_store import link_changes
from app.hat.data.store import BarRow, DataStore
from app.hat.ids import uuid7

FIXTURES = Path(__file__).parents[1] / "fixtures"
D = Decimal


def _sessions(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSIONS = _sessions(date(2011, 1, 3), date(2026, 10, 23))


def _span(
    iid: str,
    isin: str,
    first: date,
    last: date,
    symbol: str,
    *,
    open_: str = "100",
    prev: str | None = "100",
    close: str = "100",
    last_symbol: str | None = None,
    tail: tuple[Bar, ...] = (),
) -> Span:
    """A span whose only edge bars are its first (open, at `close`) and last (at `close`)
    sessions, unless `tail` gives its last sessions."""
    previous = None if prev is None else D(prev)
    head = (Bar(first, D(open_), D(close)),)
    tail = tail or (Bar(last, D(close), D(close)),)
    return Span(iid, isin, first, last, symbol, last_symbol or symbol, previous, head, tail)


def _decide(
    spans: list[Span], actions: Sequence[Action] = (), changes: Sequence[SymbolChange] = ()
) -> list[Decision]:
    found = candidates(spans, SESSIONS, changes)
    by_id: dict[str, list[Action]] = {}
    for a in actions:
        by_id.setdefault(a.instrument_id, []).append(a)
    return decide_all(found, by_id)


# HDFCBANK 2019: the old ISIN traded its last session ex the 2:1 split (2019-09-19, close
# 1101.05); the new ISIN opened 1108 on 2019-09-20 with PREVCLOSE 1101.05.
HDFC_OLD = _span(
    "b2", "INE040A01026", date(2011, 7, 15), date(2019, 9, 19), "HDFCBANK", close="1101.05"
)
HDFC_NEW = _span(
    "b3",
    "INE040A01034",
    date(2019, 9, 20),
    date(2026, 10, 23),
    "HDFCBANK",
    open_="1108",
    prev="1101.05",
    close="980",
)
HDFC_SPLIT = Action("b3", date(2019, 9, 19), "split", D("0.5"), "Face Value Split")


def test_an_fv_split_with_a_new_isin_is_linked() -> None:
    [d] = _decide([HDFC_OLD, HDFC_NEW], [HDFC_SPLIT])
    assert (d.old_id, d.new_id, d.status, d.reason) == ("b2", "b3", LINKED, FACE_VALUE_CHANGE)
    assert d.effective == date(2019, 9, 20)
    assert d.evidence["gap_factor"] == "1"  # the ex-date was the old ISIN's own last day


def test_tatamotors_2011_split_is_linked() -> None:
    # INE155A01014 last traded 2011-09-12 ex the 10:2 split at 146.35; INE155A01022 opened
    # 147.30 on 2011-09-13 with PREVCLOSE 146.35. The split is stored under the new ISIN only.
    old = _span(
        "t1", "INE155A01014", date(2011, 6, 22), date(2011, 9, 12), "TATAMOTORS", close="146.35"
    )
    new = _span(
        "t2",
        "INE155A01022",
        date(2011, 9, 13),
        date(2026, 10, 23),
        "TATAMOTORS",
        open_="147.30",
        prev="146.35",
        last_symbol="TMPV",
    )
    split = Action("t2", date(2011, 9, 12), "split", D("0.2"), "Face Value Split")
    [d] = _decide([old, new], [split])
    assert (d.status, d.reason) == (LINKED, FACE_VALUE_CHANGE)


def test_a_split_on_the_new_isins_first_day_uses_its_factor() -> None:
    # NESTLEIND 2024: PREVCLOSE carries the old raw close, the open is a tenth of it.
    old = _span(
        "n1", "INE239A01016", date(2011, 6, 22), date(2024, 1, 4), "NESTLEIND", close="27116.40"
    )
    new = _span(
        "n2",
        "INE239A01024",
        date(2024, 1, 5),
        date(2026, 10, 23),
        "NESTLEIND",
        open_="2754",
        prev="27116.40",
    )
    split = Action("n1", date(2024, 1, 5), "split", D("0.1"), "Face Value Split")
    [d] = _decide([old, new], [split])
    assert (d.status, d.reason, d.evidence["gap_factor"]) == (LINKED, FACE_VALUE_CHANGE, "0.1")
    [missing] = _decide([old, new])  # without the stored factor the open is a 90% crash
    assert (missing.status, missing.reason) == (REVIEW, PRICE_JUMP)


def test_an_unstored_action_on_the_old_isins_last_day_is_never_linked() -> None:
    # GOLDBEES 2019: the old ISIN's last session (12-19) was ex a 1:100 unit split HAT has no
    # factor for; the change itself is seamless (33.55 -> open 33.80), the day before is not.
    tail = (
        Bar(date(2019, 12, 18), D("3370.10"), D("3359.60")),
        Bar(date(2019, 12, 19), D("30.20"), D("33.55")),
    )
    old = _span(
        "g1",
        "INF732E01102",
        date(2011, 6, 22),
        date(2019, 12, 19),
        "GOLDBEES",
        close="33.55",
        tail=tail,
    )
    new = _span(
        "g2",
        "INF204KB17I5",
        date(2019, 12, 20),
        date(2026, 10, 23),
        "GOLDBEES",
        open_="33.80",
        prev="33.55",
    )
    [d] = _decide([old, new])
    assert (d.status, d.reason) == (REVIEW, PRICE_JUMP)
    assert d.evidence["edge_move"].startswith("2019-12-18 -> 2019-12-19")
    split = Action("g2", date(2019, 12, 19), "split", D("0.01"))
    assert _decide([old, new], [split])[0].status == LINKED  # with the factor it is seamless


def test_a_pure_reissue_is_linked() -> None:
    old = _span("r1", "INE0000000A1", date(2020, 1, 1), date(2020, 6, 1), "ABC")
    new = _span("r2", "INE0000000A2", date(2020, 6, 2), date(2021, 1, 1), "ABC", open_="103")
    [d] = _decide([old, new])
    assert (d.status, d.reason) == (LINKED, ISIN_REISSUE)


def test_demerger_child_is_never_a_candidate() -> None:
    # Tata Motors 2025: the parent keeps INE155A01022 (renamed TMPV) and goes on trading; the
    # CV business lists as TMCV on a new ISIN with its own history.
    parent = _span(
        "t2",
        "INE155A01022",
        date(2011, 9, 13),
        date(2026, 10, 23),
        "TATAMOTORS",
        last_symbol="TMPV",
    )
    child = _span(
        "cv",
        "INE1TAE01010",
        date(2025, 11, 12),
        date(2026, 10, 23),
        "TMCV",
        open_="335",
        prev="260.75",
    )
    changes = parse_symbol_changes((FIXTURES / "nse_symbolchange.csv").read_text("utf-8"))
    assert candidates([parent, child], SESSIONS, changes) == []


def test_merged_away_company_is_never_linked() -> None:
    # HDFC Ltd's history ends at the merger (2023-07-12); HDFCBANK keeps its own ISIN.
    hdfc = _span("h", "INE001A01036", date(2011, 6, 22), date(2023, 7, 12), "HDFC", close="2700")
    bank = _span("b3", "INE040A01034", date(2019, 9, 20), date(2026, 10, 23), "HDFCBANK")
    assert candidates([hdfc, bank], SESSIONS, []) == []
    # a new ISIN taking over a merged-away symbol: NSE did not carry the old close
    taker = _span(
        "x",
        "INE999X01010",
        date(2023, 7, 13),
        date(2026, 10, 23),
        "HDFC",
        open_="1650",
        prev="1640",
    )
    [d] = _decide([hdfc, taker])
    assert (d.status, d.reason) == (REVIEW, PREV_CLOSE_MISMATCH)


def test_restructuring_text_near_the_change_is_never_linked() -> None:
    old = _span("s1", "INE0000000B1", date(2020, 1, 1), date(2020, 6, 1), "XYZ")
    new = _span("s2", "INE0000000B2", date(2020, 6, 2), date(2021, 1, 1), "XYZ")
    scheme = Action("s1", date(2020, 5, 20), "notice", D(1), "Scheme Of Arrangement")
    [d] = _decide([old, new], [scheme])
    assert (d.status, d.reason) == (REVIEW, RESTRUCTURED)


def test_two_successors_make_both_ambiguous() -> None:
    old = _span("a0", "INE0000000C0", date(2020, 1, 1), date(2020, 6, 1), "DUP")
    one = _span("a1", "INE0000000C1", date(2020, 6, 2), date(2021, 1, 1), "DUP")
    two = _span("a2", "INE0000000C2", date(2020, 6, 3), date(2021, 1, 1), "DUP")
    decisions = _decide([old, one, two])
    assert {(d.new_id, d.status, d.reason) for d in decisions} == {
        ("a1", REVIEW, AMBIGUOUS),
        ("a2", REVIEW, AMBIGUOUS),
    }


def test_suspension_gap_bounds() -> None:
    old = _span("g1", "INE0000000D1", date(2020, 1, 1), date(2020, 6, 1), "GAP")
    near = _span("g2", "INE0000000D2", date(2020, 6, 9), date(2021, 1, 1), "GAP")  # 5 between
    late = _span("g2", "INE0000000D2", date(2020, 6, 10), date(2021, 1, 1), "GAP")  # 6
    gone = _span("g2", "INE0000000D2", date(2020, 8, 3), date(2021, 1, 1), "GAP")  # 44
    assert sessions_between(SESSIONS, old.last_day, near.first_day) == 5
    assert _decide([old, near])[0].status == LINKED
    assert (_decide([old, late])[0].status, _decide([old, late])[0].reason) == (REVIEW, LONG_GAP)
    assert _decide([old, gone]) == []


def test_a_missing_partition_never_shortens_the_gap() -> None:
    """The gap counts exchange sessions, not stored partitions: with the six sessions between
    the two ISINs missing from the store, the gap is still six (review), a known holiday is not
    a session and a special Saturday session is."""
    old = _span("m1", "INE0000000M1", date(2020, 1, 1), date(2020, 6, 1), "HOLE")
    new = _span("m2", "INE0000000M2", date(2020, 6, 10), date(2021, 1, 1), "HOLE")
    stored = [d for d in SESSIONS if not date(2020, 6, 1) < d < date(2020, 6, 10)]
    assert sessions_between(stored, old.last_day, new.first_day) == 0  # the store alone: a link
    sessions = exchange_sessions(stored, closed=set(), special=set())
    assert sessions_between(sessions, old.last_day, new.first_day) == 6
    [decision] = decide_all(candidates([old, new], sessions, []), {})
    assert (decision.status, decision.reason) == (REVIEW, LONG_GAP)
    holiday, saturday = date(2020, 6, 3), date(2020, 6, 6)
    sessions = exchange_sessions(stored, closed={holiday}, special={saturday})
    assert holiday not in sessions
    assert saturday in sessions
    assert sessions_between(sessions, old.last_day, new.first_day) == 6
    assert exchange_sessions([], closed=set(), special={saturday}) == []


def test_symbol_change_needs_a_record() -> None:
    old = _span("y1", "INE0000000E1", date(2020, 1, 1), date(2020, 6, 1), "OLDSYM")
    new = _span("y2", "INE0000000E2", date(2020, 6, 2), date(2021, 1, 1), "NEWSYM")
    assert _decide([old, new]) == []
    [d] = _decide([old, new], changes=[SymbolChange("OLDSYM", "NEWSYM", date(2020, 6, 2))])
    assert (d.status, d.reason) == (LINKED, SYMBOL_CHANGE)
    far = SymbolChange("OLDSYM", "NEWSYM", date(2019, 1, 1))
    assert _decide([old, new], changes=[far]) == []


def test_chain_applies_each_action_once() -> None:
    # Q42: HDFCBANK's three ISINs each hold a copy of the 2011 split, 2019 split, 2025 bonus.
    copies = [
        Action(iid, ex, kind, D(f))
        for iid in ("b1", "b2", "b3")
        for ex, kind, f in (
            (date(2011, 7, 14), "split", "0.2"),
            (date(2019, 9, 19), "split", "0.5"),
            (date(2025, 8, 26), "bonus", "0.5"),
        )
    ]
    adjust = adjustment(copies, [("b1", "b2"), ("b2", "b3")])
    assert adjust.lineage == {"b1": "b3", "b2": "b3"}
    assert sorted((a.instrument_id, a.ex_date, a.factor) for a in adjust.factors) == [
        ("b3", date(2011, 7, 14), D("0.2")),
        ("b3", date(2019, 9, 19), D("0.5")),
        ("b3", date(2025, 8, 26), D("0.5")),
    ]
    # unlinked, each ISIN keeps its own copies under its own id
    assert len(adjustment(copies, []).factors) == 9


def test_disagreeing_copies_keep_the_current_isins_and_are_reported() -> None:
    heads = chains([("o", "n")])
    actions = [
        Action("o", date(2020, 1, 1), "bonus", D("0.5")),
        Action("n", date(2020, 1, 1), "bonus", D("0.25")),
    ]
    factors, conflicts = chain_factors(actions, heads)
    assert [(f.instrument_id, f.factor) for f in factors] == [("n", D("0.25"))]
    assert conflicts == [("n", date(2020, 1, 1), "bonus")]


def test_a_chain_never_forks() -> None:
    with pytest.raises(LineageError):
        chains([("a", "b"), ("a", "c")])
    with pytest.raises(LineageError):
        chains([("a", "c"), ("b", "c")])


def test_owner_overrides_win_over_the_rules() -> None:
    day = date(2020, 1, 1)
    rules = [
        Decision("a", "b", day, REVIEW, PRICE_JUMP, {"old_isin": "A"}),
        Decision("c", "d", day, LINKED, FACE_VALUE_CHANGE),
        Decision("x", "b", day, LINKED, ISIN_REISSUE),  # shares b with the owner's link
        Decision("e", "f", day, LINKED, ISIN_REISSUE),
    ]
    out = apply_overrides(rules, {("a", "b"): LINK, ("c", "d"): UNLINK, ("q", "r"): LINK})
    assert [(d.old_id, d.status, d.reason, d.evidence.get("rules")) for d in out] == [
        ("a", LINKED, OWNER_LINK, "review:price_jump"),
        ("c", REVIEW, OWNER_UNLINK, "linked:face_value_change"),
        ("x", REVIEW, OWNER_DISPLACED, "linked:isin_reissue"),
        ("e", LINKED, ISIN_REISSUE, None),
    ]
    assert out[0].evidence["old_isin"] == "A"
    chains((d.old_id, d.new_id) for d in out if d.status == LINKED)  # no fork
    # applied again (to the stored rows), the rules' verdict is the first one's
    again = apply_overrides(out, {("a", "b"): UNLINK})
    assert (again[0].reason, again[0].evidence["rules"]) == (OWNER_UNLINK, "review:price_jump")


def test_jump_kinds() -> None:
    # a one-tick move on a penny stock (VISESHINFO 2019), past 25% and no action
    assert jump_kind("INE861A01058", D("0.10"), D("0.05")) == JUMP_TICK
    assert jump_kind("INE861A01058", D("0.15"), D("0.25")) == JUMP_TICK  # two ticks
    assert jump_kind("INE861A01058", D("0.15"), D("0.30")) == JUMP_OTHER
    assert jump_kind("INE885A01024", D("424.90"), D("213.30")) == JUMP_OTHER  # a split
    assert jump_kind("INE281A20018", D("0.10"), D("0.05")) == JUMP_RIGHTS  # -RE, type 20
    assert jump_kind("INF846K01347", D("4303.20"), D("43.65")) == JUMP_ETF
    assert jump_kind("INE861A01058", None, D("0.05")) == JUMP_OTHER  # raw price unknown


def test_jump_counts_per_year_and_kind() -> None:
    def jump(day: date, isin: str, before: str, after: str) -> Jump:
        return Jump(
            "i", day - timedelta(days=1), day, D(before), D(after), isin, D(before), D(after)
        )

    years, kinds = jump_counts(
        [
            jump(date(2019, 5, 10), "INE861A01058", "0.10", "0.05"),
            jump(date(2012, 9, 25), "INE885A01024", "424.90", "213.30"),
            jump(date(2019, 5, 13), "INE861A01058", "0.05", "0.10"),
        ]
    )
    assert years == {2012: 1, 2019: 2}
    assert kinds == {JUMP_OTHER: 1, JUMP_TICK: 2, JUMP_RIGHTS: 0, JUMP_ETF: 0}


def test_nse_lists_parse() -> None:
    changes = parse_symbol_changes((FIXTURES / "nse_symbolchange.csv").read_text("utf-8"))
    assert SymbolChange("TATAMOTORS", "TMPV", date(2025, 10, 24)) in changes
    assert all(c.old != "OLDX" for c in changes)  # an unreadable date is skipped
    names = parse_name_changes((FIXTURES / "nse_namechange.csv").read_text("utf-8"))
    assert [(n.symbol, n.effective) for n in names if n.symbol == "TMPV"] == [
        ("TMPV", date(2003, 11, 24)),
        ("TMPV", date(2025, 10, 24)),
    ]


def test_adjusted_view_reads_a_linked_chain_as_one_series() -> None:
    old, new = uuid.UUID(int=1), uuid.UUID(int=2)
    days = [date(2019, 9, 18), date(2019, 9, 19), date(2019, 9, 20), date(2019, 9, 23)]
    raw = {  # HDFCBANK across the 2019 change: the split is ex on the old ISIN's last day
        days[0]: (old, "INE040A01026", "2187.75"),
        days[1]: (old, "INE040A01026", "1101.05"),
        days[2]: (new, "INE040A01034", "1199.60"),
        days[3]: (new, "INE040A01034", "1257.25"),
    }
    split = Action(str(new), days[1], "split", D("0.5"))
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        for day, (iid, isin, close) in raw.items():
            px = D(close)
            row = BarRow(
                iid, isin, "HDFCBANK", "EQ", px, px, px, px, px, 1, None, "bhavcopy", uuid7()
            )
            store.write_bars(day, [row])
        with store.connect(adjustment([split], [(str(old), str(new))])) as con:
            rows = con.execute(
                "SELECT instrument_id, date, close FROM bars_daily ORDER BY date"
            ).fetchall()
    assert {r[0] for r in rows} == {str(new)}
    assert [r[2] for r in rows] == [D("1093.875"), D("1101.05"), D("1199.60"), D("1257.25")]


def test_industries_follow_the_chain() -> None:
    """A classification stored under a linked older ISIN counts for its chain (the id the
    backtest knows the instrument by); one under an unlinked ISIN does not."""
    old, mid, head, other = (uuid.UUID(int=i) for i in (1, 2, 3, 4))
    lineage = {str(old): str(head), str(mid): str(head)}
    rows = [
        (head, date(2020, 1, 1), "Financial Services"),
        (old, date(2010, 1, 1), "Banks"),
        (other, date(2010, 1, 1), "Housing Finance"),
    ]
    assert chain_industries(rows, [head], lineage) == {
        str(head): [(date(2010, 1, 1), "Banks"), (date(2020, 1, 1), "Financial Services")]
    }
    assert chain_industries(rows, [head], {}) == {
        str(head): [(date(2020, 1, 1), "Financial Services")]
    }


def test_chain_members() -> None:
    """Each instrument of a link maps to its whole chain, whatever order the links come in."""
    members = chain_members([("c", "d"), ("a", "b"), ("b", "c"), ("x", "y")])
    assert members["a"] == members["d"] == frozenset("abcd")
    assert members["y"] == frozenset("xy")
    assert "z" not in members


def test_link_changes_lists_only_pairs_that_gain_or_lose_a_link() -> None:
    """A link created, removed or demoted is journalled; a review case appearing, a link kept or
    a review case dropped changes no series and is not."""
    before = {("a", "b"): LINKED, ("c", "d"): LINKED, ("e", "f"): REVIEW, ("g", "h"): LINKED}
    after = {("a", "b"): LINKED, ("c", "d"): REVIEW, ("i", "j"): LINKED, ("k", "l"): REVIEW}
    assert link_changes(before, after) == [
        (("c", "d"), LINKED, REVIEW),
        (("g", "h"), LINKED, None),
        (("i", "j"), None, LINKED),
    ]
    assert link_changes(after, after) == []


def test_issuer_is_the_equity_isin_stem() -> None:
    # SUMEETINDS INE235C01010 ended 2024-10-17, INE235C01028 trades from 2025-06-19
    assert issuer("INE235C01010") == issuer("ine235c01028") == "INE235C01"
    for other in ("INF732E01102", "IN9155A01020", "INE235C02018", "INE235C0101"):
        assert issuer(other) == ""


def test_a_relisting_of_the_same_issuer_after_a_long_gap_is_for_review() -> None:
    # SUMEETINDS: INE235C01010 last traded 2024-10-17; INE235C01028 lists 2025-06-19 under
    # the same ticker, far beyond any rule's gap: the owner's to review, never linked by rule
    old = _span("s1", "INE235C01010", date(2020, 1, 1), date(2020, 3, 2), "SUMEETINDS")
    new = _span("s2", "INE235C01028", date(2020, 9, 1), date(2020, 11, 2), "SUMEETX")
    later = _span("s3", "INE235C01036", date(2020, 12, 1), date(2021, 1, 1), "SUMEETY")
    [d, nxt] = _decide([old, new, later])
    assert (d.old_id, d.new_id, d.status, d.reason) == ("s1", "s2", REVIEW, LONG_GAP)
    assert (nxt.old_id, nxt.new_id) == ("s2", "s3")  # its next ISIN only, not every later one

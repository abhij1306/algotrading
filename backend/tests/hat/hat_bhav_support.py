"""Builders for the verification: tiny bhavcopy zips in both NSE formats, bar rows and fake
fetchers. Everything is generated in memory; no file here is a download."""

import io
import zipfile
from datetime import date
from decimal import Decimal

from hypothesis import strategies as st

from app.hat.data.bhavcopy import BhavRow
from app.hat.data.store import BarRow

MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
OLD_HEADER = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,"
    "ISIN,"
)
UDIFF_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
    "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
    "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)

ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALNUM = ALPHA + "0123456789"
isins = st.builds(
    lambda body, check: "INE" + body + str(check),
    st.text(alphabet=ALNUM, min_size=8, max_size=8),
    st.integers(0, 9),
)
symbols = st.text(alphabet=ALPHA + "&-", min_size=1, max_size=10).filter(lambda s: s[0] in ALPHA)
prices = st.decimals(min_value="0.05", max_value="99999.95", places=2)
cents = st.decimals(min_value="0", max_value="999999999.99", places=2)
series = st.sampled_from(["EQ", "BE", "BZ"])


@st.composite
def bhav_rows(draw: st.DrawFn, day: date, min_size: int = 1, max_size: int = 8) -> list[BhavRow]:
    """Security-days of distinct ISINs (a file holds one row per ISIN once parsed); a share of
    them without a traded value."""
    keys = draw(
        st.lists(
            st.tuples(isins, series),
            min_size=min_size,
            max_size=max_size,
            unique_by=lambda k: k[0],
        )
    )
    rows = []
    for isin, ser in keys:
        # a bar that passes the row check: low <= open, close <= high
        low, o, c, h = sorted(draw(prices) for _ in range(4))
        if draw(st.booleans()):
            o, c = c, o
        prev = draw(prices)
        rows.append(
            BhavRow(
                day,
                isin,
                ser,
                draw(symbols),
                o,
                h,
                low,
                c,
                prev,
                draw(st.integers(0, 10**9)),
                draw(st.one_of(st.none(), cents)),
            )
        )
    return rows


def _zip(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def _f(value: Decimal | int | None) -> str:
    return "" if value is None else format(value, "f")


def render_old(day: date, rows: list[BhavRow], other_series: int = 0) -> bytes:
    """The old `cmDDMMMYYYYbhav.csv` layout (the column list is `[A]`, see )."""
    stamp = f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"
    lines = [OLD_HEADER]
    for r in rows:
        lines.append(
            f"{r.symbol},{r.series},{_f(r.open)},{_f(r.high)},{_f(r.low)},{_f(r.close)},"
            f"{_f(r.close)},{_f(r.prev_close)},{r.volume},{_f(r.traded_value)},{stamp},10,"
            f"{r.isin},"
        )
    for i in range(other_series):
        lines.append(f"GOI{i},GS,100,100,100,100,100,100,1,100,{stamp},1,IN00000000{i % 10},")
    return _zip("cmbhav.csv", "\n".join(lines) + "\n")


def render_udiff(day: date, rows: list[BhavRow], other_series: int = 0) -> bytes:
    lines = [UDIFF_HEADER]

    def one(
        sym: str,
        ser: str,
        isin: str,
        o: Decimal | int,
        h: Decimal | int,
        low: Decimal | int,
        c: Decimal | int,
        prev: Decimal | int,
        vol: int,
        tv: Decimal | int | None,
    ) -> str:
        return (
            f"{day.isoformat()},{day.isoformat()},CM,NSE,STK,1,{isin},{sym},{ser},,,,,{sym} NAME,"
            f"{_f(o)},{_f(h)},{_f(low)},{_f(c)},{_f(c)},{_f(prev)},,,,,{vol},{_f(tv)},10,F1,1,,,,,"
        )

    for r in rows:
        lines.append(
            one(
                r.symbol,
                r.series,
                r.isin,
                r.open,
                r.high,
                r.low,
                r.close,
                r.prev_close,
                r.volume,
                r.traded_value,
            )
        )
    for i in range(other_series):
        lines.append(one(f"GOI{i}", "GS", f"IN00000000{i % 10}", 1, 1, 1, 1, 1, 1, 1))
    return _zip("udiff.csv", "\n".join(lines) + "\n")


def bar(
    instrument_id: object,
    run_id: object,
    close: str = "100.00",
    *,
    volume: int = 1000,
    source: str = "bhavcopy",
    traded: str | None = "100000.00",
) -> BarRow:
    c = Decimal(close)
    return BarRow(
        instrument_id,
        "INE000000001",
        "AAA",
        "EQ",
        c,
        c,
        c,
        c,
        c,
        volume,
        None if traded is None else Decimal(traded),
        source,
        run_id,
    )

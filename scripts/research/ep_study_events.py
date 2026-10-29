"""The EP study's gap events (research/ep-study `01_build_panel.py`, `common.load_panel`,
`02_events.py`) rebuilt over 2022-2024 from the same stores, so the engine's EP candidates can
be reconciled with them event by event (`s2_ep_parity.py reconcile`). The study's cache is not
kept; this module is its definition, transcribed:

- prices: the adjusted `bars_daily` view (`DataStore.connect(stored_adjustment)`: splits,
  bonuses, rights, demergers and dividends, ISIN chains linked), joined to the raw bars on
  (ISIN, series, date); rows grouped by the chain id;
- member: a raw instrument id inside a NIFTY 500 interval of `hat_index_member` (from <= d < to);
- ADV20: the mean raw traded value of the previous 20 rows (at least 15), at least Rs 10 cr;
- 63-session history (`ret63` defined: at least 64 earlier rows), a positive NSE prev close;
- not a corporate-action ex-date (|(close/prev_close) / (adjusted day return) - 1| > 0.5%);
- gap: adjusted open over the previous row's adjusted close, minus 1; an event at >= `min_gap`,
  not frozen all day (high = low);
- earnings `earn`: a non-estimated `hat_financial_result.public_at` whose first session opening
  (09:15 IST) at or after it is the day; `earn_bm`: a results board meeting (the study's
  `board_meetings_*.csv` files, `board_meetings`) on the session or the one before. The study's
  `E` is either.
"""

from __future__ import annotations

import glob
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.hat.data.chains import stored_adjustment
from app.hat.data.store import DataStore
from app.hat.models import HatFinancialResult, HatIndexMember

IST = ZoneInfo("Asia/Kolkata")
LIQ = 1e8  # Rs 10 cr
LOAD_FROM = date(2021, 1, 1)  # history for ADV20 and the 63-session return before 2022


def _bars(store: DataStore, session: Any, start: date, end: date) -> pd.DataFrame:  # noqa: ANN401
    con = store.connect(stored_adjustment(session))
    raw = con.execute(
        """
        select cast(date as date) d, instrument_id iid, isin, symbol, series,
               cast(open as double) o, cast(high as double) h, cast(low as double) l,
               cast(close as double) c, cast(prev_close as double) pc,
               cast(traded_value as double) tv
        from bars_daily_raw
        where cast(date as date) between ? and ? and series in ('EQ', 'BE')
        """,
        [LOAD_FROM, end],
    ).df()
    adj = con.execute(
        """
        select instrument_id cid, isin, series, cast(date as date) d,
               cast(open as double) ao, cast(close as double) ac
        from bars_daily
        where cast(date as date) between ? and ? and series in ('EQ', 'BE')
        """,
        [LOAD_FROM, end],
    ).df()
    con.close()
    raw = raw.sort_values(["iid", "d"]).drop_duplicates(["iid", "d"], keep="last")
    bars = raw.merge(adj, on=["isin", "series", "d"], how="inner")
    bars["d"] = pd.to_datetime(bars.d)
    return bars[bars.d <= pd.Timestamp(end)]


def _members(session: Any) -> pd.DataFrame:  # noqa: ANN401
    rows = session.execute(
        select(
            HatIndexMember.instrument_id,
            HatIndexMember.effective_from,
            HatIndexMember.effective_to,
        ).where(HatIndexMember.index_name == "NIFTY 500")
    ).all()
    mem = pd.DataFrame([(str(i), f, t) for i, f, t in rows], columns=["iid", "f", "t"])
    mem["f"] = pd.to_datetime(mem.f)
    mem["t"] = pd.to_datetime(mem.t).fillna(pd.Timestamp("2100-01-01"))
    return mem


def _filings(session: Any, end: date) -> pd.DataFrame:  # noqa: ANN401
    r = HatFinancialResult
    rows = session.execute(
        select(r.instrument_id, r.public_at).where(
            r.public_at <= datetime.combine(end, time(23, 59, 59), IST),
            r.public_at_estimated.is_not(True),
        )
    ).all()
    res = pd.DataFrame([(str(i), p) for i, p in rows], columns=["iid", "public_at"])
    res["pa"] = (
        pd.to_datetime(res.public_at, utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    )
    return res


def first_session_open_after(sessions: np.ndarray, at: np.ndarray) -> np.ndarray:
    """The index of the first session whose 09:15 open is at or after each instant (the study's
    earnings day); `len(sessions)` when there is none."""
    opens = pd.to_datetime(sessions) + pd.Timedelta(hours=9, minutes=15)
    return np.searchsorted(opens.values, at, side="left")


def study_events(
    store: DataStore,
    session: Any,  # noqa: ANN401
    start: date,
    end: date,
    board_meetings: Path,
    min_gap: float = 0.06,
) -> pd.DataFrame:
    """The study's eligible gap days of `start..end` at `min_gap` or more, one row each, with
    the study's flags; and the eligibility columns of every day so a missing event can be
    explained."""
    p = _bars(store, session, start, end)
    mem = _members(session)
    m = p[["iid", "d"]].reset_index().merge(mem, on="iid")
    m = m[(m.d >= m.f) & (m.d < m.t)]
    p["member"] = False
    p.loc[m["index"].unique(), "member"] = True
    p = p.sort_values(["cid", "d"]).drop_duplicates(["cid", "d"], keep="last")
    p = p.reset_index(drop=True)
    g = p.groupby("cid", sort=False)
    p["tpos"] = g.cumcount()
    p["adv20"] = g.tv.transform(lambda s: s.shift(1).rolling(20, min_periods=15).mean())
    p["prev_ac"] = g.ac.shift(1)
    p["ret63_ok"] = g.ac.shift(64).notna()
    p["gap"] = p.ao / p.prev_ac - 1
    p["exdate"] = ((p.c / p.pc) / (p.ac / p.prev_ac) - 1).abs() > 0.005
    p["locked"] = p.h == p.l
    p["elig"] = p.member & (p.adv20 >= LIQ) & p.ret63_ok & p.pc.gt(0) & ~p.exdate & p.gap.notna()
    sessions = np.sort(p.d.unique())
    res = _filings(session, end)
    idx = first_session_open_after(sessions, res.pa.values)
    ok = idx < len(sessions)
    res = res[ok].copy()
    res["d"] = pd.to_datetime(sessions[idx[ok]])
    res = res.drop_duplicates(["iid", "d"])[["iid", "d"]]
    res["earn"] = True
    p = p.merge(res, on=["iid", "d"], how="left")
    p["earn"] = p.earn.fillna(False).astype(bool)
    bm = pd.concat(
        pd.read_csv(f, usecols=["isin", "meeting_date", "purpose"])
        for f in sorted(glob.glob(str(board_meetings / "board_meetings_*.csv")))
    )
    bm = bm[bm.purpose.str.contains("result", case=False, na=False)].copy()
    bm["md"] = pd.to_datetime(bm.meeting_date, format="%d-%b-%Y", errors="coerce")
    bm = bm[bm.md.notna() & (bm.md <= pd.Timestamp(end))].drop_duplicates(["isin", "md"])
    si = np.searchsorted(sessions, bm.md.values, side="left")
    okb = si < len(sessions) - 1
    bm, si = bm[okb], si[okb]
    flag = pd.concat(
        [
            pd.DataFrame({"isin": bm["isin"].values, "d": pd.to_datetime(sessions[si])}),
            pd.DataFrame({"isin": bm["isin"].values, "d": pd.to_datetime(sessions[si + 1])}),
        ]
    ).drop_duplicates()
    flag["earn_bm"] = True
    p = p.merge(flag, on=["isin", "d"], how="left")
    p["earn_bm"] = p.earn_bm.fillna(False).astype(bool)
    p["E"] = p.earn | p.earn_bm
    p = p[(p.d >= pd.Timestamp(start)) & (p.d <= pd.Timestamp(end))]
    p["event"] = p.elig & (p.gap >= min_gap) & ~p.locked
    return p.reset_index(drop=True)

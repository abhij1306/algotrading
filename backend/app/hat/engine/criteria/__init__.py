"""The criteria library: a registry of the 37 catalogue types.

Pure by construction: nothing here imports the database, a router, a broker or the network
(CONF `import_graph_boundaries`). Parameter models validate a config on save; `evaluate`
returns `(passed, score, value, reason)` for one instrument and date.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ...errors import HatError
from . import book, darvas, live, pullback, reversion, setup, setups, universe, vcp
from .base import (
    STAGES,
    CriterionResult,
    CriterionType,
    EvalContext,
    Evaluator,
    LiveEvaluator,
    ParamModel,
    Stage,
    no_data,
)
from .live import LiveContext

BARS = "bars_daily"
# A type that needs the pre-open data makes its playbook a pre-open one (`Playbook.preopen`).
PREOPEN = "preopen"
NOT_EVALUABLE = "not_evaluable"
FEATURES = "features_daily"
RESULTS = "financial_results"  # `hat_financial_result` and its derived growth
# The setup levels the library's criteria expose (`CriterionType.levels`).
PIVOT = "pivot"
CONTRACTION_LOW = "final_contraction_low"


class UnknownCriterionError(HatError):
    """A config named a criterion type the catalogue does not have."""


def _t(
    code: str,
    stage: Stage,
    params: type[ParamModel],
    needs: tuple[str, ...],
    evaluate: Evaluator | None = None,
    extra_stages: tuple[Stage, ...] = (),
    live_fn: LiveEvaluator | None = None,
    levels: tuple[str, ...] = (),
) -> CriterionType:
    return CriterionType(
        code,
        stage,
        params,
        needs,
        evaluate,
        extra_stages=extra_stages,
        evaluate_live=live_fn,
        levels=levels,
    )


def _live(
    code: str, stage: Stage, params: type[ParamModel], needs: tuple[str, ...], fn: LiveEvaluator
) -> CriterionType:
    return _t(code, stage, params, needs, live_fn=fn)


# Library-table order.
_TYPES = (
    _t(
        "series_in", "universe", universe.SeriesInParams, ("instrument_master",), universe.series_in
    ),
    _t("min_turnover", "universe", universe.MinTurnoverParams, (BARS,), universe.min_turnover),
    _t("min_price", "universe", universe.MinPriceParams, (BARS,), universe.min_price),
    _t(
        "price_band_allowed",
        "universe",
        universe.PriceBandAllowedParams,
        ("price_band",),
        universe.price_band_allowed,
    ),
    # also a playbook setup criterion: a pre-open setup judges it on the previous session
    _t("adr_min", "universe", universe.AdrMinParams, (BARS,), universe.adr_min, ("setup",)),
    _t("sma_above", "setup", setup.SmaAboveParams, (BARS,), setup.sma_above),
    _t("sma_rising", "setup", setup.SmaRisingParams, (BARS,), setup.sma_rising),
    _t("pct_from_52w_low", "setup", setup.PctFrom52wLowParams, (BARS,), setup.pct_from_52w_low),
    _t("pct_from_52w_high", "setup", setup.PctFrom52wHighParams, (BARS,), setup.pct_from_52w_high),
    _t("rs_rank", "setup", setup.RsRankParams, (FEATURES,), setup.rs_rank),
    _t("momentum_rank", "setup", setup.MomentumRankParams, (FEATURES,), setup.momentum_rank),
    _t("vcp", "setup", vcp.VcpParams, (BARS,), vcp.vcp, levels=(PIVOT, CONTRACTION_LOW)),
    _t(
        "book_vcp",
        "setup",
        book.BookVcpParams,
        (BARS, "index_daily"),
        book.book_vcp,
        levels=(PIVOT, CONTRACTION_LOW),
    ),
    # the S1 setups 2-5 and the rules every S1 setup shares (`setups`)
    *(
        _t(code, "setup", params, (BARS,), fn, levels=(PIVOT, CONTRACTION_LOW))
        for code, params, fn in (
            ("book_cheat", setups.BookCheatParams, setups.book_cheat),
            ("book_cup_handle", setups.BookCupHandleParams, setups.book_cup_handle),
            ("book_power_play", setups.BookPowerPlayParams, setups.book_power_play),
            ("book_primary_base", setups.BookPrimaryBaseParams, setups.book_primary_base),
        )
    ),
    _t("stop_cap", "setup", setups.StopCapParams, (BARS,), setups.stop_cap),
    _t("overhead_supply", "setup", setups.OverheadSupplyParams, (BARS,), setups.overhead_supply),
    _t("setup_tags", "setup", setups.SetupTagsParams, (BARS, "index_daily"), setups.setup_tags),
    _t(
        "consolidation",
        "setup",
        setup.ConsolidationParams,
        (BARS,),
        setup.consolidation,
        levels=("consolidation_high",),
    ),
    _t("ma_surf", "setup", setup.MaSurfParams, (BARS,), setup.ma_surf),
    _t("prior_move", "setup", setup.PriorMoveParams, (BARS,), setup.prior_move),
    _t(
        "earnings_growth",
        "setup",
        setup.EarningsGrowthParams,
        ("fundamentals",),
        setup.earnings_growth,
    ),
    _t("close_vs_pivot", "setup", setup.CloseVsPivotParams, (BARS,), setup.close_vs_pivot),
    _t(
        "darvas_box",
        "setup",
        darvas.DarvasBoxParams,
        (BARS,),
        darvas.darvas_box,
        levels=(PIVOT, CONTRACTION_LOW),
    ),
    _t(
        "ema_pullback",
        "setup",
        pullback.EmaPullbackParams,
        (BARS,),
        pullback.ema_pullback,
        levels=(PIVOT, CONTRACTION_LOW),
    ),
    _t(
        "rsi_below",
        "setup",
        reversion.RsiBelowParams,
        (BARS,),
        reversion.rsi_below,
        levels=(PIVOT,),
    ),
    _live("gap_up", "trigger", live.GapUpParams, (PREOPEN, "live_bars"), live.gap_up),
    _live("volume_pace", "trigger", live.VolumePaceParams, ("live_bars", BARS), live.volume_pace),
    _live(
        "early_volume", "trigger", live.EarlyVolumeParams, ("live_bars", BARS), live.early_volume
    ),
    _live(
        "catalyst_attached",
        "trigger",
        live.CatalystAttachedParams,
        ("announcements",),
        live.catalyst_attached,
    ),
    _live(
        "price_breaks_level",
        "trigger",
        live.PriceBreaksLevelParams,
        ("live_bars",),
        live.price_breaks_level,
    ),
    _live(
        "earnings_catalyst",
        "trigger",
        live.EarningsCatalystParams,
        (RESULTS,),
        live.earnings_catalyst,
    ),
    # The Q-EP setup is built live and holds `neglect` (BL "Qullamaggie-style bot").
    _t("neglect", "trigger", setup.NeglectParams, (BARS,), setup.neglect, ("setup",)),
    _live("adtr_filter", "entry_filter", live.AdtrFilterParams, (BARS,), live.adtr_filter),
    _live("max_chase", "entry_filter", live.MaxChaseParams, ("live_bars",), live.max_chase),
)

REGISTRY: dict[str, CriterionType] = {t.code: t for t in _TYPES}


def get_type(code: str) -> CriterionType:
    try:
        return REGISTRY[code]
    except KeyError:
        raise UnknownCriterionError("unknown criterion type") from None


def _params(ctype: CriterionType, params: BaseModel | dict[str, Any]) -> BaseModel:
    # Always validate afresh: a model instance may have been changed since it was built.
    raw = params.model_dump() if isinstance(params, BaseModel) else params
    return ctype.params.model_validate(raw)


def evaluate(code: str, params: BaseModel | dict[str, Any], ctx: EvalContext) -> CriterionResult:
    """Validate `params` against the type's model and evaluate over daily bars. A live type
    has no daily evaluator: here it fails closed with reason `not_evaluable`."""
    ctype = get_type(code)
    model = _params(ctype, params)
    if ctype.evaluate is None:
        return no_data(NOT_EVALUABLE)
    return ctype.evaluate(ctx, model)


def evaluate_live(
    code: str, params: BaseModel | dict[str, Any], ctx: live.LiveContext
) -> CriterionResult:
    """Validate `params` and evaluate a live trigger or entry filter at one instant. A daily
    type is not evaluated here: it fails closed with reason `not_evaluable`."""
    ctype = get_type(code)
    model = _params(ctype, params)
    if ctype.evaluate_live is None:
        return no_data(NOT_EVALUABLE)
    return ctype.evaluate_live(ctx, model)


__all__ = [
    "CONTRACTION_LOW",
    "PIVOT",
    "PREOPEN",
    "REGISTRY",
    "STAGES",
    "CriterionResult",
    "CriterionType",
    "EvalContext",
    "LiveContext",
    "UnknownCriterionError",
    "evaluate",
    "evaluate_live",
    "get_type",
]

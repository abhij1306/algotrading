"""The criteria library: a registry of the 24 catalogue types (04 §4).

Pure by construction: nothing here imports the database, a router, a broker or the network
(CONF `import_graph_boundaries`). Parameter models validate a config on save; `evaluate`
returns `(passed, score, value, reason)` for one instrument and date.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ...errors import HatError
from . import live, setup, universe, vcp
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
NOT_EVALUABLE = "not_evaluable"
FEATURES = "features_daily"


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
) -> CriterionType:
    return CriterionType(
        code, stage, params, needs, evaluate, extra_stages=extra_stages, evaluate_live=live_fn
    )


def _live(
    code: str, stage: Stage, params: type[ParamModel], needs: tuple[str, ...], fn: LiveEvaluator
) -> CriterionType:
    return _t(code, stage, params, needs, live_fn=fn)


# Library-table order of 04 §4.
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
    _t("adr_min", "universe", universe.AdrMinParams, (BARS,), universe.adr_min),
    _t("sma_above", "setup", setup.SmaAboveParams, (BARS,), setup.sma_above),
    _t("sma_rising", "setup", setup.SmaRisingParams, (BARS,), setup.sma_rising),
    _t("pct_from_52w_low", "setup", setup.PctFrom52wLowParams, (BARS,), setup.pct_from_52w_low),
    _t("pct_from_52w_high", "setup", setup.PctFrom52wHighParams, (BARS,), setup.pct_from_52w_high),
    _t("rs_rank", "setup", setup.RsRankParams, (FEATURES,), setup.rs_rank),
    _t("momentum_rank", "setup", setup.MomentumRankParams, (FEATURES,), setup.momentum_rank),
    _t("vcp", "setup", vcp.VcpParams, (BARS,), vcp.vcp),
    _t("consolidation", "setup", setup.ConsolidationParams, (BARS,), setup.consolidation),
    _t("ma_surf", "setup", setup.MaSurfParams, (BARS,), setup.ma_surf),
    _t("prior_move", "setup", setup.PriorMoveParams, (BARS,), setup.prior_move),
    _t(
        "earnings_growth",
        "setup",
        setup.EarningsGrowthParams,
        ("fundamentals",),
        setup.earnings_growth,
    ),
    _live("gap_up", "trigger", live.GapUpParams, ("preopen", "live_bars"), live.gap_up),
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

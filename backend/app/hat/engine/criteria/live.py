"""Parameter models of the live trigger and entry-filter criteria (04 §4, HAT-CRIT-115..122).

Their schemas are registered now so a config can hold them and the catalogue is complete; their
evaluation needs live bars, pre-open prices and announcements and is PR 15's (`evaluate` stays
None in the registry). `neglect`, a trigger-stage criterion over daily bars, is evaluated in
`setup.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import ParamModel

LEVEL_PIVOT = "pivot"
LEVEL_OPENING_RANGE = "opening_range_high"
LEVEL_BREAKOUT = "max(consolidation_high, opening_range_high)"
LEVELS = (LEVEL_PIVOT, LEVEL_OPENING_RANGE, LEVEL_BREAKOUT)


class GapUpParams(ParamModel):
    min_pct: float = Field(default=10.0, gt=0, le=1000)


class VolumePaceParams(ParamModel):
    vs_avg50: float = Field(default=1.5, gt=0, le=1000)


class EarlyVolumeParams(ParamModel):
    vs_adv20: float = Field(default=0.5, gt=0, le=1000)


class CatalystAttachedParams(ParamModel):
    within_hours: int = Field(default=24, ge=1, le=168)
    required_in: list[Literal["shadow", "confirm", "auto"]] = Field(
        default_factory=lambda: ["confirm"], max_length=3
    )


class PriceBreaksLevelParams(ParamModel):
    level: Literal["pivot", "opening_range_high", "max(consolidation_high, opening_range_high)"]
    bar: Literal["5m"] = "5m"


class AdtrFilterParams(ParamModel):
    max_multiple: float = Field(default=1.0, gt=0, le=100)


class MaxChaseParams(ParamModel):
    pct: float = Field(gt=0, le=100)

"""Stage evaluation and the setup score (04 §1-§3).

`evaluate_stage` runs the enabled criteria of one stage in ascending `order`: a failing hard
criterion stops the stage at once and is stored with its reason and value (HAT-CRIT-032); score
criteria add `(weight, score)`; the stage score is the weighted mean, 1.0 when no score
criterion is enabled (HAT-CRIT-030). A stage passes when every hard criterion passed and the
score reaches the threshold (HAT-CRIT-031). `evaluate_playbook` chains the nightly stages
universe, regime gate, setup, and stops at the first that fails (HAT-CRIT-010).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from ..criteria import CriterionResult, EvalContext, evaluate
from .config import Instance, StrategyConfig

EMPTY_SCORE = 1.0  # [OWNER-PENDING] docs silent: no scored criterion means only hard rules decide


@dataclass(frozen=True)
class CriterionOutcome:
    type: str
    order: int
    mode: str
    weight: float
    result: CriterionResult


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    passed: bool
    score: float
    outcomes: tuple[CriterionOutcome, ...]
    failed: CriterionOutcome | None = None
    reason: str = "ok"


def weighted_mean(pairs: Iterable[tuple[float, float]]) -> float:
    """sum(w_i s_i) / sum(w_i) over `(w_i > 0, s_i in [0, 1])`, exact in rationals and rounded
    once; 1.0 for no pair."""
    total = Fraction(0)
    weights = Fraction(0)
    for weight, score in pairs:
        total += Fraction(weight) * Fraction(score)
        weights += Fraction(weight)
    if weights == 0:
        return EMPTY_SCORE
    return float(total / weights)


def passes_threshold(hard_ok: bool, score: float, threshold: float) -> bool:
    """HAT-CRIT-031: all hard criteria passed and the score is at least the threshold."""
    return hard_ok and score >= threshold


def evaluate_stage(
    stage: str,
    instances: Sequence[Instance],
    ctx: EvalContext,
    threshold: float = 0.0,
) -> StageOutcome:
    """Evaluate the enabled `instances` of `stage` in ascending `order` (the list's own order
    does not matter). Stops at the first failing hard criterion."""
    done: list[CriterionOutcome] = []
    scored: list[tuple[float, float]] = []
    for instance in sorted((i for i in instances if i.enabled), key=lambda i: i.order):
        result = evaluate(instance.type, instance.params, ctx)
        outcome = CriterionOutcome(
            instance.type, instance.order, instance.mode, instance.weight, result
        )
        done.append(outcome)
        if instance.mode == "hard":
            if not result.passed:
                return StageOutcome(stage, False, 0.0, tuple(done), outcome, result.reason)
        else:
            scored.append((instance.weight, result.score))
    score = weighted_mean(scored)
    passed = passes_threshold(True, score, threshold)
    return StageOutcome(
        stage, passed, score, tuple(done), None, "ok" if passed else "below_threshold"
    )


@dataclass(frozen=True)
class PlaybookEvaluation:
    """`stages` holds the evaluated stages in stage order, the last being the one that stopped
    the symbol when `passed` is false."""

    playbook: str
    passed: bool
    stages: tuple[StageOutcome, ...] = field(default_factory=tuple)

    @property
    def stopped_at(self) -> str | None:
        return None if self.passed else self.stages[-1].stage


def regime_outcome(allows_longs: bool) -> StageOutcome:
    return StageOutcome(
        "regime",
        allows_longs,
        1.0 if allows_longs else 0.0,
        (),
        None,
        "ok" if allows_longs else "regime_blocks_longs",
    )


def evaluate_playbook(
    config: StrategyConfig, playbook: str, ctx: EvalContext, *, regime_allows_longs: bool
) -> PlaybookEvaluation:
    """The nightly stages for one playbook: universe, regime gate, setup. A later stage runs only
    after every earlier one passed."""
    chosen = config.playbook(playbook)
    universe = evaluate_stage("universe", config.universe, ctx)
    if not universe.passed:
        return PlaybookEvaluation(playbook, False, (universe,))
    gate = regime_outcome(regime_allows_longs)
    if not gate.passed:
        return PlaybookEvaluation(playbook, False, (universe, gate))
    setup = evaluate_stage("setup", chosen.setup.criteria, ctx, chosen.setup.threshold)
    return PlaybookEvaluation(playbook, setup.passed, (universe, gate, setup))

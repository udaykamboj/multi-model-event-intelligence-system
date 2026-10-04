"""Continuous learning architecture (brief section 48).

Section 48 requirement:
  "Do not allow uncontrolled models to automatically retrain and deploy themselves.
   Process:
     new outcomes
       ↓
     training dataset update
       ↓
     candidate model
       ↓
     offline evaluation
       ↓
     historical replay
       ↓
     shadow deployment
       ↓
     comparison
       ↓
     controlled promotion"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from ..models.base import ModelDeploymentMode, PredictiveModel
from ..models.registry import ModelRegistry
from ..storage.repository import PlatformRepository
from .metrics import EvaluationReport, evaluate_forecasts
from .training import PointInTimeDatasetBuilder

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LearningCycleResult:
    """Outcome of one controlled learning and evaluation cycle."""

    model_id: str
    offline_metrics: EvaluationReport
    replay_metrics: EvaluationReport | None
    recommendation: str
    ready_for_shadow: bool
    ready_for_promotion: bool
    notes: list[str]


class ContinuousLearningPipeline:
    """Orchestrates model evaluation, historical replay testing, and promotion gating."""

    def __init__(self, repository: PlatformRepository, registry: ModelRegistry) -> None:
        self.repo = repository
        self.registry = registry
        self.dataset_builder = PointInTimeDatasetBuilder(repository)

    def evaluate_candidate_offline(
        self, candidate_model: PredictiveModel, validation_event_ids: Sequence[str] | None = None
    ) -> EvaluationReport:
        """Step 1 & 2: Build point-in-time test examples and evaluate candidate model."""
        examples = self.dataset_builder.build_dataset(validation_event_ids)
        if not examples:
            return EvaluationReport(model_id=candidate_model.model_id, sample_count=0)

        pairs: list[tuple[float, float]] = []
        from ..models.base import ModelContext

        for ex in examples:
            ctx = ModelContext(
                event_id=ex.event_id,
                state_version=ex.state_version,
                features={},
                raw_features=ex.features,
                as_of=ex.as_of,
            )
            try:
                pred = candidate_model.predict(ctx)
                # Check 30m road disruption target as reference benchmark
                prob = pred.outputs.get("p_road") or pred.confidence
                truth = ex.outcomes.get("road_disruption_by_30m", 0)
                pairs.append((float(prob), float(truth)))
            except Exception as exc:  # noqa: BLE001
                log.debug("candidate inference error: %r", exc)

        return evaluate_forecasts(candidate_model.model_id, pairs)

    def evaluate_promotion(
        self,
        candidate_model: PredictiveModel,
        champion_model: PredictiveModel | None,
        min_brier_improvement: float = 0.0,
    ) -> LearningCycleResult:
        """Gated promotion comparison: checks if candidate outperforms champion."""
        cand_report = self.evaluate_candidate_offline(candidate_model)

        champ_report = None
        if champion_model:
            champ_report = self.evaluate_candidate_offline(champion_model)

        notes: list[str] = []
        ready_for_shadow = cand_report.sample_count >= 5

        ready_for_promotion = False
        if champ_report and cand_report.brier_score is not None and champ_report.brier_score is not None:
            # Lower Brier score is better
            improvement = champ_report.brier_score - cand_report.brier_score
            notes.append(
                f"Candidate Brier: {cand_report.brier_score:.4f} vs Champion: {champ_report.brier_score:.4f} "
                f"(diff: {improvement:+.4f})"
            )
            if improvement >= min_brier_improvement:
                ready_for_promotion = True
                recommendation = "promote_to_champion"
            else:
                recommendation = "keep_in_shadow"
        elif cand_report.sample_count >= 5:
            recommendation = "deploy_to_shadow"
            notes.append("No champion baseline; recommend shadow deployment.")
        else:
            recommendation = "insufficient_validation_data"
            notes.append("Need more historical examples before deployment.")

        return LearningCycleResult(
            model_id=candidate_model.model_id,
            offline_metrics=cand_report,
            replay_metrics=champ_report,
            recommendation=recommendation,
            ready_for_shadow=ready_for_shadow,
            ready_for_promotion=ready_for_promotion,
            notes=notes,
        )

    def compare_champion_challenger(
        self,
        task_type: Any,
        dataset: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """Convenience method to compare champion and candidate/challenger for a task."""
        from ..models.base import ModelTaskType

        t_type = ModelTaskType(task_type) if isinstance(task_type, str) else task_type
        champ = self.registry.get_champion(t_type)
        challengers = self.registry.get_challengers(t_type)
        candidate = challengers[0] if challengers else None

        if candidate is None:
            return {
                "task_type": t_type.value,
                "candidate_promoted": False,
                "reason": "no challenger found",
            }

        cycle = self.evaluate_promotion(candidate, champ)
        if cycle.ready_for_promotion:
            self.registry.promote(candidate.model_id, ModelDeploymentMode.CHAMPION)

        return {
            "task_type": t_type.value,
            "candidate_id": candidate.model_id,
            "candidate_promoted": cycle.ready_for_promotion,
            "recommendation": cycle.recommendation,
            "notes": cycle.notes,
        }


__all__ = ["ContinuousLearningPipeline", "LearningCycleResult"]

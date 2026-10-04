"""Model evaluation, training data generation, and continuous learning package."""

from .learning import ContinuousLearningPipeline, LearningCycleResult
from .metrics import (
    EvaluationReport,
    compute_brier_score,
    compute_classification_metrics,
    compute_expected_calibration_error,
    compute_regression_metrics,
    evaluate_forecasts,
)
from .training import PointInTimeDatasetBuilder, TrainingExample

__all__ = [
    "ContinuousLearningPipeline",
    "EvaluationReport",
    "LearningCycleResult",
    "PointInTimeDatasetBuilder",
    "TrainingExample",
    "compute_brier_score",
    "compute_classification_metrics",
    "compute_expected_calibration_error",
    "compute_regression_metrics",
    "evaluate_forecasts",
]

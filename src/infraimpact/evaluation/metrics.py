"""Model evaluation framework (brief section 45).

Section 45 requirements:
  - Connect forecasts to ground truth outcomes.
  - Metrics: Brier score, calibration error, precision, recall, F1, MAE, RMSE,
    false-alert rate, missed-impact rate, forecast lead time.
  - Probability calibration error estimation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class EvaluationReport:
    """Standardized metric report for a model over a test dataset."""

    model_id: str
    sample_count: int
    brier_score: float | None = None
    expected_calibration_error: float | None = None
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    mae: float | None = None
    rmse: float | None = None
    false_alert_rate: float | None = None
    missed_impact_rate: float | None = None
    mean_lead_time_minutes: float | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "sample_count": self.sample_count,
            "brier_score": self.brier_score,
            "expected_calibration_error": self.expected_calibration_error,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "mae": self.mae,
            "rmse": self.rmse,
            "false_alert_rate": self.false_alert_rate,
            "missed_impact_rate": self.missed_impact_rate,
            "mean_lead_time_minutes": self.mean_lead_time_minutes,
            "details": self.details or {},
        }


def compute_brier_score(predictions: Sequence[float], ground_truth: Sequence[int | bool | float]) -> float:
    """Calculate mean squared difference between predicted probability and binary outcome via scikit-learn.

    Brier score = (1/N) * sum((p_i - y_i)^2). Lower is better (0 is perfect).
    """
    if not predictions or len(predictions) != len(ground_truth):
        return 0.0
    y_true = [int(bool(y)) if isinstance(y, (bool, int)) else float(y) for y in ground_truth]
    y_prob = [float(p) for p in predictions]
    try:
        from sklearn.metrics import brier_score_loss

        return round(float(brier_score_loss(y_true, y_prob)), 4)
    except Exception:
        total = sum((float(p) - float(y)) ** 2 for p, y in zip(y_prob, y_true, strict=False))
        return round(total / len(predictions), 4)


def compute_expected_calibration_error(
    probabilities: Sequence[float], ground_truth: Sequence[int | bool | float], n_bins: int = 10
) -> float:
    """Compute Expected Calibration Error (ECE) across probability bins."""
    if not probabilities or len(probabilities) != len(ground_truth):
        return 0.0

    bins = [[] for _ in range(n_bins)]
    for p, y in zip(probabilities, ground_truth, strict=False):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, float(y)))

    total_samples = len(probabilities)
    ece = 0.0
    for b in bins:
        if not b:
            continue
        bin_size = len(b)
        avg_confidence = sum(item[0] for item in b) / bin_size
        avg_accuracy = sum(item[1] for item in b) / bin_size
        ece += (bin_size / total_samples) * abs(avg_accuracy - avg_confidence)

    return round(ece, 4)


def compute_classification_metrics(
    predictions: Sequence[float], ground_truth: Sequence[int | bool | float], threshold: float = 0.5
) -> dict[str, float]:
    """Precision, recall, F1, false alert rate, missed impact rate using scikit-learn."""
    if not predictions or len(predictions) != len(ground_truth):
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "false_alert_rate": 0.0, "missed_impact_rate": 0.0}

    y_true = [1 if bool(y) else 0 for y in ground_truth]
    y_pred = [1 if float(p) >= threshold else 0 for p in predictions]

    from sklearn.metrics import confusion_matrix, f1_score, precision_score, recall_score

    prec = float(precision_score(y_true, y_pred, zero_division=0.0))
    rec = float(recall_score(y_true, y_pred, zero_division=0.0))
    f1 = float(f1_score(y_true, y_pred, zero_division=0.0))

    cm = confusion_matrix(y_true, y_pred, labels=[0, 1])
    tn, fp, fn, tp = cm.ravel()

    false_alert_rate = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    missed_impact_rate = fn / (tp + fn) if (tp + fn) > 0 else 0.0

    return {
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
        "false_alert_rate": round(false_alert_rate, 4),
        "missed_impact_rate": round(missed_impact_rate, 4),
    }


def compute_regression_metrics(
    predictions: Sequence[float], ground_truth: Sequence[float]
) -> dict[str, float]:
    """Mean Absolute Error (MAE) and Root Mean Squared Error (RMSE) via scikit-learn."""
    if not predictions or len(predictions) != len(ground_truth):
        return {"mae": 0.0, "rmse": 0.0}

    from sklearn.metrics import mean_absolute_error, root_mean_squared_error

    mae = float(mean_absolute_error(ground_truth, predictions))
    rmse = float(root_mean_squared_error(ground_truth, predictions))
    return {"mae": round(mae, 4), "rmse": round(rmse, 4)}


def evaluate_forecasts(
    model_id_or_pairs: str | Sequence[tuple[float, float]],
    pairs: Sequence[tuple[float, float]] | None = None,
    lead_times_min: Sequence[float] | None = None,
) -> EvaluationReport:
    """Evaluate a sequence of (forecast_probability, ground_truth) pairs."""
    if isinstance(model_id_or_pairs, str):
        model_id = model_id_or_pairs
        actual_pairs = pairs or []
    else:
        model_id = "model"
        actual_pairs = model_id_or_pairs

    if not actual_pairs:
        return EvaluationReport(model_id=model_id, sample_count=0)

    probs = [float(p[0]) for p in actual_pairs]
    truth = [float(p[1]) for p in actual_pairs]

    brier = compute_brier_score(probs, truth)
    ece = compute_expected_calibration_error(probs, truth)
    cls_metrics = compute_classification_metrics(probs, truth)
    reg_metrics = compute_regression_metrics(probs, truth)

    mean_lead = (sum(lead_times_min) / len(lead_times_min)) if lead_times_min else None

    return EvaluationReport(
        model_id=model_id,
        sample_count=len(actual_pairs),
        brier_score=brier,
        expected_calibration_error=ece,
        precision=cls_metrics["precision"],
        recall=cls_metrics["recall"],
        f1=cls_metrics["f1"],
        mae=reg_metrics["mae"],
        rmse=reg_metrics["rmse"],
        false_alert_rate=cls_metrics["false_alert_rate"],
        missed_impact_rate=cls_metrics["missed_impact_rate"],
        mean_lead_time_minutes=round(mean_lead, 1) if mean_lead is not None else None,
        details={"classification": cls_metrics, "regression": reg_metrics},
    )


brier_score = compute_brier_score
expected_calibration_error = compute_expected_calibration_error
mean_absolute_error = lambda p, g: compute_regression_metrics(p, g)["mae"]


__all__ = [
    "EvaluationReport",
    "brier_score",
    "compute_brier_score",
    "compute_classification_metrics",
    "compute_expected_calibration_error",
    "compute_regression_metrics",
    "evaluate_forecasts",
    "expected_calibration_error",
    "mean_absolute_error",
]

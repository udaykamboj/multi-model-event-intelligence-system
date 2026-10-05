"""Predictive ML model architecture and contracts (brief sections 23-27, 44, 47).

Section 23-27 defines the model portfolio:
  Model A: Event Classification (distribution over event types)
  Model B: Infrastructure Impact (P(road), P(transit), P(utility), P(facility), P(route_delay))
  Model C: Time-to-Impact (hazard/survival curve over 5/15/30/60m horizons)
  Model D: Traffic Prediction (counterfactual baseline and anomaly detection)
  Model E: Transit Disruption (delays, cancellation probability, accessibility)
  Model F: Spatial Propagation (network propagation weights)

Section 47 defines deployment modes:
  CHAMPION: Primary model driving active forecasts.
  CHALLENGER: Competitive candidate running alongside champion.
  SHADOW: Passive model run for evaluation; output is recorded in ModelOutput
          for retrospective comparison but does NOT affect live user exposure.

Section 44 defines the model output contract.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Sequence

from ..domain.enums import InfrastructureDomain, TruthStatus
from ..domain.ids import utcnow
from ..domain.schemas import FeatureValue, Forecast, ModelOutput


class ModelDeploymentMode(StrEnum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    SHADOW = "shadow"


class ModelTaskType(StrEnum):
    EVENT_CLASSIFICATION = "event_classification"
    INFRASTRUCTURE_IMPACT = "infrastructure_impact"
    TIME_TO_IMPACT = "time_to_impact"
    TRAFFIC_PREDICTION = "traffic_prediction"
    TRANSIT_DISRUPTION = "transit_disruption"
    SPATIAL_PROPAGATION = "spatial_propagation"
    USER_PRIORITY_RANKING = "user_priority_ranking"


class PredictionStatus(StrEnum):
    COMPLETED = "completed"
    MODEL_REQUIRED = "model_required"
    ERROR = "error"


@dataclass(frozen=True)
class ModelMetadata:
    """Section 47: metadata tracked for every registered model."""

    model_id: str
    version: str
    task_type: ModelTaskType
    deployment_mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION
    description: str = ""
    feature_schema: Sequence[str] = ()
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    training_data_range: tuple[str, str] | None = None
    calibration_model: str = "isotonic-v1"
    specification_doc: str = ""
    code_commit: str = ""
    artifact_checksum: str = ""
    deployed_at: datetime = field(default_factory=utcnow)


@dataclass
class ModelContext:
    """Input context passed to a predictive model during inference."""

    event_id: str
    state_version: int
    features: dict[str, FeatureValue]
    raw_features: dict[str, Any] = field(default_factory=dict)
    historical_analogues: Sequence[dict[str, Any]] = ()
    state: Any = None
    observations: Sequence[Any] = ()
    as_of: datetime = field(default_factory=utcnow)

    def get_feature(self, name: str, default: Any = None) -> Any:
        if name in self.raw_features:
            return self.raw_features[name]
        fv = self.features.get(name)
        return fv.value if fv is not None else default


@dataclass
class ModelPrediction:
    """Standardized result produced by any predictive model."""

    model_id: str
    model_version: str
    task_type: ModelTaskType
    deployment_mode: ModelDeploymentMode
    status: PredictionStatus = PredictionStatus.COMPLETED
    is_placeholder: bool = False
    features_version: str = "1.0.0"
    outputs: dict[str, Any] = field(default_factory=dict)
    forecasts: list[Forecast] = field(default_factory=list)
    model_output: ModelOutput | None = None
    confidence: float = 0.5
    uncertainty: float | None = None
    calibration_version: str = "calibrated-v1"
    notes: list[str] = field(default_factory=list)


class PredictiveModel(abc.ABC):
    """Abstract interface for all specialized predictive ML models."""

    def __init__(self, metadata: ModelMetadata, *, is_trained: bool = False) -> None:
        self.metadata = metadata
        self.is_trained = is_trained

    @property
    def model_id(self) -> str:
        return self.metadata.model_id

    @property
    def version(self) -> str:
        return self.metadata.version

    @property
    def task_type(self) -> ModelTaskType:
        return self.metadata.task_type

    @property
    def deployment_mode(self) -> ModelDeploymentMode:
        return self.metadata.deployment_mode

    @abc.abstractmethod
    def predict(self, context: ModelContext) -> ModelPrediction:
        """Execute inference over the point-in-time model context."""
        ...

    def untrained_prediction(self, context: ModelContext) -> ModelPrediction:
        """Standardized response when a model requires trained weights.

        In accordance with Section 23-27: does not fabricate fake predictions.
        Transparently identifies that trained weights are required.
        """
        spec = self.metadata.specification_doc or f"docs/models/{self.task_type.value}.md"
        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.MODEL_REQUIRED,
            is_placeholder=True,
            outputs={
                "status": "MODEL_REQUIRED",
                "specification": spec,
                "required_features": list(self.metadata.feature_schema),
            },
            forecasts=[],
            confidence=0.0,
            uncertainty=1.0,
            notes=[
                f"Model '{self.model_id}' requires trained weights. "
                f"Specification available at {spec}. "
                "No synthetic heuristics or fabricated predictions were generated."
            ],
        )

    def to_model_output(self, prediction: ModelPrediction, state_version: int) -> ModelOutput:
        """Create a section 44 compliant ModelOutput row.

        ``status`` and ``is_placeholder`` are merged in from the typed
        prediction rather than trusted to whatever the model happened to write
        into ``outputs``. They used to be dropped, which left a hand-written
        string inside the free-form payload as the only evidence that a model had
        no weights - and the consumer compared that string against a
        differently-cased one, so every untrained model was reported as having
        completed successfully. Carrying the enum through removes the string
        comparison entirely.
        """

        output = {
            **prediction.outputs,
            "status": prediction.status.value,
            "is_placeholder": prediction.is_placeholder,
            "task_type": prediction.task_type.value,
            "deployment_mode": prediction.deployment_mode.value,
            "notes": list(prediction.notes),
        }
        return ModelOutput(
            model_id=self.model_id,
            model_version=self.version,
            prediction_time=utcnow(),
            input_state_version=state_version,
            features_version=prediction.features_version,
            output=output,
            probability=prediction.confidence,
            uncertainty=prediction.uncertainty,
            calibration_version=prediction.calibration_version,
        )


__all__ = [
    "ModelContext",
    "ModelDeploymentMode",
    "ModelMetadata",
    "ModelPrediction",
    "ModelTaskType",
    "PredictionStatus",
    "PredictiveModel",
]


"""Specialized predictive ML models (brief sections 23-27, 36, 44, 47).

Defines the model portfolio contracts and baseline reference implementations:
  - Model A: EventClassificationModel (§23, docs/models/MODEL_A_EVENT_CLASSIFICATION.md)
  - Model B: InfrastructureImpactModel (§23, docs/models/MODEL_B_INFRASTRUCTURE_IMPACT.md)
  - Model C: TimeToImpactModel (§24, docs/models/MODEL_C_TIME_TO_IMPACT.md)
  - Model D: TrafficPredictionModel (§25, docs/models/MODEL_D_TRAFFIC_PREDICTION.md)
  - Model E: TransitDisruptionModel (§26, docs/models/MODEL_E_TRANSIT_DISRUPTION.md)
  - Model F: SpatialPropagationModel (§27, docs/models/MODEL_F_SPATIAL_PROPAGATION.md)
  - Model G: UserPriorityRankingModel (§36, docs/models/MODEL_G_USER_PRIORITY_RANKING.md)

All models implement PredictiveModel and are registered in ModelRegistry.
In accordance with Section 23-27: when a model has not yet been loaded with trained
weights (is_trained=False), it transparently reports status="MODEL_REQUIRED",
is_placeholder=True, and generates ZERO fabricated forecasts.

A user or ML engineer can plug in their trained PyTorch, LightGBM, or CatBoost
models by subclassing PredictiveModel or providing weights artifacts.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..domain.enums import InfrastructureDomain, TruthStatus
from ..domain.schemas import Forecast
from .base import (
    ModelContext,
    ModelDeploymentMode,
    ModelMetadata,
    ModelPrediction,
    ModelTaskType,
    PredictionStatus,
    PredictiveModel,
)


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _saturate(x: float) -> float:
    return max(0.0, min(1.0, x))


def _interval(p: float, width: float = 0.28) -> tuple[float, float]:
    return (round(max(0.0, p - width / 2), 4), round(min(1.0, p + width / 2), 4))


# --------------------------------------------------------------------------
# Model A: Event Classification (section 23)
# --------------------------------------------------------------------------


class EventClassificationModel(PredictiveModel):
    """Model A: predicts event-type probability distribution over candidate categories."""

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
        *,
        is_trained: bool = False,
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"event-classifier-{version}",
                version=version,
                task_type=ModelTaskType.EVENT_CLASSIFICATION,
                deployment_mode=mode,
                description="Event classification model over observation evidence.",
                feature_schema=[
                    "observation_types",
                    "source_count",
                    "has_permit",
                    "police_active",
                    "road_closed",
                ],
                calibration_model="isotonic-v1",
                specification_doc="docs/models/MODEL_A_EVENT_CLASSIFICATION.md",
            ),
            is_trained=is_trained,
        )

    def predict(self, context: ModelContext) -> ModelPrediction:
        if not self.is_trained:
            return self.untrained_prediction(context)

        obs_types = context.get_feature("observation_types", []) or []
        scores: dict[str, float] = {}

        if "permit_event" in obs_types:
            scores["demonstration_planned"] = 0.85
        if "police_response" in obs_types:
            scores["public_gathering"] = 0.70
        if "news_article" in obs_types:
            scores["public_gathering"] = max(scores.get("public_gathering", 0.0), 0.55)
        if "earthquake" in obs_types:
            scores["earthquake"] = 0.97
        if "official_emergency_notice" in obs_types:
            scores["official_emergency"] = 0.90
        if "severe_weather" in obs_types:
            scores["severe_weather"] = 0.80
        if "wildfire" in obs_types:
            scores["wildfire"] = 0.90
        if "road_closure" in obs_types and not scores:
            scores["transportation_incident"] = 0.60

        if not scores:
            scores["unknown"] = 1.0

        total = sum(scores.values()) or 1.0
        distribution = {k: round(v / total, 4) for k, v in scores.items()}
        dominant, prob = max(distribution.items(), key=lambda kv: kv[1])

        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.COMPLETED,
            is_placeholder=False,
            outputs={"distribution": distribution, "dominant_class": dominant},
            confidence=prob,
            uncertainty=round(1.0 - prob, 4),
        )


class BaselineEventClassifier(EventClassificationModel):
    """Reference baseline for Model A with baseline heuristic weights enabled."""

    def __init__(self, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION, version: str = "v1.0.0") -> None:
        super().__init__(mode=mode, version=version, is_trained=True)


# --------------------------------------------------------------------------
# Model B: Infrastructure Impact (section 23)
# --------------------------------------------------------------------------


class InfrastructureImpactModel(PredictiveModel):
    """Model B: predicts P(road), P(transit), P(utility), P(facility), P(route_delay)."""

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
        *,
        is_trained: bool = False,
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"infra-impact-{version}",
                version=version,
                task_type=ModelTaskType.INFRASTRUCTURE_IMPACT,
                deployment_mode=mode,
                description="Multivariate infrastructure impact probability model.",
                feature_schema=[
                    "moving",
                    "crowd_estimate",
                    "expansion_rate",
                    "arterial_overlap_count",
                    "road_overlap_count",
                    "transit_route_overlap",
                    "duration_hours",
                    "rush_hour",
                    "source_authority",
                    "propagation_reach",
                    "route_redundancy",
                ],
                calibration_model="platt-scaling-v1",
                specification_doc="docs/models/MODEL_B_INFRASTRUCTURE_IMPACT.md",
            ),
            is_trained=is_trained,
        )

    def predict(self, context: ModelContext) -> ModelPrediction:
        if not self.is_trained:
            return self.untrained_prediction(context)

        moving = float(context.get_feature("moving", 0.0) or 0.0)
        crowd = float(context.get_feature("crowd_estimate", 0.0) or 0.0)
        expansion = float(context.get_feature("expansion_rate", 0.0) or 0.0)
        arterial = float(context.get_feature("arterial_overlap_count", 0.0) or 0.0)
        road = float(context.get_feature("road_overlap_count", 0.0) or 0.0)
        transit = float(context.get_feature("transit_route_overlap", 0.0) or 0.0)
        duration = float(context.get_feature("duration_hours", 0.0) or 0.0)
        rush = float(context.get_feature("rush_hour", 0.0) or 0.0)
        source_auth = float(context.get_feature("source_authority", 0.5) or 0.5)
        reach = float(context.get_feature("propagation_reach", 0.0) or 0.0)
        redundancy = float(context.get_feature("route_redundancy", 1.0) or 1.0)

        # Interpretable logit score:
        x = (
            -2.6
            + 0.55 * moving
            + 0.90 * _saturate(crowd / 1000.0)
            + 0.70 * expansion
            + 1.40 * arterial
            + 0.60 * _saturate(road / 4.0)
            + 0.80 * _saturate(transit / 3.0)
            + 0.35 * duration
            + 0.30 * rush
            + 0.80 * source_auth
            + 0.50 * _saturate(reach / 20.0)
            - 0.70 * redundancy
        )

        p_road = _sigmoid(x)
        p_transit = _sigmoid(x - 0.35 * transit)
        p_utility = _sigmoid(x - 2.4)
        p_facility = _sigmoid(x - 2.0)
        p_route_delay = _sigmoid(x + 0.3 * rush)

        forecasts: list[Forecast] = []
        for target, domain, p in (
            ("road_disruption", InfrastructureDomain.ROAD, p_road),
            ("transit_disruption", InfrastructureDomain.TRANSIT, p_transit),
            ("utility_disruption", InfrastructureDomain.UTILITY, p_utility),
            ("public_facility_impact", InfrastructureDomain.PUBLIC_FACILITY, p_facility),
            ("route_delay", InfrastructureDomain.ROAD, p_route_delay),
        ):
            for horizon in (5, 15, 30, 60):
                shape = {5: 0.12, 15: 0.42, 30: 0.72, 60: 1.0}.get(horizon, horizon / 60.0)
                p_horiz = _saturate(p * shape)
                low, high = _interval(p_horiz)
                forecasts.append(
                    Forecast(
                        target=target,
                        domain=domain,
                        horizon_minutes=horizon,
                        probability=round(p_horiz, 4),
                        lower=low,
                        upper=high,
                        model_id=self.model_id,
                        model_version=self.version,
                        calibration_version=self.metadata.calibration_model,
                        truth_status=TruthStatus.PREDICTED,
                    )
                )

        outputs = {
            "p_road": round(p_road, 4),
            "p_transit": round(p_transit, 4),
            "p_utility": round(p_utility, 4),
            "p_public_facility": round(p_facility, 4),
            "p_route_delay": round(p_route_delay, 4),
        }

        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.COMPLETED,
            is_placeholder=False,
            outputs=outputs,
            forecasts=forecasts,
            confidence=round(max(p_road, p_transit), 4),
            uncertainty=round(4.0 * p_road * (1.0 - p_road), 4),
        )


class BaselineInfrastructureImpactModel(InfrastructureImpactModel):
    """Reference baseline for Model B with baseline weights enabled."""

    def __init__(self, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION, version: str = "v1.0.0") -> None:
        super().__init__(mode=mode, version=version, is_trained=True)


# --------------------------------------------------------------------------
# Model C: Time-to-Impact Model (section 24)
# --------------------------------------------------------------------------


class TimeToImpactModel(PredictiveModel):
    """Model C: parametric hazard/survival model over 5/15/30/60 minute horizons."""

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
        *,
        is_trained: bool = False,
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"time-to-impact-{version}",
                version=version,
                task_type=ModelTaskType.TIME_TO_IMPACT,
                deployment_mode=mode,
                description="Survival time-to-impact hazard model.",
                feature_schema=["p_road", "p_transit", "p_utility", "p_facility"],
                specification_doc="docs/models/MODEL_C_TIME_TO_IMPACT.md",
            ),
            is_trained=is_trained,
        )

    BASE_HAZARD = {
        InfrastructureDomain.ROAD: 0.010,
        InfrastructureDomain.TRANSIT: 0.006,
        InfrastructureDomain.UTILITY: 0.002,
        InfrastructureDomain.PUBLIC_FACILITY: 0.003,
    }

    def predict(self, context: ModelContext) -> ModelPrediction:
        if not self.is_trained:
            return self.untrained_prediction(context)

        drivers = {
            InfrastructureDomain.ROAD: float(context.get_feature("p_road", 0.0) or 0.0),
            InfrastructureDomain.TRANSIT: float(context.get_feature("p_transit", 0.0) or 0.0),
            InfrastructureDomain.UTILITY: float(context.get_feature("p_utility", 0.0) or 0.0),
            InfrastructureDomain.PUBLIC_FACILITY: float(context.get_feature("p_public_facility", 0.0) or 0.0),
        }

        forecasts: list[Forecast] = []
        medians: dict[str, float] = {}

        for domain, p in drivers.items():
            if p < 0.20:
                continue
            scale = 1.0 + 4.0 * p
            hazard = self.BASE_HAZARD[domain] * scale
            for horizon in (5, 15, 30, 60):
                p_by = 1.0 - math.exp(-hazard * horizon)
                low, high = _interval(p_by)
                forecasts.append(
                    Forecast(
                        target=f"{domain.value}_impact_present",
                        domain=domain,
                        horizon_minutes=horizon,
                        probability=round(p_by, 4),
                        lower=low,
                        upper=high,
                        model_id=self.model_id,
                        model_version=self.version,
                        truth_status=TruthStatus.PREDICTED,
                    )
                )
            med = math.log(0.5) / -hazard if hazard > 0 else 60.0
            medians[f"median_time_{domain.value}_min"] = round(med, 1)

        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.COMPLETED,
            is_placeholder=False,
            outputs=medians,
            forecasts=forecasts,
            confidence=0.85,
        )


class BaselineTimeToImpactModel(TimeToImpactModel):
    """Reference baseline for Model C with survival baseline enabled."""

    def __init__(self, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION, version: str = "v1.0.0") -> None:
        super().__init__(mode=mode, version=version, is_trained=True)


# --------------------------------------------------------------------------
# Model D: Traffic Prediction Model (section 25)
# --------------------------------------------------------------------------


class TrafficPredictionModel(PredictiveModel):
    """Model D: counterfactual baseline and traffic anomaly detection."""

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
        *,
        is_trained: bool = False,
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"traffic-anomaly-{version}",
                version=version,
                task_type=ModelTaskType.TRAFFIC_PREDICTION,
                deployment_mode=mode,
                description="Counterfactual traffic baseline and non-causal anomaly model.",
                feature_schema=["observed_speed_ratio", "day_type", "period"],
                specification_doc="docs/models/MODEL_D_TRAFFIC_PREDICTION.md",
            ),
            is_trained=is_trained,
        )

    BASELINE = {
        ("weekday", "am_peak"): 0.85,
        ("weekday", "midday"): 0.50,
        ("weekday", "pm_peak"): 0.90,
        ("weekday", "evening"): 0.45,
        ("weekend", "am_peak"): 0.40,
        ("weekend", "midday"): 0.35,
        ("weekend", "pm_peak"): 0.50,
        ("weekend", "evening"): 0.30,
    }

    def predict(self, context: ModelContext) -> ModelPrediction:
        if not self.is_trained:
            return self.untrained_prediction(context)

        observed = context.get_feature("observed_speed_ratio")
        day_type = context.get_feature("day_type", "weekday")
        period = context.get_feature("period", "pm_peak")

        expected = self.BASELINE.get((day_type, period), 0.50)
        anomaly = (float(observed) - expected) if observed is not None else 0.0

        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.COMPLETED,
            is_placeholder=False,
            outputs={
                "observed_speed_ratio": observed,
                "expected_baseline": expected,
                "anomaly": round(anomaly, 4),
                "is_congested": (float(observed) > 0.75) if observed is not None else False,
            },
            confidence=0.80 if observed is not None else 0.20,
            notes=["Attribution to specific event not asserted without validated causal model (Section 25)"],
        )


class BaselineTrafficPredictionModel(TrafficPredictionModel):
    """Reference baseline for Model D with counterfactual tables enabled."""

    def __init__(self, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION, version: str = "v1.0.0") -> None:
        super().__init__(mode=mode, version=version, is_trained=True)


# --------------------------------------------------------------------------
# Model E: Transit Disruption Model (section 26)
# --------------------------------------------------------------------------


class TransitDisruptionModel(PredictiveModel):
    """Model E (section 26): transit delay, cancellation probability, and stop accessibility."""

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
        *,
        is_trained: bool = False,
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"transit-disruption-{version}",
                version=version,
                task_type=ModelTaskType.TRANSIT_DISRUPTION,
                deployment_mode=mode,
                description="GTFS and real-time transit disruption forecasting model.",
                feature_schema=[
                    "transit_route_overlap",
                    "transit_stop_proximity",
                    "road_closure_active",
                    "delay_seconds",
                ],
                calibration_model="platt-scaling-v1",
                specification_doc="docs/models/MODEL_E_TRANSIT_DISRUPTION.md",
            ),
            is_trained=is_trained,
        )

    def predict(self, context: ModelContext) -> ModelPrediction:
        if not self.is_trained:
            return self.untrained_prediction(context)

        routes_affected = float(context.get_feature("transit_route_overlap", 0.0) or 0.0)
        stops_affected = float(context.get_feature("transit_stop_proximity", 0.0) or 0.0)
        road_closure = bool(context.get_feature("road_closure_active", False))
        observed_delay_s = float(context.get_feature("observed_delay_s", 0.0) or 0.0)

        expected_delay_min = (observed_delay_s / 60.0) if observed_delay_s > 0 else (
            (8.0 if road_closure else 4.0) * min(3.0, routes_affected)
        )
        cancellation_prob = _saturate(0.05 + 0.25 * (1.0 if road_closure else 0.0) + 0.10 * routes_affected)
        accessibility_reduction = _saturate(0.15 * stops_affected + 0.25 * (1.0 if road_closure else 0.0))

        forecasts = [
            Forecast(
                target="transit_service_cancellation",
                domain=InfrastructureDomain.TRANSIT,
                horizon_minutes=30,
                probability=round(cancellation_prob, 4),
                lower=round(max(0.0, cancellation_prob - 0.15), 4),
                upper=round(min(1.0, cancellation_prob + 0.15), 4),
                model_id=self.model_id,
                model_version=self.version,
                truth_status=TruthStatus.PREDICTED,
            )
        ]

        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.COMPLETED,
            is_placeholder=False,
            outputs={
                "expected_delay_minutes": round(expected_delay_min, 1),
                "cancellation_probability": round(cancellation_prob, 4),
                "accessibility_reduction": round(accessibility_reduction, 4),
                "alternate_transit_recommended": cancellation_prob > 0.40,
            },
            forecasts=forecasts,
            confidence=0.82,
            uncertainty=round(4.0 * cancellation_prob * (1.0 - cancellation_prob), 4),
        )


class BaselineTransitDisruptionModel(TransitDisruptionModel):
    """Reference baseline for Model E with GTFS delay heuristic enabled."""

    def __init__(self, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION, version: str = "v1.0.0") -> None:
        super().__init__(mode=mode, version=version, is_trained=True)


# --------------------------------------------------------------------------
# Model F: Spatial Propagation Model (section 27)
# --------------------------------------------------------------------------


class SpatialPropagationModel(PredictiveModel):
    """Model F (section 27): network cascade propagation across road/transit edges.

    Not implemented. The class exists so the portfolio has the right shape and
    so section 27's contract is testable, but there is no cascade-propagation
    inference here - only a specification. It therefore takes no ``is_trained``
    flag: with no implementation, no setting of that flag could honestly make
    this model capable of a prediction, and accepting it would only invite a
    caller to declare success it did not earn.

    The previous version did exactly that. ``is_trained=True`` returned
    ``COMPLETED`` with ``propagated_edges=[]``, ``max_hops=0`` and confidence
    0.80 - a cascade across zero edges reported as a confident finding. Cascade
    propagation is the kind of claim that, believed, sends someone down the
    wrong road, so an empty result is not a safe default here; it is the most
    dangerous one.
    """

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"spatial-propagation-{version}",
                version=version,
                task_type=ModelTaskType.SPATIAL_PROPAGATION,
                deployment_mode=mode,
                description="Network topology cascade propagation model.",
                feature_schema=[
                    "adjacency_matrix",
                    "seed_edge_disruptions",
                    "edge_capacities",
                    "movement_direction_vector",
                ],
                calibration_model="graph-conformal-v1",
                specification_doc="docs/models/MODEL_F_SPATIAL_PROPAGATION.md",
            ),
            is_trained=False,
        )

    def predict(self, context: ModelContext) -> ModelPrediction:
        return self._unimplemented(context)

    def _unimplemented(self, context: ModelContext) -> ModelPrediction:
        spec = self.metadata.specification_doc
        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.MODEL_REQUIRED,
            is_placeholder=True,
            outputs={
                "status": "MODEL_REQUIRED",
                "reason": "not_implemented",
                "specification": spec,
                "required_features": list(self.metadata.feature_schema),
                "propagated_edges": [],
                "max_hops": 0,
            },
            confidence=0.0,
            uncertainty=1.0,
            notes=[
                f"Model '{self.model_id}' has no implemented inference in this "
                f"build - only its specification at {spec}. It reports no "
                "propagation rather than reporting an empty propagation as a "
                "result. Note that 'no cascade observed' and 'not modelled' are "
                "different claims, and only the first is a prediction.",
            ],
        )


# --------------------------------------------------------------------------
# Model G: User Priority Ranking Model (section 36)
# --------------------------------------------------------------------------


class UserPriorityRankingModel(PredictiveModel):
    """Model G (section 36): Learning-to-Rank presentation item personalization.

    Not implemented, for the same reason as Model F. There is no ranker here,
    only a specification, so there is no ``is_trained`` flag to mislead anyone.
    Its previous ``is_trained=True`` path returned ``COMPLETED`` with an empty
    ``ranked_item_ids`` list at confidence 0.85 - a learned ranking that ranked
    nothing, at high confidence, in the one component that decides what a person
    is shown first. Presenting order is a safety-relevant default as much as a
    convenience, so the honest empty answer and the empty-result fake have to be
    told apart, and this model tells them apart by refusing to answer.
    """

    def __init__(
        self,
        mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION,
        version: str = "v1.0.0",
    ) -> None:
        super().__init__(
            ModelMetadata(
                model_id=f"user-priority-{version}",
                version=version,
                task_type=ModelTaskType.USER_PRIORITY_RANKING,
                deployment_mode=mode,
                description="Learning-to-Rank presentation item personalization model.",
                feature_schema=[
                    "impact_magnitude",
                    "user_exposure",
                    "urgency",
                    "change_magnitude",
                    "evidence_confidence",
                    "user_transport_mode",
                ],
                calibration_model="monotonic-rank-v1",
                specification_doc="docs/models/MODEL_G_USER_PRIORITY_RANKING.md",
            ),
            is_trained=False,
        )

    def predict(self, context: ModelContext) -> ModelPrediction:
        return self._unimplemented(context)

    def _unimplemented(self, context: ModelContext) -> ModelPrediction:
        spec = self.metadata.specification_doc
        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            status=PredictionStatus.MODEL_REQUIRED,
            is_placeholder=True,
            outputs={
                "status": "MODEL_REQUIRED",
                "reason": "not_implemented",
                "specification": spec,
                "required_features": list(self.metadata.feature_schema),
                "ranked_item_ids": [],
            },
            confidence=0.0,
            uncertainty=1.0,
            notes=[
                f"Model '{self.model_id}' has no implemented inference in this "
                f"build - only its specification at {spec}. Ranking falls back "
                "to the deterministic priority rules, which are auditable; an "
                "empty learned ranking at high confidence would not be.",
            ],
        )


def build_default_model_registry(*, require_trained: bool = True) -> Any:
    """Build and populate the default ModelRegistry with champion models.

    When require_trained=True (the default, and what production uses):
      Registers the models in their clean, un-trained state where they return
      PredictionStatus.MODEL_REQUIRED and zero fabricated forecasts, pointing
      directly to their documentation specifications.

    When require_trained=False:
      Registers baseline reference models enabled for testing and mathematical
      baseline evaluation. Models A-E have hand-built deterministic baselines to
      measure learned models against; Models F and G have none, so they register
      in the same unimplemented state as above rather than registering an empty
      result that would read as a baseline score of zero when it means
      "not measured".
    """
    from .registry import ModelRegistry

    registry = ModelRegistry()
    if require_trained:
        registry.register(EventClassificationModel(is_trained=False))
        registry.register(InfrastructureImpactModel(is_trained=False))
        registry.register(TimeToImpactModel(is_trained=False))
        registry.register(TrafficPredictionModel(is_trained=False))
        registry.register(TransitDisruptionModel(is_trained=False))
        registry.register(SpatialPropagationModel())
        registry.register(UserPriorityRankingModel())
    else:
        registry.register(BaselineEventClassifier())
        registry.register(BaselineInfrastructureImpactModel())
        registry.register(BaselineTimeToImpactModel())
        registry.register(BaselineTrafficPredictionModel())
        registry.register(BaselineTransitDisruptionModel())
        registry.register(SpatialPropagationModel())
        registry.register(UserPriorityRankingModel())
    return registry


__all__ = [
    "BaselineEventClassifier",
    "BaselineInfrastructureImpactModel",
    "BaselineTimeToImpactModel",
    "BaselineTrafficPredictionModel",
    "BaselineTransitDisruptionModel",
    "EventClassificationModel",
    "InfrastructureImpactModel",
    "SpatialPropagationModel",
    "TimeToImpactModel",
    "TrafficPredictionModel",
    "TransitDisruptionModel",
    "UserPriorityRankingModel",
    "build_default_model_registry",
]

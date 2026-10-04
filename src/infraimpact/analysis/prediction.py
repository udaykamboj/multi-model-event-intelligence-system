"""Predictive capabilities (brief sections 13-15, 23-27).

These are *interpretable baselines*, not the final model. They are written so
the ML-flavoured parts (logistic scoring, time-to-impact survival form,
counterfactual traffic baseline) are structurally right, and so a trained model
can later be swapped in behind the same interface and be *measured* against
these baselines.

Three deliberate restraints:
  1. A traffic anomaly is never asserted to be event-caused (section 25).
  2. Probabilities carry a calibration identifier from day one (section 45).
  3. Every forecast stores model id, version and features version (section 44).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain.enums import CapabilityTier, InfrastructureDomain, ObservationType, TruthStatus
from ..domain.ids import utcnow
from ..domain.schemas import FeatureValue, Forecast, ModelOutput
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult

CALIBRATION_VERSION = "heuristic-v1"


@dataclass(frozen=True)
class LogisticWeights:
    """Explicit coefficients. Interpretability is a feature, not a limitation."""

    intercept: float = -2.6
    event_moving: float = 0.55
    event_crowd: float = 0.9
    event_expansion: float = 0.7
    arterial_overlap: float = 1.4
    road_overlap: float = 0.6
    transit_overlap: float = 0.8
    duration_hours: float = 0.35
    rush_hour: float = 0.3
    source_confidence: float = 0.8
    propagation_reach: float = 0.5
    route_redundancy: float = -0.7


class EventClassificationCapability(AnalysisCapability):
    """Model A: event-type probability distribution."""

    capability_id = "event_classification"
    tier = CapabilityTier.CHEAP
    description = "Soft classification of the event type from observation evidence."
    estimated_latency_ms = 15
    estimated_cost = 0.02
    model_version = "heuristic-classifier-v1"

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        types = ctx.observation_types
        scores: dict[str, float] = {}

        if ObservationType.PERMIT_EVENT in types:
            scores["demonstration_planned"] = 0.85
        if ObservationType.POLICE_RESPONSE in types:
            scores["public_gathering"] = 0.7
        if ObservationType.NEWS_ARTICLE in types:
            scores["public_gathering"] = max(scores.get("public_gathering", 0.0), 0.55)
        if ObservationType.EARTHQUAKE in types:
            scores["earthquake"] = 0.97
        if ObservationType.OFFICIAL_EMERGENCY_NOTICE in types:
            scores["official_emergency"] = 0.9
        if ObservationType.SEVERE_WEATHER in types:
            scores["severe_weather"] = 0.8
        if ObservationType.WILDFIRE in types:
            scores["wildfire"] = 0.9
        if ObservationType.ROAD_CLOSURE in types and not scores:
            scores["transportation_incident"] = 0.6

        # A road closure caused by a "special event" is evidence *for* a
        # demonstration, not against it.
        if any(
            "special event" in str(o.structured_payload.get("reason", "")).lower()
            for o in ctx.observations
        ):
            scores["demonstration_planned"] = max(scores.get("demonstration_planned", 0.0), 0.6)

        if not scores:
            scores["unknown"] = 1.0

        total = sum(scores.values()) or 1.0
        distribution = {k: round(v / total, 4) for k, v in scores.items()}
        dominant, probability = max(distribution.items(), key=lambda kv: kv[1])

        result.features["event_class"] = FeatureValue(
            name="event_class", value=dominant, confidence=probability
        )
        result.features["event_class_confidence"] = FeatureValue(
            name="event_class_confidence", value=probability
        )
        result.features["event_class_distribution"] = FeatureValue(
            name="event_class_distribution", value=distribution
        )
        result.model_outputs.append(
            ModelOutput(
                model_id="heuristic-classifier",
                model_version=self.model_version,
                prediction_time=utcnow(),
                input_state_version=ctx.state.state_version,
                output=distribution,
                probability=probability,
                calibration_version=CALIBRATION_VERSION,
            )
        )
        return result


class TrafficAnomalyCapability(AnalysisCapability):
    """Section 25: counterfactual baseline, and no causal claim.

    Traffic anomaly = observed - expected. It is emphatically *not* attributed
    to the event without a validated attribution model, because a downtown
    anomaly at rush hour has many candidate causes.
    """

    capability_id = "traffic_anomaly"
    tier = CapabilityTier.TRIGGERED
    description = "Traffic deviation against a weekday/time baseline. Not causally attributed."
    triggered_by = frozenset({ObservationType.TRAFFIC_CONDITION, ObservationType.TRAFFIC_FLOW, ObservationType.TRAVEL_TIME})
    domains = frozenset({InfrastructureDomain.ROAD})
    estimated_latency_ms = 60
    estimated_cost = 0.1
    model_version = "baseline-expected-v1"

    #: Baseline congestion by weekday x period. Replace with a fitted model
    #: trained through ``infraimpact.evaluation`` as data accumulates.
    BASELINE = {
        ("weekday", "am_peak"): 0.85,
        ("weekday", "midday"): 0.5,
        ("weekday", "pm_peak"): 0.9,
        ("weekday", "evening"): 0.45,
        ("weekend", "am_peak"): 0.4,
        ("weekend", "midday"): 0.35,
        ("weekend", "pm_peak"): 0.5,
        ("weekend", "evening"): 0.3,
    }

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        observed = [
            float(o.structured_payload.get("speed_ratio") or o.structured_payload.get("congestion_ratio") or 0.0)
            for o in ctx.observations
            if o.observation_type in {ObservationType.TRAFFIC_CONDITION, ObservationType.TRAFFIC_FLOW}
            and (o.structured_payload.get("speed_ratio") or o.structured_payload.get("congestion_ratio"))
        ]
        expected = self.BASELINE.get(self._period(ctx), 0.5)

        if not observed:
            result.features["traffic_anomaly"] = FeatureValue(
                name="traffic_anomaly",
                value=None,
                confidence=0.0,
            )
            result.features["traffic_baseline"] = FeatureValue(
                name="traffic_baseline", value=expected
            )
            result.notes.append("no traffic observation available; anomaly not computed")
            return result

        mean_observed = sum(observed) / len(observed)
        anomaly = mean_observed - expected
        result.features.update(
            {
                "traffic_observed": FeatureValue(name="traffic_observed", value=round(mean_observed, 4)),
                "traffic_baseline": FeatureValue(name="traffic_baseline", value=expected),
                "traffic_anomaly": FeatureValue(
                    name="traffic_anomaly",
                    value=round(anomaly, 4),
                    confidence=min(1.0, len(observed) / 5.0),
                ),
            }
        )
        # Explicitly record non-attribution so downstream consumers cannot
        # mistake an anomaly for proof of event causation.
        result.notes.append(
            f"traffic anomaly {anomaly:+.2f} vs baseline {expected:.2f}; "
            "attribution to this event NOT asserted"
        )
        return result

    @staticmethod
    def _period(ctx: CapabilityContext) -> tuple[str, str]:
        local_hour = ctx.state.first_observed.hour if ctx.state.first_observed else 12
        day_type = "weekend" if ctx.state.first_observed and ctx.state.first_observed.weekday() >= 5 else "weekday"
        if 7 <= local_hour < 10:
            period = "am_peak"
        elif 11 <= local_hour < 14:
            period = "midday"
        elif 15 <= local_hour < 19:
            period = "pm_peak"
        else:
            period = "evening"
        return (day_type, period)


class InfrastructureImpactCapability(AnalysisCapability):
    """Model B: probability of each infrastructure impact type."""

    capability_id = "infrastructure_impact"
    tier = CapabilityTier.TRIGGERED
    description = "Probability of road / transit / utility / public-facility impact."
    domains = frozenset(
        {
            InfrastructureDomain.ROAD,
            InfrastructureDomain.TRANSIT,
            InfrastructureDomain.UTILITY,
            InfrastructureDomain.PUBLIC_FACILITY,
        }
    )
    estimated_latency_ms = 120
    estimated_cost = 0.25
    model_version = "interpretable-logit-v1"

    WEIGHTS = LogisticWeights()

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        if not ctx.observation_types & {
            ObservationType.ROAD_CLOSURE,
            ObservationType.TRANSIT_SERVICE_ALERT,
            ObservationType.POLICE_RESPONSE,
            ObservationType.NEWS_ARTICLE,
            ObservationType.PERMIT_EVENT,
            ObservationType.FIRE_DISPATCH,
            ObservationType.OFFICIAL_EMERGENCY_NOTICE,
        }:
            return result

        w = self.WEIGHTS
        crowd = float(ctx.state.derived.get("estimated_crowd") or 0.0)
        duration_h = _duration_hours(ctx)

        x = (
            w.intercept
            + w.event_moving * ctx.state.derived.get("moving", 0.0)
            + w.event_crowd * _saturate(crowd / 1000.0)
            + w.event_expansion * float(ctx.state.derived.get("expansion", 0.0))
            + w.arterial_overlap * float(ctx.feature("arterial_overlap_count", 0.0) or 0.0)
            + w.road_overlap * _saturate(float(ctx.feature("road_overlap_count", 0.0) or 0.0) / 4.0)
            + w.transit_overlap * _saturate(float(ctx.feature("transit_route_overlap", 0.0) or 0.0) / 3.0)
            + w.duration_hours * duration_h
            + w.rush_hour * (1.0 if _is_rush_hour(ctx) else 0.0)
            + w.source_confidence * ctx.state.evidence.vector.get("source_authority", 0.3)
            + w.propagation_reach * _saturate(float(ctx.feature("propagation_reach", 0.0) or 0.0) / 20.0)
            + w.route_redundancy * float(ctx.feature("route_redundancy", 1.0) or 1.0)
        )

        p_road = _sigmoid(x)
        p_transit = _sigmoid(x - 0.35 * float(ctx.feature("transit_route_overlap", 0.0) or 0.0))
        p_utility = _sigmoid(x - 2.4)
        p_facility = _sigmoid(x - 2.0 + 0.5 * float(ctx.feature("critical_facility_inside", 0.0) or 0.0))

        horizons = (5, 15, 30, 60)
        for target, domain, probability in (
            ("road_disruption", InfrastructureDomain.ROAD, p_road),
            ("transit_disruption", InfrastructureDomain.TRANSIT, p_transit),
            ("utility_disruption", InfrastructureDomain.UTILITY, p_utility),
            ("public_facility_impact", InfrastructureDomain.PUBLIC_FACILITY, p_facility),
        ):
            for horizon in horizons:
                cumulative = _cumulative_by_horizon(probability, horizon)
                lower, upper = _interval(cumulative)
                result.forecasts.append(
                    Forecast(
                        target=target,
                        domain=domain,
                        horizon_minutes=horizon,
                        probability=round(cumulative, 4),
                        lower=lower,
                        upper=upper,
                        model_id="interpretable-logit",
                        model_version=self.model_version,
                        calibration_version=CALIBRATION_VERSION,
                        truth_status=TruthStatus.PREDICTED,
                    )
                )
                result.features[f"p_{target}"] = FeatureValue(
                    name=f"p_{target}",
                    value=round(probability, 4),
                    confidence=_interval_width(cumulative),
                )

        result.model_outputs.append(
            ModelOutput(
                model_id="interpretable-logit",
                model_version=self.model_version,
                prediction_time=utcnow(),
                input_state_version=ctx.state.state_version,
                output={
                    "p_road": round(p_road, 4),
                    "p_transit": round(p_transit, 4),
                    "p_utility": round(p_utility, 4),
                    "p_public_facility": round(p_facility, 4),
                },
                probability=round(max(p_road, p_transit), 4),
                uncertainty=round(_interval_width(p_road), 4),
                calibration_version=CALIBRATION_VERSION,
            )
        )
        return result


class TimeToImpactCapability(AnalysisCapability):
    """Section 24: time-to-event in survival form, with explicit censoring.

    Rather than "will transit be affected?" we ask "how long until it likely is?",
    because that is the question a user actually has.
    """

    capability_id = "time_to_impact"
    tier = CapabilityTier.TRIGGERED
    description = "Hazard-based time-to-impact over 5/15/30/60 minute horizons."
    domains = frozenset({InfrastructureDomain.ROAD, InfrastructureDomain.TRANSIT})
    estimated_latency_ms = 80
    estimated_cost = 0.12
    model_version = "parametric-hazard-v1"

    #: Instantaneous hazard rates per minute, informed by observed state.
    BASE_HAZARD = {
        InfrastructureDomain.ROAD: 0.010,
        InfrastructureDomain.TRANSIT: 0.006,
        InfrastructureDomain.UTILITY: 0.002,
        InfrastructureDomain.PUBLIC_FACILITY: 0.003,
    }

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        drivers = {
            InfrastructureDomain.ROAD: float(ctx.feature("p_road_disruption", 0.0) or 0.0),
            InfrastructureDomain.TRANSIT: float(ctx.feature("p_transit_disruption", 0.0) or 0.0),
            InfrastructureDomain.UTILITY: float(ctx.feature("p_utility_disruption", 0.0) or 0.0),
            InfrastructureDomain.PUBLIC_FACILITY: float(
                ctx.feature("p_public_facility_impact", 0.0) or 0.0
            ),
        }
        active = [d for d, p in drivers.items() if p >= 0.25]
        if not active:
            return result

        for domain in active:
            # Hazard scales with the predicted probability; higher probability
            # concentrates mass earlier.
            scale = 1.0 + 4.0 * drivers[domain]
            hazard = self.BASE_HAZARD[domain] * scale
            for horizon in (5, 15, 30, 60):
                p_by = 1.0 - math.exp(-hazard * horizon)
                lower, upper = _interval(p_by)
                result.forecasts.append(
                    Forecast(
                        target=f"{domain.value}_impact_present",
                        domain=domain,
                        horizon_minutes=horizon,
                        probability=round(p_by, 4),
                        lower=lower,
                        upper=upper,
                        model_id="parametric-hazard",
                        model_version=self.model_version,
                        calibration_version=CALIBRATION_VERSION,
                        truth_status=TruthStatus.PREDICTED,
                    )
                )
            median = math.log(0.5) / -hazard if hazard > 0 else None
            result.features[f"median_time_to_{domain.value}_impact_min"] = FeatureValue(
                name=f"median_time_to_{domain.value}_impact_min",
                value=round(median, 2) if median else None,
                unit="minutes",
            )
        return result


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _saturate(x: float) -> float:
    return max(0.0, min(1.0, x))


def _cumulative_by_horizon(p_ever: float, horizon: int) -> float:
    """Convert "impact occurs at some point" to "impact present by horizon".

    Assumes the hazard rises in the first half hour (as a crowd reaches critical
    mass) then flattens, which matches observed event dynamics far better than
    a flat rate.
    """
    shape = {5: 0.12, 15: 0.42, 30: 0.72, 60: 1.0}.get(horizon, horizon / 60.0)
    return _saturate(p_ever * shape)


def _interval(p: float, width: float = 0.28) -> tuple[float, float]:
    return (round(max(0.0, p - width / 2), 4), round(min(1.0, p + width / 2), 4))


def _interval_width(p: float) -> float:
    # Uncertainty is widest near 0.5 and narrowest at the extremes - the
    # standard behaviour of a well-behaved probability.
    return round(4.0 * p * (1.0 - p), 4)


def _duration_hours(ctx: CapabilityContext) -> float:
    if ctx.state.first_observed is None or ctx.state.last_observed is None:
        return 0.0
    hours = (ctx.state.last_observed - ctx.state.first_observed).total_seconds() / 3600.0
    return _saturate(hours)


def _is_rush_hour(ctx: CapabilityContext) -> bool:
    if ctx.state.first_observed is None:
        return False
    if ctx.state.first_observed.weekday() >= 5:
        return False
    hour = ctx.state.first_observed.hour
    return (7 <= hour < 10) or (15 <= hour < 19)


__all__ = [
    "CALIBRATION_VERSION",
    "EventClassificationCapability",
    "InfrastructureImpactCapability",
    "LogisticWeights",
    "TimeToImpactCapability",
    "TrafficAnomalyCapability",
]
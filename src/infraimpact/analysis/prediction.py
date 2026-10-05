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
    """Counterfactual baseline and traffic anomaly detection.

    Reports deviation from an expectation; never attributes the deviation to this
    event (section 25).

    The honest boundary here is measurement. A road closure makes congestion
    *likely*, and it says nothing about whether congestion is happening or how
    bad it is. This capability previously crossed that line in three ways, each
    of which produced numbers that looked measured and were not:

      - With a closure but no traffic observation it set the observed ratio to
        0.45 x baseline and confidence 0.70. That is a fabricated measurement,
        and it was indistinguishable downstream from one that had been observed.
      - It converted ratios to mph by multiplying by an invented 35 mph
        free-flow speed, then derived travel time and delay from a hardcoded
        600 s nominal. The segment's actual free-flow speed was never known.
      - With no affected segments identified it reported the literal identifier
        ``road:4th-ave``, naming a specific real street that appeared nowhere in
        the evidence.

    So: the baseline table below is still emitted, because an expectation is a
    fact about expectations. Observed quantities are emitted only when an
    observation actually carries them. When nothing has been measured, this
    reports that nothing has been measured, and that is a complete answer.
    """

    capability_id = "traffic_anomaly"
    tier = CapabilityTier.TRIGGERED
    description = "Traffic deviation against a weekday/time baseline. Not causally attributed."
    triggered_by = frozenset({ObservationType.TRAFFIC_CONDITION, ObservationType.TRAFFIC_FLOW, ObservationType.TRAVEL_TIME})
    domains = frozenset({InfrastructureDomain.ROAD})
    estimated_latency_ms = 60
    estimated_cost = 0.1
    model_version = "baseline-expected-v1"

    #: Expected speed ratio (observed / free-flow) by weekday x period. An
    #: explicit, inspectable prior to be replaced by a fitted model through
    #: ``infraimpact.evaluation``. Replace with a fitted model trained through
    #: ``infraimpact.evaluation`` as data accumulates.
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

    #: Ratio thresholds for naming a congestion level. Declared here rather than
    #: inlined so the classification is auditable as a policy choice.
    CONGESTION_BANDS = (
        (0.40, "GRIDLOCK"),
        (0.60, "SEVERE"),
        (0.75, "MODERATE"),
        (0.90, "MINOR"),
    )

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        expected = self.BASELINE.get(self._period(ctx), 0.5)

        observed = self._observed_ratios(ctx)
        speeds = self._observed_speeds(ctx)
        travel_times = self._observed_travel_times(ctx)

        has_closure = any(o.observation_type == ObservationType.ROAD_CLOSURE for o in ctx.observations) or any(
            i.domain == InfrastructureDomain.ROAD for i in ctx.state.affected_infrastructure
        )
        # Only identifiers the evidence actually carries. No invented segments.
        affected_segments = [
            i.identifier for i in ctx.state.affected_infrastructure if i.domain == InfrastructureDomain.ROAD
        ]

        # The expectation is reportable either way; it describes the baseline,
        # not the world.
        result.features["traffic_baseline_expected_ratio"] = FeatureValue(
            name="traffic_baseline_expected_ratio",
            value=expected,
            unit="ratio",
            as_of=ctx.state.last_observed or utcnow(),
        )

        if affected_segments:
            result.features["traffic_affected_road_segments"] = FeatureValue(
                name="traffic_affected_road_segments",
                value=affected_segments,
            )

        if not observed and not speeds and not travel_times:
            return self._unmeasured(
                result,
                ctx,
                expected=expected,
                has_closure=has_closure,
                affected_segments=affected_segments,
            )

        # Ratio scale: from real ratios, or from real speeds over a real
        # free-flow reference. Never from an assumed free-flow speed.
        if observed:
            measured_ratio = sum(observed) / len(observed)
            ratio_confidence = min(1.0, 0.4 + 0.15 * len(observed))
            ratio_basis = f"{len(observed)} reported speed ratio(s)"
        elif speeds and speeds[0]["free_flow_mph"]:
            measured_ratio = sum(s["speed_mph"] / s["free_flow_mph"] for s in speeds) / len(speeds)
            ratio_confidence = min(1.0, 0.4 + 0.15 * len(speeds))
            ratio_basis = f"{len(speeds)} reported speed(s) over reported free-flow"
        else:
            measured_ratio = None
            ratio_confidence = 0.0
            ratio_basis = "no speed ratio or free-flow reference available"

        if measured_ratio is not None:
            anomaly = round(measured_ratio - expected, 4)
            cong_level = self._congestion_level(measured_ratio)

            result.features["traffic_observed_speed_ratio"] = FeatureValue(
                name="traffic_observed_speed_ratio",
                value=round(measured_ratio, 4),
                unit="ratio",
                confidence=ratio_confidence,
                as_of=ctx.state.last_observed or utcnow(),
            )
            result.features["traffic_anomaly"] = FeatureValue(
                name="traffic_anomaly",
                value=anomaly,
                unit="ratio",
                confidence=ratio_confidence,
                as_of=ctx.state.last_observed or utcnow(),
            )
            result.features["traffic_congestion_level"] = FeatureValue(
                name="traffic_congestion_level",
                value=cong_level,
                confidence=ratio_confidence,
            )
            result.features["traffic_measurement_available"] = FeatureValue(
                name="traffic_measurement_available",
                value=True,
                confidence=1.0,
            )

        # Absolute speeds only when absolute speeds were reported.
        current_speed = base_speed = None
        if speeds:
            current_speed = round(sum(s["speed_mph"] for s in speeds) / len(speeds), 1)
            result.features["traffic_current_speed_mph"] = FeatureValue(
                name="traffic_current_speed_mph",
                value=current_speed,
                unit="mph",
                confidence=min(1.0, 0.4 + 0.15 * len(speeds)),
                as_of=ctx.state.last_observed or utcnow(),
            )
            if all(s["free_flow_mph"] for s in speeds):
                base_speed = round(sum(s["free_flow_mph"] for s in speeds) / len(speeds), 1)
                result.features["traffic_expected_baseline_speed_mph"] = FeatureValue(
                    name="traffic_expected_baseline_speed_mph",
                    value=base_speed,
                    unit="mph",
                )

        # Travel time and delay only when travel time and free-flow were
        # reported. Delay is the difference between two measurements.
        delay_sec = delay_pct = None
        if travel_times:
            current_tt = round(sum(t["travel_time_s"] for t in travel_times) / len(travel_times), 1)
            result.features["traffic_travel_time_seconds"] = FeatureValue(
                name="traffic_travel_time_seconds",
                value=current_tt,
                unit="seconds",
                as_of=ctx.state.last_observed or utcnow(),
            )
            free_flow_tt = [t["free_flow_s"] for t in travel_times if t["free_flow_s"]]
            if free_flow_tt:
                nominal = round(sum(free_flow_tt) / len(free_flow_tt), 1)
                result.features["traffic_baseline_travel_time_seconds"] = FeatureValue(
                    name="traffic_baseline_travel_time_seconds",
                    value=nominal,
                    unit="seconds",
                )
                delay_sec = round(max(0.0, current_tt - nominal), 1)
                delay_pct = round((delay_sec / nominal) * 100.0, 1) if nominal else None
                result.features["traffic_delay_seconds"] = FeatureValue(
                    name="traffic_delay_seconds",
                    value=delay_sec,
                    unit="seconds",
                )
                if delay_pct is not None:
                    result.features["traffic_delay_percentage"] = FeatureValue(
                        name="traffic_delay_percentage",
                        value=delay_pct,
                        unit="percent",
                    )

        result.features["traffic_confidence"] = FeatureValue(
            name="traffic_confidence",
            value=ratio_confidence,
        )

        result.model_outputs.append(
            ModelOutput(
                model_id="baseline-traffic-anomaly",
                model_version=self.model_version,
                prediction_time=utcnow(),
                input_state_version=ctx.state.state_version,
                output={
                    "status": "COMPLETED",
                    "measurement_available": True,
                    "measurement_basis": ratio_basis,
                    "expected_ratio": expected,
                    "observed_ratio": round(measured_ratio, 4) if measured_ratio is not None else None,
                    "anomaly_ratio": round(measured_ratio - expected, 4) if measured_ratio is not None else None,
                    "congestion_level": self._congestion_level(measured_ratio) if measured_ratio is not None else None,
                    "current_speed_mph": current_speed,
                    "free_flow_speed_mph": base_speed,
                    "delay_seconds": delay_sec,
                    "delay_percentage": delay_pct,
                },
                # Confidence in the deviation, from measurement count only.
                # Not a probability of congestion, which this model does not
                # estimate.
                probability=round(ratio_confidence, 4),
                calibration_version=CALIBRATION_VERSION,
            )
        )

        if measured_ratio is not None:
            result.notes.append(
                f"traffic anomaly {anomaly:+.2f} vs baseline {expected:.2f} ({cong_level}) "
                f"from {ratio_basis}; attribution to this event NOT asserted"
            )
        else:
            result.notes.append(
                "traffic reported absolute measurements but no speed ratio or "
                "free-flow reference, so no deviation is claimed"
            )
        return result

    def _unmeasured(
        self,
        result: CapabilityResult,
        ctx: CapabilityContext,
        *,
        expected: float,
        has_closure: bool,
        affected_segments: list[str],
    ) -> CapabilityResult:
        """No traffic has been measured. Report exactly that.

        The previous version of this path returned an observed ratio of
        ``expected * 0.45`` at confidence 0.70 whenever a closure was present,
        which is a fabricated observation with a fabricated confidence attached
        to it. A closure is evidence about infrastructure, not about traffic
        flow, so it is recorded as context and no traffic quantity is derived
        from it.
        """

        result.features["traffic_measurement_available"] = FeatureValue(
            name="traffic_measurement_available",
            value=False,
            confidence=1.0,
        )
        result.features["traffic_confidence"] = FeatureValue(
            name="traffic_confidence",
            value=0.0,
        )

        result.model_outputs.append(
            ModelOutput(
                model_id="baseline-traffic-anomaly",
                model_version=self.model_version,
                prediction_time=utcnow(),
                input_state_version=ctx.state.state_version,
                output={
                    "status": "NO_MEASUREMENT",
                    "measurement_available": False,
                    "measurement_basis": "no traffic observation carried a speed, ratio or travel time",
                    "expected_ratio": expected,
                    "observed_ratio": None,
                    "anomaly_ratio": None,
                    "congestion_level": None,
                    "current_speed_mph": None,
                    "free_flow_speed_mph": None,
                    "delay_seconds": None,
                    "delay_percentage": None,
                    "road_closure_present": has_closure,
                },
                # No deviation was measured, so no confidence in one is claimed.
                probability=None,
                calibration_version=CALIBRATION_VERSION,
            )
        )

        if has_closure:
            result.notes.append(
                "road closure present but no traffic measurement available; no "
                "traffic anomaly, delay or congestion level is reported. "
                "Congestion is plausible and unobserved - these are different, "
                "and only the second is reportable."
            )
        else:
            result.notes.append(
                "no traffic measurement available for this event; baseline "
                f"expectation {expected:.2f} recorded, nothing observed"
            )
        return result

    @staticmethod
    def _observed_ratios(ctx: CapabilityContext) -> list[float]:
        """Speed ratios the evidence actually carries."""

        found: list[float] = []
        for o in ctx.observations:
            if o.observation_type not in {ObservationType.TRAFFIC_CONDITION, ObservationType.TRAFFIC_FLOW}:
                continue
            raw = o.structured_payload.get("speed_ratio") or o.structured_payload.get("congestion_ratio")
            if raw is None:
                continue
            found.append(float(raw))
        return found

    @staticmethod
    def _observed_speeds(ctx: CapabilityContext) -> list[dict[str, float]]:
        """Absolute speeds, only where reported alongside their reference."""

        found: list[dict[str, float]] = []
        for o in ctx.observations:
            if o.observation_type not in {ObservationType.TRAFFIC_CONDITION, ObservationType.TRAFFIC_FLOW}:
                continue
            speed = o.structured_payload.get("speed_mph")
            if speed is None:
                continue
            free_flow = o.structured_payload.get("free_flow_speed_mph")
            found.append({"speed_mph": float(speed), "free_flow_mph": float(free_flow) if free_flow else 0.0})
        return found

    @staticmethod
    def _observed_travel_times(ctx: CapabilityContext) -> list[dict[str, float]]:
        """Travel times, only where reported alongside their free-flow reference."""

        found: list[dict[str, float]] = []
        for o in ctx.observations:
            if o.observation_type != ObservationType.TRAVEL_TIME:
                continue
            travel = o.structured_payload.get("travel_time_seconds")
            if travel is None:
                continue
            free_flow = o.structured_payload.get("free_flow_travel_time_seconds")
            found.append({"travel_time_s": float(travel), "free_flow_s": float(free_flow) if free_flow else 0.0})
        return found

    @classmethod
    def _congestion_level(cls, ratio: float) -> str:
        for threshold, label in cls.CONGESTION_BANDS:
            if ratio < threshold:
                return label
        return "NORMAL"

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
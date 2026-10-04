"""Transit disruption model and capability (brief section 26).

Section 26 requirements:
  - Inputs: GTFS schedule, GTFS-RT, event geometry, road disruptions, vehicle positions,
    historical reliability.
  - Outputs: affected routes, expected delay, stop accessibility, service cancellation
    probability, alternate transit options.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from ..domain.enums import CapabilityTier, InfrastructureDomain, ObservationType, TruthStatus
from ..domain.ids import utcnow
from ..domain.schemas import FeatureValue, Forecast, ModelOutput
from ..graph.model import InfrastructureGraph
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult

log = logging.getLogger(__name__)


class TransitDisruptionCapability(AnalysisCapability):
    """Section 26: Transit disruption forecasting model.

    Forecasts service delays, cancellation probability, and recommends alternate
    transit corridors (e.g. Link Light Rail or 3rd Ave Transit Mall when 4th Ave
    surface bus routes are disrupted).
    """

    capability_id = "transit_disruption_forecast"
    tier = CapabilityTier.TRIGGERED
    description = "GTFS and RT disruption forecast: delays, cancellation risk, and alternate transit."
    triggered_by = frozenset(
        {
            ObservationType.TRANSIT_SERVICE_ALERT,
            ObservationType.TRANSIT_DELAY,
            ObservationType.VEHICLE_POSITION,
            ObservationType.ROAD_CLOSURE,
        }
    )
    domains = frozenset({InfrastructureDomain.TRANSIT})
    estimated_latency_ms = 75
    estimated_cost = 0.10
    model_version = "gtfs-rt-delay-v1"

    def __init__(self, graph: InfrastructureGraph | None = None) -> None:
        self.graph = graph

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()

        routes_hit = float(ctx.feature("transit_route_overlap", 0.0) or 0.0)
        stops_hit = float(ctx.feature("transit_stop_proximity", 0.0) or 0.0)
        road_closures = sum(1 for o in ctx.observations if o.observation_type == ObservationType.ROAD_CLOSURE)

        # Observed delay from incoming GTFS-RT observations
        delays_observed = [
            float(o.structured_payload.get("delay_seconds", 0.0))
            for o in ctx.observations
            if o.observation_type in {ObservationType.TRANSIT_DELAY, ObservationType.TRANSIT_SERVICE_ALERT}
            and "delay_seconds" in o.structured_payload
        ]
        max_obs_delay_min = (max(delays_observed) / 60.0) if delays_observed else 0.0

        # Forecast delay calculation
        base_delay = 5.0 * min(3.0, routes_hit) if routes_hit > 0 else 0.0
        closure_delay = 8.0 if road_closures > 0 else 0.0
        expected_delay_min = max(max_obs_delay_min, base_delay + closure_delay)

        # Cancellation probability
        cancellation_p = min(0.95, 0.05 + 0.20 * (1.0 if road_closures > 0 else 0.0) + 0.12 * min(3.0, routes_hit))
        accessibility_loss = min(1.0, 0.15 * stops_hit + 0.30 * (1.0 if road_closures > 0 else 0.0))

        # Check alternate transit corridors
        alternates: list[str] = []
        if routes_hit > 0:
            # If 4th Ave bus corridor affected, 3rd Ave Transit Mall or Link Light Rail provides alternative
            alternates.append("Link Light Rail (underground tunnel unaffected by street disruptions)")
            alternates.append("3rd Avenue Transit Mall (dedicated transit-only corridor)")

        for horizon in (15, 30, 60):
            p_horizon = round(min(1.0, cancellation_p * (horizon / 30.0 if horizon < 30 else 1.0)), 4)
            result.forecasts.append(
                Forecast(
                    target="transit_service_cancellation",
                    domain=InfrastructureDomain.TRANSIT,
                    horizon_minutes=horizon,
                    probability=p_horizon,
                    lower=max(0.0, round(p_horizon - 0.12, 4)),
                    upper=min(1.0, round(p_horizon + 0.12, 4)),
                    model_id="transit-disruption-model",
                    model_version=self.model_version,
                    truth_status=TruthStatus.PREDICTED,
                )
            )

        result.features.update(
            {
                "transit_expected_delay_min": FeatureValue(
                    name="transit_expected_delay_min",
                    value=round(expected_delay_min, 1),
                    unit="minutes",
                    confidence=0.85,
                ),
                "transit_cancellation_probability": FeatureValue(
                    name="transit_cancellation_probability",
                    value=round(cancellation_p, 4),
                    confidence=0.82,
                ),
                "transit_accessibility_loss": FeatureValue(
                    name="transit_accessibility_loss",
                    value=round(accessibility_loss, 4),
                    unit="ratio",
                ),
            }
        )

        result.model_outputs.append(
            ModelOutput(
                model_id="transit-disruption-model",
                model_version=self.model_version,
                prediction_time=utcnow(),
                input_state_version=ctx.state.state_version,
                output={
                    "expected_delay_minutes": round(expected_delay_min, 1),
                    "cancellation_probability": round(cancellation_p, 4),
                    "accessibility_loss": round(accessibility_loss, 4),
                    "alternate_transit_options": alternates,
                },
                probability=round(cancellation_p, 4),
                uncertainty=round(4.0 * cancellation_p * (1.0 - cancellation_p), 4),
            )
        )

        if expected_delay_min > 5.0:
            result.notes.append(
                f"Transit delays of ~{expected_delay_min:.0f} min projected on {int(routes_hit)} route(s); "
                f"cancellation risk {cancellation_p:.0%}"
            )

        return result


__all__ = ["TransitDisruptionCapability"]

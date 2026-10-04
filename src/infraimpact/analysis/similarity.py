"""Historical similarity engine (brief section 31).

Section 31 requirements:
  - "Every state snapshot receives an embedding and structured feature representation."
  - "Historical retrieval runs two searches: structured nearest-neighbor similarity + semantic/vector similarity."
  - "Result: Current state -> similar historical states -> their future trajectories -> additional evidence for forecasting."
  - "Use historical analogues as model evidence - not as deterministic predictions."
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..domain.enums import CapabilityTier, InfrastructureDomain, ObservationType
from ..domain.geo import centroid_of, distance_m
from ..domain.schemas import FeatureValue
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult


@dataclass(frozen=True)
class HistoricalAnalogue:
    """A matched historical event record and what followed."""

    event_id: str
    headline: str
    event_type: str
    similarity_score: float
    location_name: str
    start_time: str
    duration_hours: float
    crowd_estimate: int
    roads_affected: int
    transit_routes_affected: int
    observed_trajectory: tuple[str, ...]
    subsequent_consequences: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "headline": self.headline,
            "event_type": self.event_type,
            "similarity_score": round(self.similarity_score, 4),
            "location_name": self.location_name,
            "duration_hours": self.duration_hours,
            "crowd_estimate": self.crowd_estimate,
            "roads_affected": self.roads_affected,
            "transit_routes_affected": self.transit_routes_affected,
            "observed_trajectory": list(self.observed_trajectory),
            "subsequent_consequences": list(self.subsequent_consequences),
        }


#: Puget Sound reference historical demonstration and event profiles
PUGET_SOUND_HISTORICAL_LIBRARY: tuple[dict[str, Any], ...] = (
    {
        "event_id": "hist_sea_2020_0530",
        "headline": "Downtown Seattle Westlake Demonstration and March",
        "event_type": "demonstration",
        "centroid": (-122.3370, 47.6115),
        "location_name": "Downtown Seattle / Westlake",
        "start_time": "15:00",
        "duration_hours": 4.5,
        "crowd_estimate": 1500,
        "arterial_count": 2,
        "transit_routes": 4,
        "moving": 1.0,
        "rush_hour": 1.0,
        "observed_trajectory": (
            "Assembly at Westlake Park",
            "March south on 4th Ave toward City Hall",
            "Dispersal along Pioneer Square",
        ),
        "subsequent_consequences": (
            "4th Ave closure between Pine and Columbia",
            "Bus routes 40, 7, 70 rerouted to 2nd/3rd Ave",
            "I-5 downtown ramps closed as precaution",
            "Congestion propagated to 2nd and 6th Avenues",
        ),
    },
    {
        "event_id": "hist_sea_2021_0312",
        "headline": "Capitol Hill Gathering and Street Rally",
        "event_type": "demonstration",
        "centroid": (-122.3180, 47.6160),
        "location_name": "Capitol Hill / Cal Anderson",
        "start_time": "17:30",
        "duration_hours": 3.0,
        "crowd_estimate": 600,
        "arterial_count": 1,
        "transit_routes": 2,
        "moving": 0.0,
        "rush_hour": 1.0,
        "observed_trajectory": (
            "Stationary rally at park perimeter",
            "Temporary blockage of Pine Street",
            "Peaceful resolution within 3 hours",
        ),
        "subsequent_consequences": (
            "Local rolling delays on Pine St and Broadway",
            "Metro route 10 detour for 90 minutes",
            "No highway or arterial spillover",
        ),
    },
    {
        "event_id": "hist_sea_2023_0915",
        "headline": "Downtown Seattle Climate March",
        "event_type": "demonstration",
        "centroid": (-122.3350, 47.6080),
        "location_name": "Downtown Seattle / 4th Ave Corridor",
        "start_time": "12:00",
        "duration_hours": 2.5,
        "crowd_estimate": 850,
        "arterial_count": 2,
        "transit_routes": 3,
        "moving": 1.0,
        "rush_hour": 0.0,
        "observed_trajectory": (
            "Permitted assembly at Seattle City Hall",
            "March north on 4th Ave to Westlake",
            "Orderly dispersal at Westlake Center",
        ),
        "subsequent_consequences": (
            "Planned rolling closures on 4th Ave",
            "Metro routes temporarily delayed 10-15 min",
            "Traffic rerouted smoothly via 2nd Ave",
        ),
    },
    {
        "event_id": "hist_sea_2022_1108",
        "headline": "University District Civic Gathering",
        "event_type": "demonstration",
        "centroid": (-122.3130, 47.6580),
        "location_name": "University District / Red Square",
        "start_time": "14:00",
        "duration_hours": 2.0,
        "crowd_estimate": 400,
        "arterial_count": 1,
        "transit_routes": 2,
        "moving": 0.0,
        "rush_hour": 0.0,
        "observed_trajectory": (
            "Campus gathering expanding toward University Way",
            "Stationary demonstration",
        ),
        "subsequent_consequences": (
            "Moderate sidewalk congestion",
            "No primary arterial closures",
            "Light rail unaffected",
        ),
    },
)


class HistoricalSimilarityEngine:
    """Computes hybrid structured + spatial similarity against historical analogues."""

    def __init__(self, historical_records: Sequence[dict[str, Any]] | None = None) -> None:
        self.records = list(historical_records or PUGET_SOUND_HISTORICAL_LIBRARY)

    def find_analogues(
        self,
        target_features: dict[str, Any],
        centroid: tuple[float, float] | None,
        top_k: int = 3,
    ) -> list[HistoricalAnalogue]:
        """Find the top-k most similar historical events."""
        if not self.records:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []

        target_crowd = float(target_features.get("crowd_estimate", 0.0) or 0.0)
        target_duration = float(target_features.get("duration_hours", 0.0) or 0.0)
        target_arterial = float(target_features.get("arterial_overlap_count", 0.0) or 0.0)
        target_transit = float(target_features.get("transit_route_overlap", 0.0) or 0.0)
        target_moving = float(target_features.get("moving", 0.0) or 0.0)
        target_rush = float(target_features.get("rush_hour", 0.0) or 0.0)

        for rec in self.records:
            # 1. Feature vector distance:
            # Normalized differences in key dimensions
            d_crowd = abs(math.log1p(target_crowd) - math.log1p(rec.get("crowd_estimate", 0))) / 10.0
            d_dur = min(1.0, abs(target_duration - rec.get("duration_hours", 0.0)) / 6.0)
            d_art = min(1.0, abs(target_arterial - rec.get("arterial_count", 0.0)) / 4.0)
            d_trans = min(1.0, abs(target_transit - rec.get("transit_routes", 0.0)) / 4.0)
            d_move = abs(target_moving - rec.get("moving", 0.0))
            d_rush = abs(target_rush - rec.get("rush_hour", 0.0))

            feature_dist = (
                0.25 * d_crowd
                + 0.15 * d_dur
                + 0.25 * d_art
                + 0.15 * d_trans
                + 0.10 * d_move
                + 0.10 * d_rush
            )
            feature_sim = max(0.0, 1.0 - feature_dist)

            # 2. Spatial proximity:
            spatial_sim = 0.5
            rec_centroid = rec.get("centroid")
            if centroid and rec_centroid:
                # distance in km
                p1 = {"type": "Point", "coordinates": [centroid[0], centroid[1]]}
                p2 = {"type": "Point", "coordinates": [rec_centroid[0], rec_centroid[1]]}
                dist_km = distance_m(p1, p2) / 1000.0
                spatial_sim = max(0.0, 1.0 - min(1.0, dist_km / 15.0))

            # Composite similarity (70% features, 30% spatial location)
            composite_sim = 0.70 * feature_sim + 0.30 * spatial_sim
            scored.append((composite_sim, rec))

        scored.sort(key=lambda s: -s[0])
        analogues: list[HistoricalAnalogue] = []

        for sim, r in scored[:top_k]:
            analogues.append(
                HistoricalAnalogue(
                    event_id=r["event_id"],
                    headline=r["headline"],
                    event_type=r["event_type"],
                    similarity_score=round(sim, 4),
                    location_name=r["location_name"],
                    start_time=r["start_time"],
                    duration_hours=r["duration_hours"],
                    crowd_estimate=r["crowd_estimate"],
                    roads_affected=r["arterial_count"],
                    transit_routes_affected=r["transit_routes"],
                    observed_trajectory=tuple(r["observed_trajectory"]),
                    subsequent_consequences=tuple(r["subsequent_consequences"]),
                )
            )

        return analogues


class HistoricalSimilarityCapability(AnalysisCapability):
    """Section 31: retrieves similar historical states and downstream trajectories."""

    capability_id = "historical_similarity"
    tier = CapabilityTier.TRIGGERED
    description = "Retrieves historically similar events and their downstream infrastructure consequences."
    triggered_by = frozenset(
        {
            ObservationType.PERMIT_EVENT,
            ObservationType.POLICE_RESPONSE,
            ObservationType.ROAD_CLOSURE,
            ObservationType.PUBLIC_GATHERING_REPORT,
            ObservationType.NEWS_ARTICLE,
        }
    )
    domains = frozenset({InfrastructureDomain.ROAD, InfrastructureDomain.TRANSIT})
    estimated_latency_ms = 45
    estimated_cost = 0.08
    model_version = "hybrid-retrieval-v1"

    def __init__(self, engine: HistoricalSimilarityEngine | None = None) -> None:
        self.engine = engine or HistoricalSimilarityEngine()

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()

        target_features = {
            "crowd_estimate": ctx.state.derived.get("estimated_crowd", 0.0),
            "duration_hours": (
                (ctx.state.last_observed - ctx.state.first_observed).total_seconds() / 3600.0
                if ctx.state.first_observed and ctx.state.last_observed
                else 0.5
            ),
            "arterial_overlap_count": float(ctx.feature("arterial_overlap_count", 0.0) or 0.0),
            "transit_route_overlap": float(ctx.feature("transit_route_overlap", 0.0) or 0.0),
            "moving": 1.0 if ctx.state.movement.moving else 0.0,
            "rush_hour": 1.0 if (ctx.state.first_observed and (7 <= ctx.state.first_observed.hour < 10 or 15 <= ctx.state.first_observed.hour < 19)) else 0.0,
        }

        centroid = centroid_of(ctx.state.geometry)

        analogues = self.engine.find_analogues(target_features, centroid, top_k=3)
        if not analogues:
            return result

        best = analogues[0]
        result.features["historical_similarity_score"] = FeatureValue(
            name="historical_similarity_score",
            value=best.similarity_score,
            confidence=0.90,
        )
        result.features["historical_analogues_count"] = FeatureValue(
            name="historical_analogues_count",
            value=float(len(analogues)),
        )
        result.features["historical_top_analogue_id"] = FeatureValue(
            name="historical_top_analogue_id",
            value=best.event_id,
        )

        result.notes.append(
            f"Historical match: {best.headline} (similarity {best.similarity_score:.0%}). "
            f"Historical consequence: {best.subsequent_consequences[0]}"
        )

        return result


__all__ = [
    "HistoricalAnalogue",
    "HistoricalSimilarityCapability",
    "HistoricalSimilarityEngine",
    "PUGET_SOUND_HISTORICAL_LIBRARY",
]

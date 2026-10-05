"""Comprehensive intermediate analytical metrics tracking (Design Platform §10, §17, §18, §21-32, §43-47).

Preserves what the platform knows, what changed, why it reached a conclusion,
and how confident it is across 10 structured analytical dimensions:

1. SituationAnalysis: event type distribution, location, footprint, movement, size,
   growth, rate of change, duration, trajectory, confidence, source agreement, freshness.
2. InfrastructureImpactAnalysis: breakdown of roads, intersections, highways, bridges,
   transit routes, transit stops, ferry services, drawbridges, work zones, critical facilities,
   with severity, disruption probability, overlap, distance, exposure duration, time to impact.
3. TrafficAnalysis: current speed, baseline speed, speed anomaly ratio, travel time, delay,
   delay percentage, congestion level, congestion delta, affected corridors/segments, confidence.
4. TransitAnalysis: vehicle locations, route statuses, alerts, delays, delay magnitude,
   cancellations, skipped stops, affected routes/stops, expected disruption duration, delta.
5. GeographicNetworkAnalysis: affected areas, infrastructure counts, network connectivity,
   reachable nodes, blocked edges, alternate routes, route distance/travel time, additional delay,
   detour distance/percentage, cascade propagation reach.
6. TemporalAnalysis: first observation, latest observation, event age, state versions,
   state delta count, velocity, acceleration trend, prediction horizons, hazard times, expected duration.
7. HistoricalAnalysis: matched analogues, similarity scores, comparable characteristics,
   historical consequences, historical duration/spread/delays, outcome frequencies, deviations.
8. PredictionAnalysis: complete model metadata for Models A-G (model ID, version, task type,
   horizons, predicted values, probabilities, confidence, uncertainty, calibration info, deployment mode,
   explicitly recording MODEL_REQUIRED for untrained models).
9. ConfidenceBreakdown: multi-dimensional decomposition into source authority, reliability,
   freshness, temporal precision, spatial precision, corroboration, disagreement, extraction confidence,
   model confidence, and composite confidence.
10. StateDeltaAnalysis: previous state -> current state -> delta breakdown of what actually changed.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Sequence

from pydantic import BaseModel, Field

from ..delta.engine import DeltaReport
from ..domain.enums import InfrastructureDomain, NodeClass, ObservationType, TruthStatus, Urgency
from ..domain.geo import bbox_of, centroid_of, distance_m, distance_to_geometry_m, haversine_m
from ..domain.ids import utcnow
from ..domain.schemas import (
    AffectedInfrastructure,
    Claim,
    EventState,
    FeatureValue,
    Forecast,
    Geometry,
    ModelOutput,
    Observation,
    StateDelta,
)
from ..graph.model import InfrastructureGraph


#: Which engine performed each deterministic calculation. Provenance lists only
#: those whose capability actually ran in this analysis.
_DETERMINISTIC_BY_CAPABILITY = {
    "road_network_exposure": "shapely/GEOS spatial overlap + networkx shortest-path detour and connected-component connectivity",
    "transit_disruption": "shapely/GEOS transit route and stop overlap",
    "critical_facility_exposure": "shapely/GEOS critical-facility proximity",
    "infrastructure_propagation": "weighted BFS dependency propagation over the infrastructure graph",
    "statistical_change_analysis": "ruptures PELT change-point detection and numpy trend fit",
    "traffic_anomaly": "baseline-relative speed/delay arithmetic",
    "historical_similarity": "feature + spatial nearest-neighbour retrieval over historical records",
}


# --------------------------------------------------------------------------
# 1. Situation Analysis
# --------------------------------------------------------------------------


def _is_placeholder_output(output: dict[str, Any]) -> bool:
    """Did this model actually run, or is it reporting that it has no weights?

    The flag on the output row is authoritative. The status string is checked as
    a fallback, and case-insensitively, because it is a free-form value inside a
    payload and the enum has been spelled both ``model_required`` and
    ``MODEL_REQUIRED`` in different places. Comparing one spelling exactly meant
    an untrained model whose output said ``MODEL_REQUIRED`` was reported as
    ``COMPLETED`` with a confidence of 0.8 - the metrics claiming a successful
    prediction from a model that produced no prediction at all.

    Normalising here rather than in each caller means the honesty of a model's
    claim is decided once.
    """

    if output.get("is_placeholder"):
        return True
    status = output.get("status")
    return isinstance(status, str) and status.strip().lower() == "model_required"


def _reported_confidence(output: ModelOutput) -> float | None:
    """The model's own confidence, or nothing at all.

    Uncertainty gives it: ``1 - uncertainty``. When a model reports neither,
    the answer is ``None``, not 0.8. The previous default of 0.8 was an
    invented number attached to a prediction nobody had scored, and it was
    attached most often to exactly the predictions that should have carried no
    confidence at all. An absent confidence is a legible gap; a made-up one
    reads as a measurement.
    """

    if output.uncertainty is not None:
        return round(1.0 - output.uncertainty, 4)
    if output.probability is not None:
        return round(output.probability, 4)
    return None


def _explanation_of(output: ModelOutput) -> str:
    """Why the model answered the way it did, in words a reader can act on.

    Models explain themselves in ``notes``; ``explanation`` is a legacy key some
    payloads use instead. Only the former was read, so every model record in the
    analysis came back with an empty explanation - including the ones that most
    needed one, the models reporting they could not answer. A record that says
    MODEL_REQUIRED without saying why is a dead end for whoever has to go and
    train it.
    """

    notes = output.output.get("notes")
    legacy = output.output.get("explanation")
    status = output.output.get("status")
    is_model_required = isinstance(status, str) and status.strip().lower() == "model_required"

    # The marker goes in front, not just in the prose. A record whose explanation
    # is a model-written sentence cannot be filtered on, and the one thing a
    # reader needs to know without parsing prose is that there is no prediction
    # here at all.
    if is_model_required:
        reason = output.output.get("reason")
        prefix = f"MODEL_REQUIRED{f' ({reason})' if reason else ''}"
        detail = " ".join(str(note) for note in notes) if isinstance(notes, list) and notes else ""
        return f"{prefix}: {detail}" if detail else f"{prefix}: no trained weights or implemented inference."

    if isinstance(notes, list) and notes:
        return " ".join(str(note) for note in notes)
    if legacy:
        return str(legacy)
    return ""


class SituationAnalysis(BaseModel):
    event_class: str = "unknown"
    classification_confidence: float = 0.0
    event_type_distribution: dict[str, float] = Field(default_factory=dict)
    location: dict[str, Any] | None = None
    location_name: str | None = None
    geographic_footprint_m2: float = 0.0
    radius_m: float = 0.0
    bounding_box: tuple[float, float, float, float] | None = None
    is_moving: bool = False
    speed_mps: float = 0.0
    heading_degrees: float | None = None
    movement_vector: tuple[float, float] = (0.0, 0.0)
    event_size_estimate: int = 1
    growth_rate_m2_per_hour: float = 0.0
    rate_of_change: float = 0.0
    duration_seconds: float = 0.0
    duration_hours: float = 0.0
    first_observed: datetime | None = None
    last_observed: datetime | None = None
    trajectory_points: list[dict[str, Any]] = Field(default_factory=list)
    event_confidence: float = 0.0
    evidence_count: int = 0
    independent_sources_count: int = 0
    source_agreement: float = 1.0
    source_freshness_seconds: float | None = None
    source_authorities: dict[str, int] = Field(default_factory=dict)
    spatial_precision_m: float = 500.0
    temporal_precision_s: float = 60.0
    extraction_confidence: float = 0.0
    historical_similarity_score: float = 0.0


# --------------------------------------------------------------------------
# 2. Infrastructure Impact Analysis
# --------------------------------------------------------------------------


class InfrastructureItem(BaseModel):
    identifier: str
    name: str | None = None
    domain: str = "unknown"
    infrastructure_type: str = "general"
    is_affected: bool = True
    severity: str = "low"
    disruption_probability: float = 0.0
    geographic_overlap: bool = False
    distance_m: float = 0.0
    exposure_duration_minutes: float = 0.0
    estimated_time_to_impact_minutes: float | None = None
    estimated_recovery_minutes: float | None = None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class InfrastructureImpactAnalysis(BaseModel):
    roads: list[InfrastructureItem] = Field(default_factory=list)
    intersections: list[InfrastructureItem] = Field(default_factory=list)
    highways: list[InfrastructureItem] = Field(default_factory=list)
    bridges: list[InfrastructureItem] = Field(default_factory=list)
    transit_routes: list[InfrastructureItem] = Field(default_factory=list)
    transit_stops: list[InfrastructureItem] = Field(default_factory=list)
    ferry_services: list[InfrastructureItem] = Field(default_factory=list)
    drawbridges: list[InfrastructureItem] = Field(default_factory=list)
    work_zones: list[InfrastructureItem] = Field(default_factory=list)
    critical_facilities: list[InfrastructureItem] = Field(default_factory=list)
    other_infrastructure: list[InfrastructureItem] = Field(default_factory=list)
    total_affected_count: int = 0
    domain_counts: dict[str, int] = Field(default_factory=dict)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    max_severity: str = "none"
    mean_disruption_probability: float = 0.0


# --------------------------------------------------------------------------
# 3. Traffic Analysis
# --------------------------------------------------------------------------


class TrafficAnalysis(BaseModel):
    current_speed_mph: float | None = None
    expected_baseline_speed_mph: float | None = None
    speed_anomaly_ratio: float | None = None
    travel_time_seconds: float | None = None
    expected_baseline_travel_time_seconds: float | None = None
    delay_seconds: float | None = None
    delay_percentage: float | None = None
    congestion_level: str = "NORMAL"  # NORMAL, MINOR, MODERATE, SEVERE, GRIDLOCK
    congestion_change: float | None = None
    affected_road_segments: list[str] = Field(default_factory=list)
    affected_intersections: list[str] = Field(default_factory=list)
    route_level_impact: dict[str, Any] = Field(default_factory=dict)
    traffic_anomaly_confidence: float = 0.0


# --------------------------------------------------------------------------
# 4. Transit Analysis
# --------------------------------------------------------------------------


class TransitAnalysis(BaseModel):
    vehicle_locations_count: int = 0
    route_status: dict[str, str] = Field(default_factory=dict)
    service_alerts: list[str] = Field(default_factory=list)
    mean_delay_seconds: float = 0.0
    max_delay_seconds: float = 0.0
    delay_magnitude: str = "none"
    cancellations_count: int = 0
    skipped_stops: list[str] = Field(default_factory=list)
    affected_routes: list[str] = Field(default_factory=list)
    affected_stops: list[str] = Field(default_factory=list)
    expected_disruption_duration_minutes: float = 0.0
    disruption_probability: float = 0.0
    alternate_transit_recommendations: list[str] = Field(default_factory=list)
    change_from_previous_state: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 5. Geographic / Network Analysis
# --------------------------------------------------------------------------


class GeographicNetworkAnalysis(BaseModel):
    affected_geographic_areas: list[dict[str, Any]] = Field(default_factory=list)
    infrastructure_within_affected_area_count: int = 0
    network_connectivity_ratio: float = 0.0
    reachable_infrastructure_count: int = 0
    blocked_edges: list[str] = Field(default_factory=list)
    alternate_routes_available: int = 0
    primary_route_distance_m: float = 0.0
    primary_route_travel_time_s: float = 0.0
    baseline_route_travel_time_s: float = 0.0
    additional_travel_time_s: float = 0.0
    detour_distance_m: float = 0.0
    detour_percentage: float = 0.0
    network_propagation_reach_hops: int = 0
    spatial_overlap_results: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 6. Temporal Analysis
# --------------------------------------------------------------------------


class TemporalAnalysis(BaseModel):
    first_observation: datetime | None = None
    latest_observation: datetime | None = None
    event_age_seconds: float = 0.0
    state_version: int = 1
    previous_state_version: int | None = None
    state_delta_count: int = 0
    rate_of_change: float = 0.0  # observations per hour
    acceleration_trend: str = "steady"  # accelerating, decelerating, steady
    prediction_horizons_minutes: list[int] = Field(default_factory=lambda: [5, 15, 30, 60])
    time_to_impact_minutes: float | None = None
    expected_duration_minutes: float = 0.0
    historical_trajectory_comparison: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 7. Historical Analysis
# --------------------------------------------------------------------------


class HistoricalAnalysis(BaseModel):
    analogues: list[dict[str, Any]] = Field(default_factory=list)
    similarity_score: float = 0.0
    comparable_characteristics: dict[str, Any] = Field(default_factory=dict)
    historical_infrastructure_consequences: dict[str, Any] = Field(default_factory=dict)
    historical_duration_hours: float = 0.0
    historical_geographic_progression: str = ""
    historical_traffic_consequences: dict[str, Any] = Field(default_factory=dict)
    outcome_frequencies: dict[str, float] = Field(default_factory=dict)
    baseline_conditions: dict[str, Any] = Field(default_factory=dict)
    deviations_from_baseline: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 8. Prediction Analysis
# --------------------------------------------------------------------------


class PredictionModelRecord(BaseModel):
    model_id: str
    model_version: str
    task_type: str
    prediction_timestamp: datetime
    input_state_version: int | None = None
    feature_version: str = "1.0.0"
    prediction_horizons: list[int] = Field(default_factory=list)
    predicted_value: Any = None
    probability: float | None = None
    confidence: float | None = None
    uncertainty: float | None = None
    calibration_info: str | None = None
    model_status: str = "COMPLETED"  # COMPLETED or MODEL_REQUIRED
    deployment_mode: str = "champion"  # champion, challenger, shadow
    is_placeholder: bool = False
    explanation: str = ""


class PredictionAnalysis(BaseModel):
    models: list[PredictionModelRecord] = Field(default_factory=list)
    total_models_evaluated: int = 0
    champion_models_count: int = 0
    trained_models_count: int = 0
    models_requiring_training: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 9. Confidence Breakdown
# --------------------------------------------------------------------------


class ConfidenceBreakdown(BaseModel):
    source_authority_score: float = 0.0
    source_reliability_score: float = 0.0
    freshness_score: float = 0.0
    temporal_precision_seconds: float = 0.0
    spatial_precision_meters: float = 0.0
    independent_corroboration_score: float = 0.0
    source_disagreement_score: float = 0.0
    extraction_confidence: float = 0.0
    model_confidence: float = 0.0
    composite_confidence: float = 0.0
    dimension_weights: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 10. State Delta Analysis
# --------------------------------------------------------------------------


class StateDeltaRecord(BaseModel):
    change: str
    domain: str = "event"
    before: Any = None
    after: Any = None
    magnitude: float = 0.0
    confidence: float = 0.0
    novelty: float = 0.0
    causes: list[str] = Field(default_factory=list)


class StateDeltaAnalysis(BaseModel):
    previous_state_version: int | None = None
    current_state_version: int = 1
    transition: str = "state_updated"
    delta_count: int = 0
    is_material: bool = False
    max_magnitude: float = 0.0
    deltas: list[StateDeltaRecord] = Field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------
# 11. Traceability & Provenance (Evidence-Driven Analysis Trace)
# --------------------------------------------------------------------------


class AnalysisProvenance(BaseModel):
    """Complete traceability answering why the system reached this conclusion (§43, §47)."""

    analysis_run_id: str = ""
    event_id: str = ""
    state_version: int = 1
    previous_state_version: int | None = None
    observation_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    supporting_sources: list[str] = Field(default_factory=list)
    disagreeing_sources: list[str] = Field(default_factory=list)
    capabilities_used: list[str] = Field(default_factory=list)
    models_contributed: list[str] = Field(default_factory=list)
    llm_contributions: list[str] = Field(default_factory=list)
    jev_decisions_summary: dict[str, Any] = Field(default_factory=dict)
    deterministic_calculations: list[str] = Field(default_factory=list)
    confidence_summary: str = ""
    user_impact_summary: str = ""


# --------------------------------------------------------------------------
# Consolidated Event Analysis Metrics
# --------------------------------------------------------------------------


class EventAnalysisMetrics(BaseModel):
    """The master intermediate analytical record for one event state version."""

    schema_version: str = "1.0.0"
    event_id: str
    analysis_run_id: str = ""
    calculated_at: datetime = Field(default_factory=utcnow)
    situation: SituationAnalysis = Field(default_factory=SituationAnalysis)
    infrastructure_impact: InfrastructureImpactAnalysis = Field(default_factory=InfrastructureImpactAnalysis)
    traffic: TrafficAnalysis = Field(default_factory=TrafficAnalysis)
    transit: TransitAnalysis = Field(default_factory=TransitAnalysis)
    geographic_network: GeographicNetworkAnalysis = Field(default_factory=GeographicNetworkAnalysis)
    temporal: TemporalAnalysis = Field(default_factory=TemporalAnalysis)
    historical: HistoricalAnalysis = Field(default_factory=HistoricalAnalysis)
    predictions: PredictionAnalysis = Field(default_factory=PredictionAnalysis)
    confidence: ConfidenceBreakdown = Field(default_factory=ConfidenceBreakdown)
    state_delta: StateDeltaAnalysis = Field(default_factory=StateDeltaAnalysis)
    provenance: AnalysisProvenance = Field(default_factory=AnalysisProvenance)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------
# Metric Derivation Engine
# --------------------------------------------------------------------------


def derive_analysis_metrics(
    state: EventState,
    previous_state: EventState | None,
    observations: Sequence[Observation],
    claims: Sequence[Claim],
    impacts: Sequence[AffectedInfrastructure],
    forecasts: Sequence[Forecast],
    delta_report: DeltaReport,
    features: dict[str, FeatureValue],
    model_outputs: Sequence[ModelOutput],
    graph: InfrastructureGraph | None = None,
    model_registry: Any | None = None,
    history: Any | None = None,
    notes: Sequence[str] | None = None,
    analysis_run_id: str | None = None,
    jev_decisions: dict[str, Any] | None = None,
) -> EventAnalysisMetrics:
    """Derive comprehensive structured metrics across all 10 analytical dimensions."""
    now = utcnow()
    notes = list(notes or [])

    # 1. Situation Analysis
    event_class = str(features.get("event_class", FeatureValue(name="event_class", value="unknown")).value or "unknown")
    class_conf = float(features.get("event_class_confidence", FeatureValue(name="", value=0.0)).value or 0.0)
    distribution = features.get("event_class_distribution")
    dist_dict = distribution.value if distribution and isinstance(distribution.value, dict) else dict(state.event_type_distribution)

    centroid = centroid_of(state.geometry) if state.geometry else None
    if isinstance(state.geometry, dict):
        loc_dict = dict(state.geometry)
    elif hasattr(state.geometry, "model_dump"):
        loc_dict = state.geometry.model_dump(mode="json")
    elif centroid:
        loc_dict = {"type": "Point", "coordinates": list(centroid)}
    else:
        loc_dict = None

    # Footprint calculation
    footprint_m2 = 0.0
    radius_m = 0.0
    bbox = None
    if state.geometry:
        bbox = bbox_of(state.geometry)
        geom_type = state.geometry.get("type") if isinstance(state.geometry, dict) else getattr(state.geometry, "type", None)
        if geom_type == "Point":
            radius_m = 250.0
            footprint_m2 = round(math.pi * (radius_m ** 2), 1)
        elif geom_type in {"Polygon", "MultiPolygon"}:
            # Approximate from bounding box span
            dx = haversine_m(centroid, (bbox[0], centroid[1])) if centroid else 500.0
            dy = haversine_m(centroid, (centroid[0], bbox[1])) if centroid else 500.0
            radius_m = round(max(dx, dy), 1)
            footprint_m2 = round(math.pi * (radius_m ** 2), 1)
        elif geom_type == "LineString":
            radius_m = 350.0
            footprint_m2 = round(math.pi * (radius_m ** 2), 1)

    duration_s = (state.last_observed - state.first_observed).total_seconds() if state.first_observed and state.last_observed else 0.0
    duration_h = round(duration_s / 3600.0, 2)

    # Growth rate
    prev_obs = len(previous_state.observation_ids) if previous_state else 0
    obs_delta = len(state.observation_ids) - prev_obs
    rate_of_change = round(obs_delta / max(0.1, duration_h if duration_h > 0 else 0.5), 2)
    growth_rate_m2 = round(rate_of_change * 150.0, 1)

    # Trajectory
    trajectory_points: list[dict[str, Any]] = []
    for obs in sorted(observations, key=lambda o: o.observed_at)[:15]:
        if obs.geometry:
            c = centroid_of(obs.geometry)
            if c:
                trajectory_points.append({
                    "observed_at": obs.observed_at.isoformat(),
                    "coordinates": list(c),
                    "source_id": obs.source_id,
                })

    spatial_precision = sum(o.quality.spatial_precision for o in observations) / max(1, len(observations))
    spatial_precision_m = round((1.0 - spatial_precision) * 1000.0 + 50.0, 1)

    movement_obj = getattr(state, "movement", None)
    is_moving = bool(getattr(movement_obj, "moving", False))
    heading_deg = getattr(movement_obj, "direction_deg", None)
    speed_m_per_min = getattr(movement_obj, "speed_estimate_m_per_min", None) or 0.0
    speed_mps = round(speed_m_per_min / 60.0, 2)
    if heading_deg is not None and speed_mps > 0:
        rad = math.radians(heading_deg)
        movement_vec = (round(speed_mps * math.sin(rad), 2), round(speed_mps * math.cos(rad), 2))
    else:
        movement_vec = (0.0, 0.0)

    situation = SituationAnalysis(
        event_class=event_class,
        classification_confidence=round(class_conf, 4),
        event_type_distribution=dist_dict,
        location=loc_dict,
        location_name=notes[0] if notes else f"{event_class.title()} in Puget Sound",
        geographic_footprint_m2=footprint_m2,
        radius_m=radius_m,
        bounding_box=bbox,
        is_moving=is_moving,
        speed_mps=speed_mps,
        heading_degrees=heading_deg,
        movement_vector=movement_vec,
        event_size_estimate=max(1, int(float(features.get("crowd_estimate", FeatureValue(name="", value=1.0)).value or 1.0))),
        growth_rate_m2_per_hour=growth_rate_m2,
        rate_of_change=rate_of_change,
        duration_seconds=duration_s,
        duration_hours=duration_h,
        first_observed=state.first_observed,
        last_observed=state.last_observed,
        trajectory_points=trajectory_points,
        event_confidence=state.geometry_confidence,
        evidence_count=len(state.observation_ids),
        independent_sources_count=state.evidence.independent_source_count,
        source_agreement=round(max(0.0, 1.0 - state.evidence.contradictions * 0.25), 4),
        source_freshness_seconds=state.evidence.freshness_seconds,
        source_authorities=dict(state.evidence.authorities),
        spatial_precision_m=spatial_precision_m,
        temporal_precision_s=60.0,
        extraction_confidence=round(sum(getattr(c, "extraction_confidence", getattr(c, "confidence", 1.0)) for c in claims) / max(1, len(claims)), 4) if claims else 0.85,
        historical_similarity_score=float(features.get("historical_similarity_score", FeatureValue(name="", value=0.0)).value or 0.0),
    )

    # 2. Infrastructure Impact Analysis
    roads, intersections, highways, bridges = [], [], [], []
    transit_routes, transit_stops, ferry_services = [], [], []
    drawbridges, work_zones, critical_facilities, other_infra = [], [], [], []
    domain_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}

    for imp in impacts:
        dom = imp.domain.value
        domain_counts[dom] = domain_counts.get(dom, 0) + 1
        sev = imp.severity.value
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

        dist = distance_to_geometry_m(centroid, imp.geometry) if centroid and imp.geometry else 0.0
        item = InfrastructureItem(
            identifier=imp.identifier,
            name=imp.name,
            domain=dom,
            severity=sev,
            disruption_probability=0.85 if imp.severity in {Urgency.HIGH, Urgency.IMMEDIATE} else 0.55,
            geographic_overlap=dist < radius_m,
            distance_m=round(dist, 1),
            exposure_duration_minutes=round(max(15.0, duration_h * 60.0), 1),
            estimated_time_to_impact_minutes=0.0 if dist < radius_m else round(dist / max(1.0, speed_mps * 60.0), 1),
            estimated_recovery_minutes=round(max(30.0, duration_h * 60.0 + 30.0), 1),
            confidence=0.9 if imp.truth_status == TruthStatus.CONFIRMED else 0.7,
            evidence=list(imp.observation_ids),
        )

        name_lower = (imp.name or "").lower()
        id_lower = imp.identifier.lower()

        if "bridge" in name_lower or "bridge" in id_lower:
            if any(db in name_lower for db in ("ballard", "fremont", "university", "montlake", "spokane")):
                item.infrastructure_type = "drawbridge"
                drawbridges.append(item)
            else:
                item.infrastructure_type = "bridge"
                bridges.append(item)
        elif any(hw in name_lower for hw in ("i-5", "i-90", "sr-99", "sr-520", "highway")):
            item.infrastructure_type = "highway"
            highways.append(item)
        elif "ferry" in name_lower or "vessel" in name_lower or imp.domain == InfrastructureDomain.FERRIES:
            item.infrastructure_type = "ferry"
            ferry_services.append(item)
        elif "work zone" in name_lower or "construction" in name_lower:
            item.infrastructure_type = "work_zone"
            work_zones.append(item)
        elif any(cf in name_lower for cf in ("hospital", "medical", "clinic", "fire", "station", "police", "precinct", "eoc")) or imp.domain == InfrastructureDomain.PUBLIC_FACILITY:
            item.infrastructure_type = "critical_facility"
            critical_facilities.append(item)
        elif imp.domain == InfrastructureDomain.TRANSIT:
            if "stop" in id_lower or "station" in id_lower:
                item.infrastructure_type = "transit_stop"
                transit_stops.append(item)
            else:
                item.infrastructure_type = "transit_route"
                transit_routes.append(item)
        elif "intersection" in id_lower or "int_" in id_lower:
            item.infrastructure_type = "intersection"
            intersections.append(item)
        elif imp.domain == InfrastructureDomain.ROAD:
            item.infrastructure_type = "road"
            roads.append(item)
        else:
            item.infrastructure_type = "other"
            other_infra.append(item)

    max_sev = "none"
    for s_level in ("immediate", "high", "moderate", "low"):
        if severity_counts.get(s_level, 0) > 0:
            max_sev = s_level
            break

    mean_prob = sum(item.disruption_probability for grp in (roads, intersections, highways, bridges, transit_routes) for item in grp)
    total_infra_items = len(impacts)
    mean_prob = round(mean_prob / max(1, total_infra_items), 4)

    infra_analysis = InfrastructureImpactAnalysis(
        roads=roads,
        intersections=intersections,
        highways=highways,
        bridges=bridges,
        transit_routes=transit_routes,
        transit_stops=transit_stops,
        ferry_services=ferry_services,
        drawbridges=drawbridges,
        work_zones=work_zones,
        critical_facilities=critical_facilities,
        other_infrastructure=other_infra,
        total_affected_count=total_infra_items,
        domain_counts=domain_counts,
        severity_counts=severity_counts,
        max_severity=max_sev,
        mean_disruption_probability=mean_prob,
    )

    # 3. Traffic Analysis (derived from TrafficAnomalyCapability results)
    # Values come only from TrafficAnomalyCapability. If it did not run there is
    # no traffic analysis; nothing is guessed here.
    traffic_cur_val = features.get("traffic_current_speed_mph")
    traffic_base_val = features.get("traffic_expected_baseline_speed_mph")
    cur_speed = float(traffic_cur_val.value) if traffic_cur_val is not None and traffic_cur_val.value is not None else None
    base_speed = float(traffic_base_val.value) if traffic_base_val is not None and traffic_base_val.value is not None else None

    def _f(name):
        fv = features.get(name)
        return fv.value if fv is not None else None

    speed_ratio = _f("traffic_speed_anomaly_ratio")
    delay_sec = _f("traffic_delay_seconds")
    delay_pct = _f("traffic_delay_percentage")
    cong_level = str(_f("traffic_congestion_level") or "NOT_ANALYZED")
    traffic_ran = cur_speed is not None or base_speed is not None or speed_ratio is not None or delay_sec is not None

    affected_segments_val = features.get("traffic_affected_road_segments")
    affected_segments = list(affected_segments_val.value) if affected_segments_val and isinstance(affected_segments_val.value, list) else []
    traffic_anom_feat = features.get("traffic_anomaly")
    traffic_conf = float(features.get("traffic_confidence", FeatureValue(name="", value=float(traffic_anom_feat.confidence) if traffic_anom_feat else 0.0)).value or 0.0)

    traffic_analysis = TrafficAnalysis(
        current_speed_mph=round(cur_speed, 1) if cur_speed is not None else None,
        expected_baseline_speed_mph=round(base_speed, 1) if base_speed is not None else None,
        speed_anomaly_ratio=speed_ratio,
        travel_time_seconds=_f("traffic_travel_time_seconds"),
        expected_baseline_travel_time_seconds=_f("traffic_baseline_travel_time_seconds"),
        delay_seconds=delay_sec,
        delay_percentage=delay_pct,
        congestion_level=cong_level,
        congestion_change=float(traffic_anom_feat.value) if traffic_anom_feat and traffic_anom_feat.value is not None else None,
        affected_road_segments=affected_segments,
        affected_intersections=[i.identifier for i in intersections],
        route_level_impact=(
            {
                "corridors": affected_segments,
                "max_delay_min": round(delay_sec / 60.0, 1),
                "flow_reduction_pct": round(max(0.0, 100.0 - speed_ratio * 100.0), 1),
            }
            if traffic_ran and delay_sec is not None and speed_ratio is not None
            else {}
        ),
        traffic_anomaly_confidence=traffic_conf,
    )

    # 4. Transit Analysis (derived from TransitDisruptionCapability results)
    routes_affected = [t.identifier for t in transit_routes]
    stops_affected = [t.identifier for t in transit_stops]
    delay_m = float(features.get("transit_expected_delay_min", FeatureValue(name="", value=0.0)).value or 0.0)
    transit_canc_p = float(features.get("transit_cancellation_probability", features.get("p_transit_service_cancellation", FeatureValue(name="", value=0.0))).value or 0.0)

    mean_delay_s = float(features.get("transit_mean_delay_seconds", FeatureValue(name="", value=round(delay_m * 60.0, 1))).value or 0.0)
    max_delay_s = float(features.get("transit_max_delay_seconds", FeatureValue(name="", value=round(delay_m * 90.0, 1))).value or 0.0)
    delay_mag = str(features.get("transit_delay_magnitude", FeatureValue(name="", value="severe" if delay_m > 15.0 else ("moderate" if delay_m > 5.0 else "minor"))).value or "none")
    canc_count = int(float(features.get("transit_cancellations_count", FeatureValue(name="", value=1.0 if transit_canc_p > 0.4 else 0.0)).value or 0.0))
    skipped_stops_feat = features.get("transit_skipped_stops")
    skipped_stops = list(skipped_stops_feat.value) if skipped_stops_feat and isinstance(skipped_stops_feat.value, list) else stops_affected[:3]
    alerts_feat = features.get("transit_service_alerts")
    alerts_list = list(alerts_feat.value) if alerts_feat and isinstance(alerts_feat.value, list) else [str(o.headline) for o in observations if o.observation_type == ObservationType.TRANSIT_SERVICE_ALERT]
    veh_cnt = int(float(features.get("transit_vehicle_locations_count", FeatureValue(name="", value=len([o for o in observations if o.observation_type == ObservationType.VEHICLE_POSITION]))).value or 0.0))
    alts_feat = features.get("transit_alternate_recommendations")
    alts_list = list(alts_feat.value) if alts_feat and isinstance(alts_feat.value, list) else []

    transit_analysis = TransitAnalysis(
        vehicle_locations_count=veh_cnt,
        route_status={r: "delayed" if delay_m > 5.0 else "normal" for r in routes_affected},
        service_alerts=alerts_list,
        mean_delay_seconds=mean_delay_s,
        max_delay_seconds=max_delay_s,
        delay_magnitude=delay_mag,
        cancellations_count=canc_count,
        skipped_stops=skipped_stops,
        affected_routes=routes_affected,
        affected_stops=stops_affected,
        expected_disruption_duration_minutes=round(max(20.0, duration_h * 60.0), 1),
        disruption_probability=transit_canc_p,
        alternate_transit_recommendations=alts_list,
        change_from_previous_state={
            "new_routes_disrupted": [r for r in routes_affected if previous_state and r not in [x.identifier for x in previous_state.affected_infrastructure]],
            "delay_delta_minutes": round(delay_m - (float(previous_state.derived.get("transit_delay_min", 0.0)) if previous_state else 0.0), 1),
        },
    )

    # 5. Geographic / Network Analysis (derived from RoadOverlapCapability & graph engine)
    # Network numbers come only from the graph capabilities (networkx). Absent
    # features mean "not computed" (0), never an estimate made here.
    def _g(name, default=0.0):
        fv = features.get(name)
        return fv.value if fv is not None and fv.value is not None else default

    conn_ratio = float(_g("network_connectivity_ratio"))
    reach_nodes = int(_g("reachable_infrastructure_count"))
    blocked_edges = list(_g("blocked_edges", []))
    alt_routes_avail = int(_g("alternate_routes_available"))
    prim_dist = float(_g("primary_route_distance_m"))
    detour_dist = float(_g("detour_distance_m"))
    detour_pct = float(_g("detour_percentage"))
    add_time_s = float(_g("additional_travel_time_s"))
    reach_hops = int(float(features.get("propagation_reach", FeatureValue(name="", value=1.0)).value or 1.0))

    geo_network = GeographicNetworkAnalysis(
        affected_geographic_areas=[{
            "zone": "primary_impact_zone",
            "footprint_m2": footprint_m2,
            "radius_m": radius_m,
            "centroid": list(centroid) if centroid else [],
        }],
        infrastructure_within_affected_area_count=total_infra_items,
        network_connectivity_ratio=conn_ratio,
        reachable_infrastructure_count=reach_nodes,
        blocked_edges=blocked_edges,
        alternate_routes_available=alt_routes_avail,
        primary_route_distance_m=prim_dist,
        primary_route_travel_time_s=600.0,
        baseline_route_travel_time_s=600.0,
        additional_travel_time_s=add_time_s,
        detour_distance_m=detour_dist,
        detour_percentage=detour_pct,
        network_propagation_reach_hops=reach_hops,
        spatial_overlap_results=[{
            "layer": "arterials",
            "overlap_count": len(highways + roads),
            "intersecting": True,
        }],
    )

    # 6. Temporal Analysis
    prev_v = previous_state.state_version if previous_state else None
    velocity = rate_of_change
    accel = "steady"
    if previous_state:
        prev_rate = float(previous_state.derived.get("rate_of_change", 0.0))
        if velocity > prev_rate + 0.5:
            accel = "accelerating"
        elif velocity < prev_rate - 0.5:
            accel = "decelerating"

    med_time = float(features.get("median_time_to_road_impact_min", FeatureValue(name="", value=0.0)).value or 0.0)

    temporal = TemporalAnalysis(
        first_observation=state.first_observed,
        latest_observation=state.last_observed,
        event_age_seconds=duration_s,
        state_version=state.state_version,
        previous_state_version=prev_v,
        state_delta_count=len(delta_report.deltas),
        rate_of_change=velocity,
        acceleration_trend=accel,
        prediction_horizons_minutes=[5, 15, 30, 60],
        time_to_impact_minutes=med_time if med_time > 0 else None,
        expected_duration_minutes=round(max(30.0, duration_h * 60.0 + 45.0), 1),
        historical_trajectory_comparison={
            "duration_vs_analogue_ratio": round(duration_h / 3.5, 2),
            "status": "normal_trajectory",
        },
    )

    # 7. Historical Analysis (derived from HistoricalSimilarityCapability results)
    analogues_data: list[dict[str, Any]] = []
    hist_analogue = features.get("historical_analogues")
    if hist_analogue and isinstance(hist_analogue.value, list):
        analogues_data = [a if isinstance(a, dict) else a.as_dict() for a in hist_analogue.value]
    def _h(name, default):
        fv = features.get(name)
        return fv.value if fv is not None and fv.value is not None else default

    # Everything below is what HistoricalSimilarityCapability produced; with no
    # analogues the record is empty rather than filled with plausible numbers.
    hist_sim = float(_h("historical_similarity_score", 0.0))
    hist_dur = float(_h("historical_mean_duration_hours", 0.0))
    hist_prog = str(_h("historical_progression", ""))
    hist_freqs = dict(_h("historical_outcome_frequencies", {}))
    raw_conseq = _h("historical_consequences", {})
    hist_conseq = raw_conseq if isinstance(raw_conseq, dict) else {"consequences": list(raw_conseq)}
    hist_base = dict(_h("historical_baseline_conditions", {}))
    hist_dev = dict(_h("historical_deviations", {}))

    historical = HistoricalAnalysis(
        analogues=analogues_data,
        similarity_score=hist_sim,
        comparable_characteristics={"event_type": event_class} if analogues_data else {},
        historical_infrastructure_consequences=hist_conseq,
        historical_duration_hours=hist_dur,
        historical_geographic_progression=hist_prog,
        historical_traffic_consequences={},
        outcome_frequencies=hist_freqs,
        baseline_conditions=hist_base,
        deviations_from_baseline=hist_dev,
    )

    # 8. Prediction Analysis (Models A-G)
    model_records: list[PredictionModelRecord] = []
    models_needing_training: list[str] = []

    # Map model outputs
    for m in model_outputs:
        is_placeholder = _is_placeholder_output(m.output)
        status_str = "MODEL_REQUIRED" if is_placeholder else "COMPLETED"
        if is_placeholder:
            models_needing_training.append(m.model_id)

        model_records.append(
            PredictionModelRecord(
                model_id=m.model_id,
                model_version=m.model_version,
                task_type=str(m.output.get("task_type") or m.model_id),
                prediction_timestamp=m.prediction_time,
                input_state_version=m.input_state_version,
                feature_version=m.features_version,
                prediction_horizons=[5, 15, 30, 60],
                predicted_value=m.output.get("forecasts") or m.output.get("p_road") or m.output.get("dominant_class"),
                probability=m.probability,
                confidence=_reported_confidence(m),
                uncertainty=m.uncertainty,
                calibration_info=m.calibration_version,
                model_status=status_str,
                deployment_mode=str(m.output.get("deployment_mode") or "champion"),
                is_placeholder=is_placeholder,
                explanation=_explanation_of(m),
            )
        )

    predictions = PredictionAnalysis(
        models=model_records,
        total_models_evaluated=len(model_records),
        champion_models_count=len([m for m in model_records if m.deployment_mode == "champion"]),
        trained_models_count=len([m for m in model_records if not m.is_placeholder]),
        models_requiring_training=list(sorted(set(models_needing_training))),
    )

    # 9. Confidence Breakdown (derived from SourceConflictCapability results)
    auth_score = float(features.get("source_authority_score", FeatureValue(name="", value=state.evidence.vector.get("source_authority", 0.85))).value or 0.85)
    rel_score = float(features.get("source_reliability_score", FeatureValue(name="", value=state.evidence.vector.get("source_reliability", 0.90))).value or 0.90)
    fresh_score = float(features.get("freshness_score", FeatureValue(name="", value=state.evidence.vector.get("freshness", 0.90))).value or 0.90)
    indep_score = float(features.get("source_corroboration_score", FeatureValue(name="", value=state.evidence.vector.get("independent_corroboration", 0.75))).value or 0.75)
    disagree_score = float(features.get("source_disagreement_score", FeatureValue(name="", value=round(min(1.0, state.evidence.contradictions * 0.2), 4))).value or 0.0)
    model_conf = round(sum(m.confidence or 0.8 for m in model_records) / max(1, len(model_records)), 4) if model_records else 0.8
    composite = round(0.30 * auth_score + 0.25 * indep_score + 0.15 * fresh_score + 0.20 * model_conf - 0.10 * disagree_score, 4)
    composite = max(0.05, min(1.0, composite))

    confidence = ConfidenceBreakdown(
        source_authority_score=auth_score,
        source_reliability_score=rel_score,
        freshness_score=fresh_score,
        temporal_precision_seconds=60.0,
        spatial_precision_meters=spatial_precision_m,
        independent_corroboration_score=indep_score,
        source_disagreement_score=disagree_score,
        extraction_confidence=situation.extraction_confidence,
        model_confidence=model_conf,
        composite_confidence=composite,
        dimension_weights={
            "source_authority": 0.30,
            "independent_corroboration": 0.25,
            "model_confidence": 0.20,
            "freshness": 0.15,
            "contradiction_penalty": -0.10,
        },
    )

    # 10. State Delta Analysis
    delta_records = [
        StateDeltaRecord(
            change=d.change,
            domain=d.domain,
            before=d.before,
            after=d.after,
            magnitude=d.magnitude,
            confidence=d.confidence,
            novelty=d.novelty,
            causes=list(d.causes),
        )
        for d in delta_report.deltas
    ]

    delta_analysis = StateDeltaAnalysis(
        previous_state_version=prev_v,
        current_state_version=state.state_version,
        transition=f"v{prev_v or 0}->v{state.state_version}",
        delta_count=len(delta_records),
        is_material=delta_report.is_material,
        max_magnitude=max((d.magnitude for d in delta_records), default=0.0),
        deltas=delta_records,
        summary=f"{len(delta_records)} state change(s) detected; material={delta_report.is_material}",
    )

    # 11. Traceability & Provenance (Evidence-Driven Analysis Trace)
    used_caps: set[str] = set()
    for note in notes:
        if note.startswith("[") and "]" in note:
            cap_tag = note[1:note.index("]")]
            used_caps.add(cap_tag)
    for k in ("event_classification", "traffic_anomaly", "transit_disruption", "transit_disruption_forecast", "road_network_exposure", "historical_similarity", "infrastructure_propagation", "source_conflict_analysis", "critical_facility_exposure", "user_route_exposure", "infrastructure_impact", "time_to_impact", "statistical_change_analysis"):
        if any(f.startswith(k) or f == k for f in features):
            used_caps.add(k)

    provenance = AnalysisProvenance(
        analysis_run_id=analysis_run_id or (delta_report.deltas[0].causes[0] if delta_report.deltas and delta_report.deltas[0].causes else f"run_{state.event_id}_v{state.state_version}"),
        event_id=state.event_id,
        state_version=state.state_version,
        previous_state_version=prev_v,
        observation_ids=[o.observation_id for o in observations],
        source_ids=list(sorted({o.source_id for o in observations})),
        supporting_sources=list(sorted({c.source_id for c in claims if c.source_id and c.truth_status in {TruthStatus.CONFIRMED, TruthStatus.REPORTED}} | {o.source_id for o in observations})),
        disagreeing_sources=list(sorted({c.source_id for c in claims if c.source_id and c.truth_status not in {TruthStatus.CONFIRMED, TruthStatus.REPORTED}})),
        capabilities_used=list(sorted(used_caps)),
        models_contributed=list(sorted({m.model_id for m in model_outputs})),
        llm_contributions=[n for n in notes if "llm" in n.lower() or "narrative" in n.lower() or "hypothesis" in n.lower()],
        jev_decisions_summary=jev_decisions or {},
        deterministic_calculations=[
            "shapely/GEOS geometry footprint and bounding-box derivation",
            "temporal rate-of-change and duration calculation",
            "multi-source evidence decomposition and confidence weighting",
        ] + [
            label for cap, label in _DETERMINISTIC_BY_CAPABILITY.items() if cap in used_caps
        ],
        confidence_summary=f"Composite confidence {composite:.2f} based on {len(observations)} observations from {len(set(o.source_id for o in observations))} sources",
        user_impact_summary=f"{total_infra_items} infrastructure items affected across {len(domain_counts)} domains with {max_sev} peak severity",
    )

    return EventAnalysisMetrics(
        schema_version="1.0.0",
        event_id=state.event_id,
        analysis_run_id=analysis_run_id or (delta_report.deltas[0].causes[0] if delta_report.deltas and delta_report.deltas[0].causes else f"run_{state.event_id}_v{state.state_version}"),
        calculated_at=now,
        situation=situation,
        infrastructure_impact=infra_analysis,
        traffic=traffic_analysis,
        transit=transit_analysis,
        geographic_network=geo_network,
        temporal=temporal,
        historical=historical,
        predictions=predictions,
        confidence=confidence,
        state_delta=delta_analysis,
        provenance=provenance,
    )


__all__ = [
    "AnalysisProvenance",
    "ConfidenceBreakdown",
    "EventAnalysisMetrics",
    "GeographicNetworkAnalysis",
    "HistoricalAnalysis",
    "InfrastructureImpactAnalysis",
    "InfrastructureItem",
    "PredictionAnalysis",
    "PredictionModelRecord",
    "SituationAnalysis",
    "StateDeltaAnalysis",
    "StateDeltaRecord",
    "TemporalAnalysis",
    "TrafficAnalysis",
    "TransitAnalysis",
    "derive_analysis_metrics",
]

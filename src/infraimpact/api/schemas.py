"""Consumer-facing response contracts (brief section 50/51).

Section 51 is explicit: *"Do not return raw model internals to normal
clients."* That rule is enforced structurally here - the ``/v1`` response
models deliberately have no field for ``model_outputs``, ``features``,
``jev_decisions`` or ``llm_operations``. If an internal field ever needs
surfacing it has to be added deliberately, which is the point.

The internal ``/internal/*`` routes use the stored ``AnalysisRun`` directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.schemas import (
    SCHEMA_VERSION,
    AffectedInfrastructure,
    Claim,
    EvidenceNarrative,
    EvidenceSummary,
    Forecast,
    Observation,
    PresentationItem,
    RouteImpact,
)


class EventSummary(BaseModel):
    """One row in ``GET /v1/events``."""

    event_id: str
    status: str
    region_id: str
    first_observed: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    observation_count: int = 0
    state_version: int | None = None
    last_reconstructed_at: datetime | None = None


class EventListResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int
    events: list[EventSummary]


class CurrentStateView(BaseModel):
    """``current_state`` from section 51 - the latest reconstruction."""

    state_version: int
    status: str
    event_type_distribution: dict[str, float] = Field(default_factory=dict)
    geometry: dict[str, Any] | None = None
    geometry_confidence: float = 0.0
    first_observed: datetime | None = None
    last_observed: datetime | None = None
    reconstructed_at: datetime
    movement: dict[str, Any] = Field(default_factory=dict)
    evidence: EvidenceSummary
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION


class RecentChange(BaseModel):
    """A delta between two consecutive state versions."""

    state_version: int
    previous_state_version: int | None = None
    reconstructed_at: datetime | None = None
    magnitude: float | None = None
    changes: list[dict[str, Any]] = Field(default_factory=list)


class EventDetail(BaseModel):
    """The section 51 response shape, verbatim in its key order."""

    event: EventSummary
    current_state: CurrentStateView | None = None
    top_impacts: list[AffectedInfrastructure] = Field(default_factory=list)
    recent_changes: list[RecentChange] = Field(default_factory=list)
    forecast: list[Forecast] = Field(default_factory=list)
    evidence_summary: EvidenceSummary = Field(default_factory=EvidenceSummary)
    last_updated: datetime | None = None


class TimelineEntry(BaseModel):
    state_version: int
    reconstructed_at: datetime
    status: str
    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    state: dict[str, Any] = Field(default_factory=dict)


class TimelineResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str
    count: int
    entries: list[TimelineEntry]


class EvidenceResponse(BaseModel):
    """Section 50 ``/evidence``: what the belief rests on, with provenance.

    Three fields with three different guarantees, kept separate on purpose:

    * ``summary`` / ``vector`` - **measured**. Counts and scores computed by this
      platform from the ledger. If these are wrong, the platform has a bug.
    * ``observations`` / ``claims`` - **the ledger itself**, with provenance and
      all three timestamps, so any consumer can recompute the above.
    * ``narrative`` - **inferred**. Prose written by a language model about the
      evidence, or ``None`` when none was produced. It is last, optional, and
      self-labelled ``truth_status: "inferred"`` so a client cannot mistake it
      for a count. Merging it into ``summary`` would be the one change that
      makes this endpoint lie.
    """

    schema_version: str = SCHEMA_VERSION
    event_id: str
    source_count: int = 0
    independent_source_count: int = 0
    authorities: dict[str, int] = Field(default_factory=dict)
    observation_types: dict[str, int] = Field(default_factory=dict)
    contradictions: int = 0
    freshness_seconds: float | None = None
    vector: dict[str, float] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    narrative: EvidenceNarrative | None = None


class AnalysisView(BaseModel):
    """Public analysis summary.

    Carries *which* models ran and *why capabilities were skipped* - that is
    audit information, not model internals. It deliberately omits feature
    vectors and raw model outputs.
    """

    analysis_run_id: str
    event_id: str
    trigger: str
    previous_state_version: int | None = None
    new_state_version: int
    capabilities_invoked: tuple[str, ...] = ()
    capabilities_skipped: tuple[tuple[str, str], ...] = ()
    models_invoked: tuple[str, ...] = ()
    impacts: tuple[AffectedInfrastructure, ...] = ()
    forecasts: tuple[Forecast, ...] = ()
    deltas: tuple[dict[str, Any], ...] = ()
    started_at: datetime
    completed_at: datetime


class AnalysisResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str
    count: int
    runs: list[AnalysisView]


class ForecastResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str
    analysis_run_id: str | None = None
    forecasts: list[Forecast] = Field(default_factory=list)


class UserImpactView(BaseModel):
    """A stored ``UserExposure`` plus the presentation that produced alerts."""

    user_id: str
    event_id: str
    analysis_run_id: str
    exposure_level: str
    exposure_score: float
    priority: float
    distance_m: float | None = None
    inside_impact_area: bool = False
    confidence: float = 0.0
    route_impacts: tuple[RouteImpact, ...] = ()
    saved_place_impacts: dict[str, float] = Field(default_factory=dict)
    components: dict[str, float] = Field(default_factory=dict)
    presentation: tuple[PresentationItem, ...] = ()
    as_of: datetime | None = None


class UserImpactsResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    user_id: str
    count: int
    impacts: list[UserImpactView] = Field(default_factory=list)


class RouteImpactView(BaseModel):
    route_id: str
    route_name: str
    user_id: str
    event_id: str
    intersects: bool
    delay_estimate_min: float | None = None
    blocked_node_ids: tuple[str, ...] = ()
    alternative_available: bool | None = None
    evidence_ids: tuple[str, ...] = ()
    as_of: datetime | None = None


class RouteImpactResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    route_id: str
    count: int
    impacts: list[RouteImpactView] = Field(default_factory=list)


class SourceHealthView(BaseModel):
    """Section 56: declared freshness profile merged with observed health."""

    source_id: str
    state: str = "unknown"
    usage: str = "realtime"
    capability_tier: str = "triggered"
    expected_interval_s: float | None = None
    stale_after_s: float | None = None
    freshness_seconds: float | None = None
    stale: bool = False
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    latency_ms: float | None = None
    error_rate: float = 0.0
    records_received: int = 0
    message: str | None = None
    note: str = ""


class RegionStateResponse(BaseModel):
    """``GET /v1/regions/{region_id}/state`` - the section 4 region, as data."""

    schema_version: str = SCHEMA_VERSION
    region_id: str
    display_name: str
    timezone: str
    bounds: tuple[float, float, float, float]
    center: tuple[float, float]
    active_events: int = 0
    open_events: list[EventSummary] = Field(default_factory=list)
    sources: list[SourceHealthView] = Field(default_factory=list)
    observations_total: int = 0
    generated_at: datetime


class NotificationView(BaseModel):
    notification_id: str
    user_id: str
    event_id: str
    reason: str
    urgency: str
    headline: str
    body: str
    presentation: tuple[PresentationItem, ...] = ()
    created_at: datetime | None = None
    sent_at: datetime | None = None


class NotificationListResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int
    notifications: list[NotificationView] = Field(default_factory=list)


class RuntimeStatusResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    region_id: str
    driver: str
    bus_backend: str
    running: bool
    cycles_completed: int
    last_cycle: dict[str, Any] | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    server_time: datetime


class ErrorResponse(BaseModel):
    detail: str
    code: Literal["not_found", "invalid_request", "unavailable"] = "not_found"


__all__ = [
    "AnalysisResponse",
    "AnalysisView",
    "CurrentStateView",
    "ErrorResponse",
    "EvidenceResponse",
    "EventDetail",
    "EventListResponse",
    "EventSummary",
    "ForecastResponse",
    "NotificationListResponse",
    "NotificationView",
    "RecentChange",
    "RegionStateResponse",
    "RouteImpactResponse",
    "RouteImpactView",
    "RuntimeStatusResponse",
    "SourceHealthView",
    "TimelineEntry",
    "TimelineResponse",
    "UserImpactView",
    "UserImpactsResponse",
]
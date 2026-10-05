"""Versioned data contracts.

Implements the envelopes defined in the brief:
  section 6  Observation
  section 8  Claim
  section 11 Claim
  section 12 EventState
  section 34 UserContext
  section 37 PresentationItem
  section 39 EvidenceQuality
  section 43 AnalysisRun
  section 44 ModelOutput
  section 52 StateDelta

Field semantics may only be added, never repurposed (section 65).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    Authority,
    CapabilityTier,
    EdgeKind,
    HealthState,
    InfrastructureDomain,
    InferenceKind,
    NodeClass,
    NotificationReason,
    ObservationType,
    PresentationType,
    SourceType,
    TruthStatus,
    Urgency,
)
from .geo import Geometry
from .ids import new_id

SCHEMA_VERSION = "1.0.0"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Observation (section 6)
# --------------------------------------------------------------------------


class Provenance(Frozen):
    authority: Authority = Authority.UNVERIFIED
    retrieval_method: Literal["api", "feed", "scrape", "derived", "user"] = "api"
    content_hash: str
    connector_version: str = "0.1.0"
    upstream_id: str | None = None
    derived_from: tuple[str, ...] = ()


class ObservationQuality(Frozen):
    """Section 39: components are preserved individually.

    A single ``confidence`` float is never stored alone.
    """

    source_reliability: float = Field(0.5, ge=0.0, le=1.0)
    temporal_precision: float = Field(0.5, ge=0.0, le=1.0)
    spatial_precision: float = Field(0.5, ge=0.0, le=1.0)
    extraction_confidence: float = Field(1.0, ge=0.0, le=1.0)

    @property
    def composite(self) -> float:
        return round(
            0.4 * self.source_reliability
            + 0.2 * self.temporal_precision
            + 0.2 * self.spatial_precision
            + 0.2 * self.extraction_confidence,
            4,
        )


class Observation(BaseModel):
    """Universal observation envelope emitted by every source adapter."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    schema_version: str = SCHEMA_VERSION

    source_id: str
    source_record_id: str

    # Three mandatory timestamps: feeds arrive late and out of order (section 6).
    event_time: datetime
    observed_at: datetime
    ingested_at: datetime

    source_type: SourceType
    observation_type: ObservationType

    geometry: Geometry | None = None
    location_precision_m: float | None = None

    headline: str = ""
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    raw_payload_uri: str | None = None

    source_url: str | None = None

    provenance: Provenance
    quality: ObservationQuality

    centroid: tuple[float, float] | None = None
    event_id: str | None = None

    def dedupe_key(self) -> str:
        """Section 7 dedupe key."""
        from .ids import content_hash, deterministic_id

        explicit = f"{self.source_id}|{self.source_record_id}|{self.observed_at.isoformat()}"
        return deterministic_id("dk", explicit) if self.source_record_id else deterministic_id(
            "dk", content_hash(self.structured_payload)
        )



# --------------------------------------------------------------------------
# Claims (section 11)
# --------------------------------------------------------------------------


class Claim(BaseModel):
    """Atomic assertion extracted from an observation.

    Claims allow disagreement: two sources may assert contradictory values and
    both are kept.
    """

    model_config = ConfigDict(frozen=True)

    claim_id: str
    observation_id: str
    event_id: str | None = None

    predicate: str
    value: Any = None
    unit: str | None = None

    geometry: Geometry | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    extraction_method: Literal["source_structured", "rule", "llm", "derived"] = "rule"
    extraction_confidence: float = Field(1.0, ge=0.0, le=1.0)
    source_id: str | None = None
    truth_status: TruthStatus = TruthStatus.REPORTED


# --------------------------------------------------------------------------
# Infrastructure graph (section 30)
# --------------------------------------------------------------------------


class GraphNode(BaseModel):
    node_id: str
    node_class: NodeClass
    name: str | None = None
    geometry: Geometry | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    edge_id: str
    kind: EdgeKind
    source_node_id: str
    target_node_id: str
    weight: float = 1.0
    attributes: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Event state (section 12)
# --------------------------------------------------------------------------


class MovementState(BaseModel):
    moving: bool = False
    direction_deg: float | None = None
    speed_estimate_m_per_min: float | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class EvidenceNarrative(BaseModel):
    """Section 18 evidence synthesis. Stored on the run, never on the state.

    The separation is the whole point. :class:`EvidenceSummary` holds numbers the
    platform measured from observations: how many sources, how authoritative, how
    contradictory, how stale. A narrative is prose, and prose is only ever as good
    as the model that wrote it - so it is kept in a different object, on
    :class:`AnalysisRun`, rather than inside the evidence it interprets.

    The alternative was a ``narrative`` field on :class:`EvidenceSummary`. It was
    rejected because it would make ``state.evidence.narrative`` look like a
    measured property of the world. It is not. It is one model's reading of these
    numbers on one run, and keeping it in the run record means state
    reconstruction stays deterministic, offline and replayable - which section 33
    requires and which an LLM call in the rebuild path would quietly break.

    Every field is nullable and ``truth_status`` is fixed at ``"inferred"``. There
    is no code path that promotes a narrative field to CONFIRMED: a model's
    summary of ten confirmed observations is still an inference about what they
    mean.
    """

    truth_status: Literal["inferred"] = "inferred"
    source: Literal["llm", "none"] = "none"

    summary: str | None = None
    confirmed_facts: tuple[str, ...] = ()
    reported_claims: tuple[str, ...] = ()
    inferred_points: tuple[str, ...] = ()
    #: Sources as *named by the narrative*. Advisory only - the measured
    #: ``EvidenceSummary.source_count`` is what anything numeric uses, because a
    #: model can miscount and a miscount must not propagate.
    sources: tuple[str, ...] = ()
    confidence_score: float = Field(0.0, ge=0.0, le=1.0)
    #: Operations that shaped this narrative, for replay (section 43).
    operations: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class EvidenceSummary(BaseModel):
    """Section 39/40: corroboration discounts dependent sources.

    Measured only. Nothing a model wrote is ever stored here - see
    :class:`EvidenceNarrative` for why.
    """

    source_count: int = 0
    independent_source_count: int = 0
    authorities: dict[str, int] = Field(default_factory=dict)
    observation_types: dict[str, int] = Field(default_factory=dict)
    contradictions: int = 0
    freshness_seconds: float | None = None
    vector: dict[str, float] = Field(default_factory=dict)


class AffectedInfrastructure(BaseModel):
    domain: InfrastructureDomain
    identifier: str
    name: str | None = None
    geometry: Geometry | None = None
    severity: Urgency = Urgency.LOW
    truth_status: TruthStatus = TruthStatus.CONFIRMED
    observation_ids: tuple[str, ...] = ()
    detected_at: datetime | None = None


# --------------------------------------------------------------------------
# Magnitude, rate, duration (world-state scale)
# --------------------------------------------------------------------------


class QuantityEstimate(BaseModel):
    """One measured magnitude, carrying its own uncertainty.

    A bare number is the thing this exists to prevent. "300" is not a
    measurement; "300, somewhere between 150 and 600, asserted by two
    independent sources that disagree by a factor of two" is. The three numbers
    travel together or not at all:

    ``value``/``lower``/``upper``  the estimate and the interval it falls in
    ``disagreement``               0..1 spread between sources, so "two sources
                                   agree" and "two sources contradict" are
                                   distinguishable from "one source said 300"
    ``confidence``                 how much weight downstream may place on it

    Sources are *not* averaged into a false consensus (section 11). When two
    sources disagree the disagreement is widened into the interval and reported,
    rather than silently collapsed to a midpoint.
    """

    quantity: str
    unit: str | None = None
    value: float | None = None
    lower: float | None = None
    upper: float | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    method: Literal["source_structured", "aggregate", "derived", "claim"] = "source_structured"
    observation_ids: tuple[str, ...] = ()
    source_count: int = 1
    disagreement: float = Field(0.0, ge=0.0, le=1.0)
    #: True when no source in the ledger asserts this quantity for this event.
    #: The platform reports an unknown magnitude as absent rather than as zero.
    unavailable: bool = False


class EventRate(BaseModel):
    """How fast information about this event is arriving.

    Rate is the cheapest available proxy for whether a situation is developing,
    and it needs no event-type knowledge: a protest, a fire and an earthquake all
    produce a rising observation rate while they escalate and a falling one as
    they end.
    """

    observation_rate_per_hour: float = 0.0
    recent_rate_per_hour: float = 0.0
    trend: Literal["rising", "falling", "steady", "unknown"] = "unknown"
    trend_strength: float = Field(0.0, ge=0.0, le=1.0)


class EventDuration(BaseModel):
    elapsed_seconds: float = 0.0
    active_span_seconds: float = 0.0
    since_first_observed: datetime | None = None
    last_observation_at: datetime | None = None
    #: How long the platform has seen nothing. The lifecycle engine reads this
    #: rather than a hardcoded interval, because "how long is quiet" is
    #: different for a 4-minute traffic stop and a 3-day wildfire.
    silence_seconds: float = 0.0


class EventScale(BaseModel):
    """How big, how fast, how long. Absent when the data does not permit it.

    Every field here is nullable in spirit and default-empty in fact. An event
    whose sources report no magnitude gets ``magnitudes=()`` and
    ``primary=None`` - it does not get ``value=0.0``, which would assert an
    absence of magnitude rather than an absence of measurement.
    """

    magnitudes: tuple[QuantityEstimate, ...] = ()
    primary: QuantityEstimate | None = None
    rate: EventRate = Field(default_factory=EventRate)
    duration: EventDuration = Field(default_factory=EventDuration)


class LifecycleAssessment(BaseModel):
    """Why an event is in the state it is in.

    Termination is the hardest thing to get right in a world-state system,
    because closing an event that is still happening is worse than leaving a
    finished one open: the first silently deletes live information from every
    downstream view. So the verdict is stored with its basis and its evidence,
    and never as a bare string.

    ``termination_basis`` is what makes it auditable:

    ``official_release``   an official record says it is over
    ``resolution_record``  a reopening/restoration record retired the disruption
    ``silence``            no new information for longer than this event's own
                           reporting cadence implies is normal
    ``observation_cease``  the reporting window itself ended
    ``unknown``            nothing to conclude yet
    """

    status: Literal["candidate", "active", "quiescent", "closed"] = "candidate"
    reason: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    termination_basis: Literal[
        "official_release",
        "resolution_record",
        "silence",
        "observation_cease",
        "unknown",
    ] = "unknown"
    evidence_observation_ids: tuple[str, ...] = ()
    quiet_since: datetime | None = None
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: The silence threshold this verdict used, so a later reader can tell
    #: whether a different threshold would have given a different answer.
    silence_threshold_seconds: float | None = None


class EventState(BaseModel):
    """A point-in-time reconstruction. Never overwritten - a new version is written."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    schema_version: str = SCHEMA_VERSION
    state_version: int = 1

    event_type_distribution: dict[str, float] = Field(default_factory=dict)
    status: Literal["candidate", "active", "quiescent", "closed"] = "candidate"

    geometry: Geometry | None = None
    geometry_confidence: float = Field(0.0, ge=0.0, le=1.0)

    first_observed: datetime | None = None
    last_observed: datetime | None = None
    reconstructed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    movement: MovementState = Field(default_factory=MovementState)

    affected_infrastructure: tuple[AffectedInfrastructure, ...] = ()
    evidence: EvidenceSummary = Field(default_factory=EvidenceSummary)

    #: How big, how fast, how long. Additive per section 65 - an event with no
    #: measurable scale carries empty defaults, never zeroes implying a
    #: measurement of "nothing".
    scale: EventScale = Field(default_factory=EventScale)
    #: Why this event is active/quiescent/closed, with its evidence.
    lifecycle: LifecycleAssessment = Field(default_factory=LifecycleAssessment)

    observation_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()

    feature_vector_version: str = SCHEMA_VERSION
    derived: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Analysis (sections 43, 44)
# --------------------------------------------------------------------------


class FeatureValue(BaseModel):
    name: str
    value: Any = None
    unit: str | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    features_version: str = SCHEMA_VERSION
    as_of: datetime | None = None
    stale: bool = False


class Forecast(BaseModel):
    """Section 24: time-to-impact. Probability that impact exists by horizon."""

    target: str
    domain: InfrastructureDomain = InfrastructureDomain.UNKNOWN
    horizon_minutes: int
    probability: float = Field(0.0, ge=0.0, le=1.0)
    lower: float = Field(0.0, ge=0.0, le=1.0)
    upper: float = Field(0.0, ge=0.0, le=1.0)
    model_id: str | None = None
    model_version: str | None = None
    calibration_version: str | None = None
    truth_status: TruthStatus = TruthStatus.PREDICTED


class ModelOutput(BaseModel):
    """Section 44: never store only the displayed value."""

    model_id: str
    model_version: str
    prediction_time: datetime
    input_state_version: int | None = None
    features_version: str = SCHEMA_VERSION
    output: dict[str, Any] = Field(default_factory=dict)
    probability: float | None = None
    uncertainty: float | None = None
    calibration_version: str | None = None


class StateDelta(BaseModel):
    """Section 32/54: 'What changed?' is first-class."""

    change: str
    domain: str = "event"
    before: Any = None
    after: Any = None
    magnitude: float = Field(0.0, ge=0.0, le=1.0)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    novelty: float = Field(0.0, ge=0.0, le=1.0)
    causes: tuple[str, ...] = ()
    official_guidance: bool = False

    # Section 32 lists urgency and affected-user count alongside magnitude /
    # confidence / novelty. Added additively; no existing field was repurposed.
    urgency: Urgency = Urgency.NONE
    affected_user_count: int = 0


class StateDeltaRecord(BaseModel):
    """A delta as a durable, independently queryable row.

    ``StateDelta`` above is a value object: it lives inside an ``AnalysisRun``,
    which means it is only reachable by parsing that run's JSON blob, and it
    disappears entirely if the run is never written. That is wrong for the
    world's central question - *what changed?* - because the answer must exist
    the moment the state it describes exists, independent of whether any
    analysis subsequently ran on it.

    So a delta is written in the same transaction as the state version it
    describes, and carries the provenance that makes it trustworthy on its own:
    which observations caused it, which state version it came from, and whether
    it cleared the materiality floor.
    """

    delta_id: str
    schema_version: str = SCHEMA_VERSION

    event_id: str
    region_id: str = ""
    state_version: int
    previous_state_version: int | None = None

    change: str
    domain: str = "event"
    before: Any = None
    after: Any = None
    magnitude: float = Field(0.0, ge=0.0, le=1.0)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    novelty: float = Field(0.0, ge=0.0, le=1.0)
    is_material: bool = False

    causes: tuple[str, ...] = ()
    urgency: Urgency = Urgency.NONE
    affected_user_count: int = 0
    official_guidance: bool = False

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_delta(
        cls,
        delta: StateDelta,
        *,
        event_id: str,
        state_version: int,
        previous_state_version: int | None,
        region_id: str = "",
        is_material: bool = False,
        recorded_at: datetime | None = None,
    ) -> "StateDeltaRecord":
        from .ids import deterministic_id

        stamp = recorded_at or datetime.now(UTC)
        return cls(
            delta_id=deterministic_id(
                "dlt", event_id, state_version, delta.change, delta.domain
            ),
            event_id=event_id,
            region_id=region_id,
            state_version=state_version,
            previous_state_version=previous_state_version,
            change=delta.change,
            domain=delta.domain,
            before=delta.before,
            after=delta.after,
            magnitude=delta.magnitude,
            confidence=delta.confidence,
            novelty=delta.novelty,
            is_material=is_material,
            causes=delta.causes,
            urgency=delta.urgency,
            affected_user_count=delta.affected_user_count,
            official_guidance=delta.official_guidance,
            recorded_at=stamp,
        )


class AnalysisRun(BaseModel):
    """Section 43: makes every production decision replayable."""
    model_config = ConfigDict(frozen=True)

    analysis_run_id: str
    schema_version: str = SCHEMA_VERSION
    event_id: str
    region_id: str
    trigger: str
    trigger_observation_id: str | None = None

    previous_state_version: int | None = None
    new_state_version: int

    features: dict[str, FeatureValue] = Field(default_factory=dict)
    capabilities_invoked: tuple[str, ...] = ()
    capabilities_skipped: tuple[tuple[str, str], ...] = ()
    models_invoked: tuple[str, ...] = ()
    jev_decisions: dict[str, Any] = Field(default_factory=dict)
    llm_operations: tuple[str, ...] = ()
    #: Section 18 hypotheses: infrastructure relationships worth examining, and
    #: what the LLM was told when it proposed them. Stored for the next cycle
    #: and for replay. Deliberately absent from every ``/v1`` response model -
    #: a hypothesis is a question the platform has not answered.
    hypotheses: tuple[dict[str, Any], ...] = ()
    #: Section 18 evidence synthesis for this run. Deliberately *not* on
    #: ``EventState``: state reconstruction must stay deterministic and offline,
    #: so a model's reading of the evidence lives on the run that asked for it.
    narrative: EvidenceNarrative | None = None

    forecasts: tuple[Forecast, ...] = ()
    impacts: tuple[AffectedInfrastructure, ...] = ()
    deltas: tuple[StateDelta, ...] = ()
    model_outputs: tuple[ModelOutput, ...] = ()
    metrics: Any = None

    started_at: datetime
    completed_at: datetime

    software_versions: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# User (sections 34, 35, 36, 37)
# --------------------------------------------------------------------------


class SavedPlace(BaseModel):
    place_id: str
    name: str
    geometry: Geometry
    kind: Literal["home", "work", "school", "other"] = "other"


class RouteProfile(BaseModel):
    route_id: str
    name: str
    geometry: Geometry
    modes: tuple[str, ...] = ("drive",)
    node_ids: tuple[str, ...] = ()
    typical_departure_local: str | None = None


class NotificationPreferences(BaseModel):
    minimum_urgency: Urgency = Urgency.LOW
    quiet_hours_local: tuple[int, int] | None = None
    channels: tuple[str, ...] = ("in_app",)
    max_alerts_per_hour: int = 5


class UserContext(BaseModel):
    """Section 34/59: minimal, user-supplied, ephemeral by default."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    saved_places: tuple[SavedPlace, ...] = ()
    route_profiles: tuple[RouteProfile, ...] = ()
    transport_modes: tuple[str, ...] = ()
    preferences: NotificationPreferences = Field(default_factory=NotificationPreferences)

    # Ephemeral: expires unless the user explicitly saves.
    current_location: Geometry | None = None
    current_location_expires_at: datetime | None = None
    active_destination_id: str | None = None
    active_route_id: str | None = None


class RouteImpact(BaseModel):
    route_id: str
    route_name: str
    intersects: bool = False
    delay_estimate_min: float | None = None
    blocked_node_ids: tuple[str, ...] = ()
    alternative_available: bool | None = None
    alternative_geometry: Geometry | None = None
    evidence_ids: tuple[str, ...] = ()


class UserExposure(BaseModel):
    """Section 35: primarily deterministic and geospatial."""

    user_id: str
    event_id: str
    analysis_run_id: str

    distance_m: float | None = None
    inside_impact_area: bool = False

    route_impacts: tuple[RouteImpact, ...] = ()
    saved_place_impacts: dict[str, float] = Field(default_factory=dict)

    exposure_level: Urgency = Urgency.NONE
    exposure_score: float = Field(0.0, ge=0.0, le=1.0)

    components: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    as_of: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserImpactState(BaseModel):
    """Previous-vs-current comparison for one user (section 54)."""

    user_id: str
    event_id: str
    previous_analysis_run_id: str | None = None
    previous_exposure_level: Urgency | None = None
    current: UserExposure
    deltas: tuple[StateDelta, ...] = ()


class UserPriority(BaseModel):
    """Section 36: components stored independently, never collapsed."""

    user_id: str
    event_id: str
    impact_magnitude: float = Field(0.0, ge=0.0, le=1.0)
    user_exposure: float = Field(0.0, ge=0.0, le=1.0)
    urgency: float = Field(0.0, ge=0.0, le=1.0)
    change_magnitude: float = Field(0.0, ge=0.0, le=1.0)
    evidence_confidence: float = Field(0.0, ge=0.0, le=1.0)
    priority: float = Field(0.0, ge=0.0, le=1.0)
    urgency_band: Urgency = Urgency.NONE
    components: dict[str, float] = Field(default_factory=dict)


class PresentationItem(BaseModel):
    """Section 37: ranked objects, not screen instructions."""

    type: PresentationType
    priority: float = Field(0.0, ge=0.0, le=1.0)
    headline: str = ""
    detail: str | None = None
    truth_status: TruthStatus = TruthStatus.INFERRED
    evidence_ids: tuple[str, ...] = ()
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)


class NotificationCandidate(BaseModel):
    notification_id: str
    user_id: str
    event_id: str
    reason: NotificationReason
    urgency: Urgency
    headline: str
    body: str
    presentation: tuple[PresentationItem, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    dedupe_key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------
# Source health (sections 55, 56)
# --------------------------------------------------------------------------


class SourceHealth(BaseModel):
    source_id: str
    state: HealthState = HealthState.UNKNOWN
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    latency_ms: float | None = None
    error_rate: float = 0.0
    records_received: int = 0
    freshness_seconds: float | None = None
    expected_interval_s: float | None = None
    stale_after_s: float | None = None
    #: ``context_only``, not ``context``: the name the catalogue and every
    #: adapter class already use, and the more precise one. A ``context_only``
    #: signal still becomes observations - it just may never raise a
    #: notification on its own, and "only" is the half of that rule worth
    #: keeping in the vocabulary. This was ``"context"`` until a live run
    #: crashed on the mismatch, which is the argument for keeping schema
    #: literals and their producers in one place rather than two.
    usage: Literal["realtime", "historical_only", "context_only"] = "realtime"
    message: str | None = None
    capability_tier: CapabilityTier = CapabilityTier.TRIGGERED


# --------------------------------------------------------------------------
# World state (cross-event projection)
# --------------------------------------------------------------------------


class EventLifecycleTransition(BaseModel):
    """One recorded change of an event's lifecycle state.

    ``EventState.status`` is a *conclusion*; this is the *history* of how the
    platform reached it. Without the history, a status column that has been
    overwritten in place cannot answer the only question that matters after the
    fact: why did the system believe this was over, and on what evidence. That
    question has to be answerable while the event is still active too, because
    deciding to close an event is the decision most worth being able to audit.
    """

    #: Self-assigned. It was a required argument, and the runtime constructed
    #: this record without one - which raised a ValidationError inside the
    #: per-event transaction, so the state version, the deltas and the
    #: lifecycle row were all rolled back together. The loop swallowed the
    #: exception per event, ran to completion, and reported a clean cycle while
    #: persisting no state at all. An identity field that a caller can forget
    #: should not be one; durable records generate their own.
    transition_id: str = Field(default_factory=lambda: new_id("lct"))
    event_id: str
    region_id: str = ""

    from_status: str | None = None
    to_status: str
    reason: str = ""
    termination_basis: str = "unknown"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    evidence_observation_ids: tuple[str, ...] = ()
    silence_threshold_seconds: float | None = None
    observation_count: int = 0
    state_version: int | None = None

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_assessment(
        cls,
        assessment: LifecycleAssessment,
        *,
        event_id: str,
        previous_status: str | None,
        region_id: str = "",
        observation_count: int = 0,
        state_version: int | None = None,
    ) -> "EventLifecycleTransition":
        from .ids import deterministic_id

        return cls(
            transition_id=deterministic_id(
                "lcy", event_id, previous_status or "-", assessment.status, assessment.assessed_at.isoformat()
            ),
            event_id=event_id,
            region_id=region_id,
            from_status=previous_status,
            to_status=assessment.status,
            reason=assessment.reason,
            termination_basis=assessment.termination_basis,
            confidence=assessment.confidence,
            evidence_observation_ids=assessment.evidence_observation_ids,
            silence_threshold_seconds=assessment.silence_threshold_seconds,
            observation_count=observation_count,
            state_version=state_version,
            at=assessment.assessed_at,
        )


class WorldEventEntry(BaseModel):
    """One event's contribution to "what is happening right now".

    Deliberately not a full ``EventState``. A world view has to be cheap enough
    to rebuild on every cycle and small enough to serve whole, so it carries the
    fields that answer "is this happening, where, how bad, and did it just
    change" and nothing else. The full reconstruction is one lookup away on
    ``/v1/events/{id}``.
    """

    event_id: str
    status: str = "candidate"
    state_version: int = 1

    dominant_type: str | None = None
    type_distribution: dict[str, float] = Field(default_factory=dict)

    first_observed: datetime | None = None
    last_observed: datetime | None = None
    centroid: tuple[float, float] | None = None
    footprint_area_m2: float = 0.0

    moving: bool = False
    movement_speed: float | None = None
    movement_direction_deg: float | None = None

    affected_domains: tuple[str, ...] = ()
    impact_count: int = 0
    severity_peak: Urgency = Urgency.NONE

    evidence_confidence: float = Field(0.0, ge=0.0, le=1.0)
    independent_source_count: int = 0

    primary_magnitude: QuantityEstimate | None = None
    duration_seconds: float = 0.0
    observation_rate_per_hour: float = 0.0
    rate_trend: str = "unknown"

    #: Did this event's state change materially on the last rebuild? This is
    #: what separates "500 things are open" from "10 of them just moved", which
    #: is the distinction section 33 says should drive how much work happens.
    changed_materially: bool = False
    last_change: str | None = None
    lifecycle_reason: str | None = None


class WorldSnapshot(BaseModel):
    """The platform's answer to "what is happening in the world right now?".

    Assembled from every event's latest state plus source health. It is a
    projection, not a source of truth: it can always be rebuilt from the ledger,
    and it records the observation count and generation time it was built from
    so a stale snapshot is recognisable as stale rather than silently served as
    current.
    """

    snapshot_id: str
    schema_version: str = SCHEMA_VERSION
    region_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    observation_total: int = 0
    events_total: int = 0
    events_active: int = 0
    events_quiescent: int = 0
    events_closed: int = 0
    events_changed_materially: int = 0

    events: tuple[WorldEventEntry, ...] = ()

    #: domain -> number of events currently affecting it.
    domain_activity: dict[str, int] = Field(default_factory=dict)
    #: source_id -> health state value.
    source_health: dict[str, str] = Field(default_factory=dict)
    sources_degraded: tuple[str, ...] = ()
    #: Signals the catalogue knows cannot be observed right now. Carried into
    #: the world view on purpose: a world picture that silently omits traffic
    #: because the feed is unreachable reads exactly like a world with no
    #: traffic problems.
    coverage_gaps: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "generated_at": self.generated_at.isoformat(),
            "observation_total": self.observation_total,
            "events_total": self.events_total,
            "events_active": self.events_active,
            "events_quiescent": self.events_quiescent,
            "events_closed": self.events_closed,
            "events_changed_materially": self.events_changed_materially,
            "domain_activity": dict(self.domain_activity),
            "sources_degraded": list(self.sources_degraded),
            "coverage_gaps": list(self.coverage_gaps),
        }
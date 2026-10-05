"""Event resolution (brief section 10).

Four records about "downtown" are potentially four observations of *one*
event. The resolver answers:

    Are these observations referring to the same real-world event?

Candidate generation uses cheap deterministic constraints first (time window,
geographic radius), then semantic/entity scoring. The LLM may assist ambiguous
cases, but deterministic evidence always remains available and is recorded.

Merges are never irreversible: ``event_merges`` retains history and aliases are
retained so a split remains possible.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Sequence

from ..domain.enums import InfrastructureDomain, ObservationType
from ..domain.geo import bbox_polygon, centroid_of, distance_m, haversine_m, union_bbox
from ..domain.ids import deterministic_id, ensure_utc, new_id, utcnow
from ..domain.schemas import Claim, EventState, MovementState, Observation
from ..llm.client import LlmClient, NullLlmClient
from ..storage.repository import (
    EventLifecycleRepository,
    EventRepository,
    ObservationRepository,
)
from .identity import EventIdentityAllocator

log = logging.getLogger(__name__)

#: Observation types that imply the presence of a real-world event.
EVENT_BEARING_TYPES = frozenset(
    {
        ObservationType.PERMIT_EVENT,
        ObservationType.PUBLIC_GATHERING_REPORT,
        ObservationType.POLICE_RESPONSE,
        ObservationType.FIRE_DISPATCH,
        ObservationType.OFFICIAL_EMERGENCY_NOTICE,
        ObservationType.EARTHQUAKE,
        ObservationType.SEVERE_WEATHER,
        ObservationType.FLOODING,
        ObservationType.WILDFIRE,
        ObservationType.ROAD_CLOSURE,
        ObservationType.TRANSIT_SERVICE_ALERT,
        ObservationType.POWER_OUTAGE,
    }
)

#: Cross-compatible types for incidents (police CAD, fire dispatch, road closures, news, traffic)
INCIDENT_COMPATIBLE_TYPES = frozenset(
    {
        ObservationType.POLICE_RESPONSE.value,
        ObservationType.FIRE_DISPATCH.value,
        ObservationType.ROAD_CLOSURE.value,
        ObservationType.ROAD_CONSTRUCTION.value,
        ObservationType.NEWS_ARTICLE.value,
        ObservationType.TRAFFIC_FLOW.value,
        ObservationType.TRAFFIC_CONDITION.value,
        ObservationType.TRANSIT_SERVICE_ALERT.value,
        ObservationType.OFFICIAL_EMERGENCY_NOTICE.value,
    }
)

#: Common incident synonym mappings to canonicalize semantic similarity
SYNONYMS: dict[str, str] = {
    "crash": "collision",
    "accident": "collision",
    "crashed": "collision",
    "wrecks": "collision",
    "wreck": "collision",
    "blocked": "closure",
    "blockage": "closure",
    "blocking": "closure",
    "closed": "closure",
    "closures": "closure",
    "shutdown": "closure",
    "delay": "delay",
    "delays": "delay",
    "congestion": "delay",
    "slowdown": "delay",
    "slow": "delay",
    "stalled": "delay",
}

#: Evidence weight by observation type when scoring event-type compatibility.
TYPE_AFFINITY: dict[ObservationType, float] = {
    ObservationType.PERMIT_EVENT: 0.6,
    ObservationType.PUBLIC_GATHERING_REPORT: 0.9,
    ObservationType.NEWS_ARTICLE: 0.7,
    ObservationType.POLICE_RESPONSE: 0.6,
    ObservationType.FIRE_DISPATCH: 0.4,
    ObservationType.OFFICIAL_EMERGENCY_NOTICE: 0.5,
    ObservationType.EARTHQUAKE: 0.9,
    ObservationType.SEVERE_WEATHER: 0.7,
    ObservationType.ROAD_CLOSURE: 0.3,
    ObservationType.TRANSIT_SERVICE_ALERT: 0.3,
    ObservationType.POWER_OUTAGE: 0.2,
}

#: How many closed events are examined, newest first, when deciding which are
#: eligible to be reopened. Bounds the work of recovery without gating on age,
#: which is the mistake: an age-based horizon longer than the candidate time
#: window makes reopening unreachable rather than merely rare.
_RECOVERY_CLOSED_LIMIT = 500

#: Closure bases that count as statements about the world rather than inferences
#: from its silence. Events closed on these grounds are not reopened by a late
#: report.
_DEFINITIVE_CLOSURES = frozenset({"official_release", "resolution_record"})

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "is",
        "are", "was", "were", "has", "have", "with", "by", "from", "as", "it",
        "this", "that", "these", "those", "be", "been", "being", "not", "all",
        "reported", "reports", "reporting", "update", "updates", "updated",
        # Generic public safety, news, and dispatch boilerplate
        "assistance", "rendered", "directed", "patrol", "activity", "checks",
        "prevention", "priority", "call", "calls", "handling", "officer", "officers",
        "police", "response", "dispatch", "dispatched", "investigation", "investigating",
        "incident", "incidents", "premise", "service", "unit", "units", "scene",
        "unable", "locate", "complainant", "problem", "solving", "project",
        "seattle", "washington", "department", "issued", "alert", "alerts",
        "article", "news", "story", "stories", "media", "block", "ave", "st",
    }
)



@dataclass
class Resolution:
    """Outcome of resolving one observation."""

    event_id: str
    is_new_event: bool
    score: float
    reason: str
    candidate_event_id: str | None = None
    evidence: dict[str, float] = field(default_factory=dict)
    decided_by: str = "deterministic"

    def payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "is_new_event": self.is_new_event,
            "score": round(self.score, 4),
            "reason": self.reason,
            "candidate_event_id": self.candidate_event_id,
            "evidence": {k: round(v, 4) for k, v in self.evidence.items()},
            "decided_by": self.decided_by,
        }


@dataclass
class Candidate:
    """A possible event for an observation to join.

    ``state`` is optional because it usually is not there yet. The runtime
    resolves and extracts claims for every observation the moment it is ingested,
    and only reconstructs state versions afterwards, so during a burst - exactly
    the situation where matching matters most - the events an observation could
    join have linked observations but no reconstructed state.

    Skipping those candidates made the resolver's scoring inert in the one
    scenario it exists for: every observation in a batch found nothing to match
    and opened its own event. So everything the scorer needs is derived from the
    linked observations when the state version is absent, using the same
    conventions :class:`~infraimpact.events.world_state.WorldStateEngine` uses,
    and the resolver cannot reach a different conclusion about an event than the
    state engine reaches a moment later.
    """

    event_id: str
    state: EventState | None
    observations: list[Observation]
    claims: list[Claim]

    def __post_init__(self) -> None:
        self._geometry: dict[str, Any] | None = (
            self.state.geometry if self.state is not None else self._hull()
        )
        self._types: set[str] = set(
            self.state.evidence.observation_types
            if self.state is not None
            else (o.observation_type.value for o in self.observations)
        )
        self._first_observed: Any = (
            self.state.first_observed
            if self.state is not None
            else (min((o.event_time for o in self.observations), default=None))
        )

    @property
    def geometry(self) -> dict[str, Any] | None:
        """Where the event is, from the state version if one exists.

        Falls back to the hull of the observations already linked to the event,
        because during a burst there usually is no state version yet - see
        :class:`Candidate`'s docstring.
        """

        return self._geometry

    @property
    def observation_types(self) -> set[str]:
        return self._types

    @property
    def first_observed(self) -> Any:
        return self._first_observed

    def _hull(self) -> dict[str, Any] | None:
        """Bounding footprint of the linked observations.

        Deliberately the same upper-bound convention as
        ``WorldStateEngine._footprint`` - a union bbox, not a claimed shape - so
        that a candidate with no reconstructed state and a candidate with one
        describe the same area, and a measurement can never depend on which of
        the two happened to be available.
        """

        geometries = [o.geometry for o in self.observations if o.geometry]
        if not geometries:
            return None
        bbox = union_bbox(geometries)
        if bbox is None:
            return geometries[0]
        return bbox_polygon(bbox)


class EventResolver:
    def __init__(
        self,
        events: EventRepository,
        observations: ObservationRepository,
        claims,
        states,
        region_id: str,
        *,
        lifecycle: EventLifecycleRepository | None = None,
        time_window_min: float = 180.0,
        search_radius_m: float = 2500.0,
        merge_threshold: float = 0.62,
        llm: LlmClient | None = None,
    ) -> None:
        self.events = events
        #: Only consulted for closed events, so this may be None in callers that
        #: never close anything - an absent lifecycle log means "closed for no
        #: stated reason", which is recoverable.
        self.lifecycle = lifecycle
        self.observations = observations
        self.claims_repo = claims
        self.states = states
        self.region_id = region_id
        self.time_window_min = time_window_min
        self.search_radius_m = search_radius_m
        self.merge_threshold = merge_threshold
        self.llm = llm or NullLlmClient()
        #: Allocates event identity. See
        #: :class:`~infraimpact.events.identity.EventIdentityAllocator` for why
        #: the id cannot be a hash of region/type/day.
        self.identities = EventIdentityAllocator(region_id)
        from .claims import RuleClaimExtractor

        self.claim_extractor = RuleClaimExtractor()
        #: Scoped to a single :meth:`resolve` call and cleared on the way out,
        #: so it can never go stale across a burst of observations that opens new
        #: events. An instance attribute only because ``_candidates`` is reached
        #: through the scoring helpers; a stale copy would silently stop
        #: resolving to events created moments earlier.
        self._active_cache: set[str] | None = None
        #: Events the ledger already knows about, for identity allocation. Same
        #: per-burst scoping as ``_active_cache``.
        self._known_events_cache: set[str] | None = None


    # -- public API -------------------------------------------------------

    def resolve(
        self,
        observation: Observation,
        existing_claims: Sequence[Claim] = (),
    ) -> Resolution:
        self._active_cache = None
        self._known_events_cache = None
        try:
            return self._resolve(observation, existing_claims)
        finally:
            self._active_cache = None
            self._known_events_cache = None

    def _resolve(
        self,
        observation: Observation,
        existing_claims: Sequence[Claim] = (),
    ) -> Resolution:
        # Idempotency before scoring. Every feed is polled on a timer and re-reads
        # its whole window each time, so most observations reaching this point are
        # ones already resolved minutes or hours ago. Deciding that here - rather
        # than by re-running matching and hoping it lands on the same answer -
        # is what makes "reprocessing a source cannot fork an event" true by
        # construction rather than by luck.
        already = self.events.event_for_observation(observation.observation_id)
        if already is not None:
            log.debug("observation %s already resolved to %s", observation.observation_id, already)
            return Resolution(
                event_id=already,
                is_new_event=False,
                score=1.0,
                reason="observation already linked to this event (idempotent replay)",
                candidate_event_id=already,
                decided_by="idempotent",
            )

        candidates = self._candidates(observation)

        if not candidates:
            return self._open_event(
                observation,
                "no candidate within temporal/geographic constraints",
            )

        scored = [(self.score(observation, c, existing_claims), c) for c in candidates]
        scored.sort(key=lambda pair: pair[0].score, reverse=True)
        best_score, best = scored[0]

        if best_score.score < self.merge_threshold or not self._has_corroboration(
            best_score.evidence
        ):
            reason = (
                f"best candidate {best.event_id} scored {best_score.score:.2f} "
                f"< threshold {self.merge_threshold:.2f} ({best_score.reason})"
            )
            if best_score.score >= self.merge_threshold:
                reason += "; no corroborating evidence beyond proximity"
            return self._open_event(
                observation,
                reason,
                candidate_event_id=best.event_id,
                evidence=best_score.evidence,
                score=best_score.score,
            )

        # Ambiguous band: deterministic evidence is close but not decisive.
        decided_by = "deterministic"
        if best_score.score < self.merge_threshold + 0.12 and len(candidates) > 1:
            llm_decision = self.llm.resolve_event(
                candidate=self._candidate_summary(observation, existing_claims),
                options=[self._candidate_summary_obs(observation, c) for _, c in scored[:4]],
            )
            proposed = llm_decision.get("event_id")
            if proposed in {c.event_id for c in candidates}:
                best = next(c for c in candidates if c.event_id == proposed)
                best_score = next(s for s, c in scored if c.event_id == proposed)
                decided_by = "llm_assisted"
                log.info("llm-assisted resolution to %s", proposed)

        self.events.link_observation(best.event_id, observation.observation_id)
        self.events.ensure(best.event_id, observation.event_time, self.region_id)
        self._reactivate(best.event_id, observation)
        return Resolution(
            event_id=best.event_id,
            is_new_event=False,
            score=best_score.score,
            reason=best_score.reason,
            candidate_event_id=best.event_id,
            evidence=best_score.evidence,
            decided_by=decided_by,
        )

    def _open_event(
        self,
        observation: Observation,
        reason: str,
        *,
        candidate_event_id: str | None = None,
        evidence: dict[str, float] | None = None,
        score: float = 1.0,
    ) -> Resolution:
        """Open a new event with an identity the rest of the system can trust.

        Delegates to the allocator rather than hashing attributes here, so the
        rule about when two situations may share an id lives in exactly one
        place.
        """

        allocation = self.identities.allocate(
            observation,
            event_exists=self._event_exists,
            event_for_observation=self.events.event_for_observation,
            observations_of=self.events.observations_of,
        )
        self.events.ensure(allocation.event_id, observation.event_time, self.region_id)
        self.events.link_observation(allocation.event_id, observation.observation_id)
        now = utcnow()
        event_time = ensure_utc(observation.event_time)
        if event_time > now + timedelta(days=1) or (
            observation.observation_type == ObservationType.PERMIT_EVENT and event_time > now
        ):
            initial_status = "planned"
        elif (now - event_time).total_seconds() > 7 * 86400:
            initial_status = "closed"
        elif observation.observation_type in (
            ObservationType.BRIDGE_RESTRICTION,
            ObservationType.FERRY_STATUS,
            ObservationType.NEWS_ARTICLE,
            ObservationType.WEATHER_CONDITION,
            ObservationType.VEHICLE_POSITION,
            ObservationType.CAMERA_IMAGERY,
        ):
            initial_status = "quiescent"
        elif observation.observation_type not in EVENT_BEARING_TYPES:
            initial_status = "quiescent"
        else:
            initial_status = "active"

        self.events.set_status(
            allocation.event_id,
            initial_status,
            observation.observed_at,
            f"first report: {allocation.basis}",
        )
        if self._known_events_cache is not None:
            self._known_events_cache.add(allocation.event_id)
        log.info(
            "opened event %s for observation %s (%s, ordinal %d, status=%s)",
            allocation.event_id,
            observation.observation_id,
            allocation.basis,
            allocation.ordinal,
            initial_status,
        )

        return Resolution(
            event_id=allocation.event_id,
            is_new_event=True,
            score=score,
            reason=f"{reason} [identity:{allocation.basis}]",
            candidate_event_id=candidate_event_id,
            evidence=dict(evidence or {}),
            decided_by="deterministic",
        )

    def _reactivate(self, event_id: str, observation: Observation) -> None:
        """Restore an event that was closed or marked quiet to ``active``.

        Silence is not closure. An event that has been quiet long enough to be
        marked quiescent is still the event it was, and when it produces a new
        report that is news about a known situation - not grounds for inventing
        a second, identical-looking one. Reactivation is also what stops a
        long-running event from accumulating fragments: without it, every
        reappearance after a gap becomes a new id, and the platform ends up
        reconstructing five short lives instead of one long one.

        The prior status is preserved in the resolution reason and in the
        lifecycle table, so the quiet period stays visible in the history rather
        than being erased.
        """

        status = self.events.status_of(event_id)
        if status is None or status == "active":
            return
        self.events.set_status(
            event_id,
            "active",
            observation.observed_at,
            f"new report after {status}",
        )
        log.info("reactivated %s from %s", event_id, status)

    def _event_exists(self, event_id: str) -> bool:
        """Does the ledger already hold this id?

        Cached per resolution burst. Every event opened during the burst has to
        be visible to the next observation's allocation, otherwise a batch of
        observations sharing a fingerprint would each be handed ordinal 1.
        """

        if self._known_events_cache is None:
            self._known_events_cache = {row["event_id"] for row in self.events.all_events()}
        if event_id in self._known_events_cache:
            return True
        # ``all_events`` snapshots rows created before this burst; a
        # mid-burst allocation consults ``set_status``/``ensure`` afterwards, so
        # the cache has to be extended in step.
        if self.events.status_of(event_id) is not None:
            self._known_events_cache.add(event_id)
            return True
        return False

    def merge_events(self, source_event_id: str, target_event_id: str, reason: str) -> None:
        """Section 10: reversible merge with recorded history."""
        self.events.merge(source_event_id, target_event_id, reason)
        log.info("merged %s -> %s (%s)", source_event_id, target_event_id, reason)

    # -- candidate generation (cheap constraints first) --------------------

    def _candidates(self, observation: Observation) -> list[Candidate]:
        margin = self.time_window_min * 60
        window_start = observation.event_time - timedelta(seconds=margin)
        window_end = observation.event_time + timedelta(seconds=margin)

        reachable = self._reachable()
        candidate_ids = set(self.observations.event_ids_between(window_start, window_end))

        out: list[Candidate] = []
        checked: set[str] = set()

        for event_id in candidate_ids:
            if event_id not in reachable:
                continue
            checked.add(event_id)
            cand = self._build_candidate(event_id, observation)
            if cand is not None:
                out.append(cand)

        # Also inspect reachable active events within temporal and spatial reach
        for event_id in reachable:
            if event_id in checked:
                continue
            cand = self._build_candidate(event_id, observation)
            if cand is not None:
                out.append(cand)

        return out

    def _build_candidate(self, event_id: str, observation: Observation) -> Candidate | None:
        obs = self.observations.list_for_event(event_id)
        if not obs:
            return None
        if not self._temporally_compatible(observation, obs):
            return None
        candidate = Candidate(
            event_id,
            self._latest_state(event_id),
            obs,
            self.claims_repo.claims_for_event(event_id),
        )
        if observation.geometry and candidate.geometry:
            if distance_m(observation.geometry, candidate.geometry) > self.search_radius_m:
                return None
        return candidate

    def _reachable(self) -> set[str]:
        """Events that could receive a new observation.

        ``active_events()`` is a query and the set only changes when an event is
        opened or closed, so caching it for the duration of one resolve call is
        safe and removes the last full-table read from the hot path.

        Closed events are included on purpose. Closure means "we are no longer
        expecting reports", not "no report may ever arrive again" - a bridge fire
        is closed and then reopens because someone calls it in. If closed events
        were excluded outright, each reappearance would be forced into a fresh
        id, and a single long-running situation would be reconstructed as a
        series of unrelated short ones with no way to tell them apart after the
        fact. The cost is bounded by the ``closed`` status filter below, which
        keeps events closed recently out of the hot path and lets genuinely stale
        ones back in as the gap grows.
        """

        if self._active_cache is None:
            self._active_cache = set(self.events.active_events())
            self._active_cache |= self._recoverable()
        return self._active_cache

    def _recoverable(self) -> set[str]:
        """Closed events that a new report is allowed to reopen."""
        rows = self.events.all_events()
        closed = [r for r in rows if r.get("status") == "closed"][:_RECOVERY_CLOSED_LIMIT]
        out: set[str] = set()
        for row in closed:
            event_id = str(row["event_id"])
            latest = self.lifecycle.latest(event_id) if self.lifecycle else None
            if latest is None or latest.termination_basis not in _DEFINITIVE_CLOSURES:
                out.add(event_id)
        return out

    def _temporally_compatible(self, observation: Observation, siblings: Sequence[Observation]) -> bool:
        if not siblings:
            return True
        times = [o.event_time for o in siblings]
        nearest = min(abs((observation.event_time - t).total_seconds()) for t in times)
        margin = self.time_window_min * 60
        return nearest <= margin

    # -- scoring ----------------------------------------------------------

    def score(
        self,
        observation: Observation,
        candidate: Candidate,
        existing_claims: Sequence[Claim] = (),
    ) -> Resolution:
        evidence: dict[str, float] = {}

        # 1. shared entities / lexical overlap (computed first for location cues)
        entities = self._entity_overlap(observation, candidate, existing_claims)
        evidence["entity_overlap"] = entities

        # 2. spatial overlap / proximity
        spatial = self._spatial(observation, candidate)
        if (not observation.geometry or not candidate.geometry) and entities > 0.0:
            spatial = max(spatial, min(0.85, entities * 1.5))
        evidence["spatial"] = spatial

        # 3. temporal distance
        temporal = self._temporal(observation, candidate)
        evidence["temporal"] = temporal

        # 4. event-type compatibility
        compatibility = self._type_compatibility(observation, candidate)
        evidence["type_compatibility"] = compatibility

        # 5. claim continuity (does the observation continue an existing claim?)
        continuity = self._claim_continuity(observation, candidate, existing_claims)
        evidence["claim_continuity"] = continuity

        # 6. movement compatibility
        movement = self._movement_compatibility(observation, candidate)
        evidence["movement_compatibility"] = movement

        # Weights favour hard spatial/temporal evidence over soft textual cues.
        score = (
            0.30 * spatial
            + 0.20 * temporal
            + 0.15 * compatibility
            + 0.15 * entities
            + 0.12 * continuity
            + 0.08 * movement
        )

        strongest = max(evidence.items(), key=lambda kv: kv[1])
        return Resolution(
            event_id=candidate.event_id,
            is_new_event=False,
            score=score,
            reason=f"strongest signal: {strongest[0]}={strongest[1]:.2f}",
            candidate_event_id=candidate.event_id,
            evidence=evidence,
        )

    def _spatial(self, observation: Observation, candidate: Candidate) -> float:
        if not observation.geometry or not candidate.geometry:
            return 0.45 if observation.provenance.authority.value == "official" else 0.25
        d = distance_m(observation.geometry, candidate.geometry)
        if d == 0:
            return 1.0
        # Full credit inside 250 m, decaying to 0 at the search radius.
        return max(0.0, 1.0 - (d / self.search_radius_m))

    def _temporal(self, observation: Observation, candidate: Candidate) -> float:
        times = [o.event_time for o in candidate.observations]
        if not times:
            return 0.5
        nearest = min(abs((observation.event_time - t).total_seconds()) for t in times)
        return max(0.0, 1.0 - (nearest / (self.time_window_min * 60)))

    def _type_compatibility(self, observation: Observation, candidate: Candidate) -> float:
        obs_type = observation.observation_type
        affinity = TYPE_AFFINITY.get(obs_type, 0.2)
        state_types = candidate.observation_types
        if obs_type.value in state_types:
            return min(1.0, affinity + 0.4)
        if obs_type.value in INCIDENT_COMPATIBLE_TYPES and any(t in INCIDENT_COMPATIBLE_TYPES for t in state_types):
            return min(1.0, affinity + 0.3)
        return affinity

    def _has_corroboration(self, evidence: dict[str, float]) -> bool:
        """Is there positive evidence that two records describe one situation?"""
        is_spatiotemporal = (
            evidence.get("spatial", 0.0) >= 0.80
            and evidence.get("temporal", 0.0) >= 0.70
            and evidence.get("type_compatibility", 0.0) >= 0.40
        )
        has_entities = evidence.get("entity_overlap", 0.0) >= 0.40
        has_claims = evidence.get("claim_continuity", 0.0) >= 0.50
        return is_spatiotemporal or has_entities or has_claims

    def _entity_overlap(
        self, observation: Observation, candidate: Candidate, existing_claims: Sequence[Claim]
    ) -> float:
        text = _tokens(f"{observation.headline} {observation.structured_payload}")
        if not text:
            return 0.0
        candidate_text: set[str] = set()
        for obs in candidate.observations:
            candidate_text |= _tokens(f"{obs.headline} {obs.structured_payload}")
        for claim in candidate.claims:
            if claim.predicate in ("location.name", "road.affected", "incident.id") and claim.value:
                candidate_text |= _tokens(str(claim.value))
        if not candidate_text:
            return 0.0
        overlap_tokens = text & candidate_text
        if not overlap_tokens:
            return 0.0
        has_strong_entity = any(
            t.startswith(("i_", "sr_", "us_", "exit_", "mp_")) for t in overlap_tokens
        )
        overlap = len(overlap_tokens) / max(1, len(text))
        if has_strong_entity:
            return min(1.0, max(0.60, overlap * 2.0))
        if len(overlap_tokens) >= 2 or overlap >= 0.30:
            return min(1.0, overlap * 2.0)
        return 0.0

    def _claim_continuity(
        self,
        observation: Observation,
        candidate: Candidate,
        existing_claims: Sequence[Claim] = (),
    ) -> float:
        incoming = existing_claims
        if not incoming and hasattr(self, "claim_extractor") and self.claim_extractor:
            incoming = self.claim_extractor.extract(observation, "")

        if not incoming or not candidate.claims:
            return 0.0
        incoming_pairs = {(c.predicate, _norm(c.value)) for c in incoming}
        prior_pairs = {(c.predicate, _norm(c.value)) for c in candidate.claims}
        shared = incoming_pairs & prior_pairs
        if not shared:
            return 0.15
        return min(1.0, 0.5 + 0.25 * len(shared))


    def _movement_compatibility(self, observation: Observation, candidate: Candidate) -> float:
        """Section 10: movement compatibility.

        A new location slightly offset in the direction the event was already
        moving is compatible; a location in the opposite direction is weaker.
        """
        if not observation.geometry or not candidate.geometry:
            return 0.4
        movement = (
            candidate.state.movement
            if candidate.state is not None
            else self._movement_of(candidate.observations)
        )
        if not movement.moving or movement.direction_deg is None:
            return 0.5

        a = centroid_of(candidate.geometry)
        b = centroid_of(observation.geometry)
        if a is None or b is None:
            return 0.4
        d = haversine_m(a, b)
        if d < 50:
            return 0.9
        bearing = _bearing(a, b)
        delta = abs((bearing - movement.direction_deg + 180) % 360 - 180)
        alignment = max(0.0, 1.0 - delta / 180.0)
        return round(0.35 + 0.55 * alignment, 4)

    @staticmethod
    def _movement_of(observations: Sequence[Observation]) -> MovementState:
        """Direction of travel from the linked observations.

        Needed because during a burst there is no reconstructed state to read
        movement from. Derived the same way ``WorldStateEngine._movement`` does
        it, for the same reason: the resolver must not reach a different
        conclusion about an event than the state engine will a moment later.
        """

        points = [
            (o.event_time, centroid_of(o.geometry))
            for o in observations
            if o.geometry and centroid_of(o.geometry)
        ]
        if len(points) < 2:
            return MovementState(moving=False)
        points.sort(key=lambda pair: pair[0])
        span = (points[-1][0] - points[0][0]).total_seconds()
        if span <= 0:
            return MovementState(moving=False)
        return MovementState(moving=True, direction_deg=_bearing(points[0][1], points[-1][1]))

    # -- helpers ----------------------------------------------------------

    def _latest_state(self, event_id: str) -> EventState | None:
        return self.states.latest(event_id)

    def _new_event_id(self, observation: Observation) -> str:
        """Deprecated. Identity now lives in :class:`EventIdentityAllocator`.

        Kept only because tests reached for it directly. It is the bug: an id
        derived from region + type + calendar day is shared by every unrelated
        event sharing those three attributes, which is not a rare edge case but
        the normal case for any region with more than one thing happening in it.
        """

        log.warning(
            "_new_event_id is deprecated and reproduces the identity collision "
            "bug; use _open_event so identity is allocated properly"
        )
        day = observation.event_time.strftime("%Y%m%d")
        return deterministic_id("evt", self.region_id, observation.observation_type, day)[:24]

    def _candidate_summary(self, observation: Observation, claims: Sequence[Claim]) -> dict[str, Any]:
        return {
            "observation_type": str(observation.observation_type),
            "headline": observation.headline,
            "predicates": sorted({c.predicate for c in claims}),
        }

    def _candidate_summary_obs(self, observation: Observation, candidate: Candidate) -> dict[str, Any]:
        return {
            "event_id": candidate.event_id,
            "observation_type": str(observation.observation_type),
            "state_type_distribution": (
                candidate.state.event_type_distribution
                if candidate.state is not None
                else dict.fromkeys(sorted(candidate.observation_types), 1.0 / max(1, len(candidate.observations)))
            ),
            "predicates": sorted({c.predicate for c in candidate.claims}),
            "first_observed": (
                candidate.first_observed.isoformat() if candidate.first_observed else None
            ),
        }


def _canonicalize_text(raw: str) -> str:
    s = str(raw).lower()
    s = re.sub(r"\b(?:interstate\s*|i-?)(5|90|405)\b", r"i_\1", s)
    s = re.sub(r"\b(?:state\s*route\s*|sr-?|wa-?)(99|520|522|167|16|2)\b", r"sr_\1", s)
    s = re.sub(r"\b(?:u\.?s\.?\s*route\s*|us-?)(2|101)\b", r"us_\1", s)
    s = re.sub(r"\bexit\s*#?\s*(\d+[a-z]?)\b", r"exit_\1", s)
    s = re.sub(r"\bmilepost\s*#?\s*(\d+)\b", r"mp_\1", s)
    return s


def _tokens(text: str) -> set[str]:
    clean = _canonicalize_text(text)
    words = re.findall(r"[a-z0-9_]+", clean)
    tokens: set[str] = set()
    for w in words:
        w_canon = SYNONYMS.get(w, w)
        if w_canon in _STOPWORDS:
            continue
        if w_canon.startswith(("i_", "sr_", "us_", "exit_", "mp_")) or len(w_canon) >= 3:
            tokens.add(w_canon)
    return tokens



def _norm(value: Any) -> str:
    try:
        return str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return repr(value)


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlon = math.radians(b[0] - a[0])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


@dataclass
class ObservationCluster:
    cluster_id: int
    observations: list[Observation]
    centroid: tuple[float, float] | None
    earliest: Any
    latest: Any


def cluster_unresolved_observations(
    observations: Sequence[Observation],
    eps_m: float = 1500.0,
    time_window_min: float = 120.0,
    min_samples: int = 1,
) -> list[ObservationCluster]:
    """Cluster raw/unresolved observations into candidate spatiotemporal event groups using scikit-learn DBSCAN.

    Uses DBSCAN with the haversine metric on geographic coordinates for spatial proximity,
    bounded by the time window constraint. Singletons with min_samples=1 form their own clusters;
    isolated noise points (label -1) are placed in individual clusters.
    """
    if not observations:
        return []

    from collections import defaultdict
    import numpy as np
    from sklearn.cluster import DBSCAN
    from ..domain.geo import centroid_of

    valid_obs: list[tuple[Observation, tuple[float, float], float]] = []
    no_geom_obs: list[Observation] = []

    for obs in observations:
        c = centroid_of(obs.geometry) if obs.geometry else None
        if c:
            t_sec = obs.event_time.timestamp()
            valid_obs.append((obs, c, t_sec))
        else:
            no_geom_obs.append(obs)

    if not valid_obs:
        return [
            ObservationCluster(
                cluster_id=i,
                observations=[o],
                centroid=None,
                earliest=o.event_time,
                latest=o.event_time,
            )
            for i, o in enumerate(no_geom_obs)
        ]

    earth_radius_m = 6_371_008.8
    eps_rad = eps_m / earth_radius_m

    # [lat_rad, lon_rad] for haversine
    coords_rad = np.radians([[c[1], c[0]] for _, c, _ in valid_obs])

    db = DBSCAN(eps=eps_rad, min_samples=min_samples, metric="haversine")
    labels = db.fit_predict(coords_rad)

    clusters_map: dict[int, list[tuple[Observation, tuple[float, float], float]]] = defaultdict(list)
    noise_items: list[tuple[Observation, tuple[float, float], float]] = []

    for label, item in zip(labels, valid_obs, strict=False):
        if label == -1:
            noise_items.append(item)
        else:
            clusters_map[label].append(item)

    result_clusters: list[ObservationCluster] = []
    cluster_idx = 0

    for label, items in clusters_map.items():
        items.sort(key=lambda it: it[2])
        current_sub: list[tuple[Observation, tuple[float, float], float]] = [items[0]]

        for next_item in items[1:]:
            time_gap_min = (next_item[2] - current_sub[-1][2]) / 60.0
            if time_gap_min <= time_window_min:
                current_sub.append(next_item)
            else:
                avg_lon = sum(it[1][0] for it in current_sub) / len(current_sub)
                avg_lat = sum(it[1][1] for it in current_sub) / len(current_sub)
                sub_obs = [it[0] for it in current_sub]
                result_clusters.append(
                    ObservationCluster(
                        cluster_id=cluster_idx,
                        observations=sub_obs,
                        centroid=(round(avg_lon, 6), round(avg_lat, 6)),
                        earliest=min(o.event_time for o in sub_obs),
                        latest=max(o.event_time for o in sub_obs),
                    )
                )
                cluster_idx += 1
                current_sub = [next_item]

        if current_sub:
            avg_lon = sum(it[1][0] for it in current_sub) / len(current_sub)
            avg_lat = sum(it[1][1] for it in current_sub) / len(current_sub)
            sub_obs = [it[0] for it in current_sub]
            result_clusters.append(
                ObservationCluster(
                    cluster_id=cluster_idx,
                    observations=sub_obs,
                    centroid=(round(avg_lon, 6), round(avg_lat, 6)),
                    earliest=min(o.event_time for o in sub_obs),
                    latest=max(o.event_time for o in sub_obs),
                )
            )
            cluster_idx += 1

    for item in noise_items:
        o, c, _ = item
        result_clusters.append(
            ObservationCluster(
                cluster_id=cluster_idx,
                observations=[o],
                centroid=c,
                earliest=o.event_time,
                latest=o.event_time,
            )
        )
        cluster_idx += 1

    for o in no_geom_obs:
        result_clusters.append(
            ObservationCluster(
                cluster_id=cluster_idx,
                observations=[o],
                centroid=None,
                earliest=o.event_time,
                latest=o.event_time,
            )
        )
        cluster_idx += 1

    return result_clusters


__all__ = [
    "EVENT_BEARING_TYPES",
    "Candidate",
    "EventResolver",
    "ObservationCluster",
    "Resolution",
    "cluster_unresolved_observations",
    "new_id",
    "utcnow",
    "InfrastructureDomain",
]
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
from ..domain.geo import distance_m, haversine_m
from ..domain.ids import deterministic_id, new_id, utcnow
from ..domain.schemas import Claim, EventState, Observation
from ..llm.client import LlmClient, NullLlmClient
from ..storage.repository import EventRepository, ObservationRepository

log = logging.getLogger(__name__)

#: Observation types that imply the presence of a real-world event.
EVENT_BEARING_TYPES = frozenset(
    {
        ObservationType.PERMIT_EVENT,
        ObservationType.PUBLIC_GATHERING_REPORT,
        ObservationType.NEWS_ARTICLE,
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

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "is",
        "are", "was", "were", "has", "have", "with", "by", "from", "as", "it",
        "police", "reported", "reports", "seattle", "downtown",
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
    event_id: str
    state: EventState
    observations: list[Observation]
    claims: list[Claim]


class EventResolver:
    def __init__(
        self,
        events: EventRepository,
        observations: ObservationRepository,
        claims,
        states,
        region_id: str,
        *,
        time_window_min: float = 180.0,
        search_radius_m: float = 2500.0,
        merge_threshold: float = 0.62,
        llm: LlmClient | None = None,
    ) -> None:
        self.events = events
        self.observations = observations
        self.claims_repo = claims
        self.states = states
        self.region_id = region_id
        self.time_window_min = time_window_min
        self.search_radius_m = search_radius_m
        self.merge_threshold = merge_threshold
        self.llm = llm or NullLlmClient()
        #: Scoped to a single :meth:`resolve` call and cleared on the way out,
        #: so it can never go stale across a burst of observations that opens new
        #: events. An instance attribute only because ``_candidates`` is reached
        #: through the scoring helpers; a stale copy would silently stop
        #: resolving to events created moments earlier.
        self._active_cache: set[str] | None = None

    # -- public API -------------------------------------------------------

    def resolve(
        self,
        observation: Observation,
        existing_claims: Sequence[Claim] = (),
    ) -> Resolution:
        self._active_cache = None
        try:
            return self._resolve(observation, existing_claims)
        finally:
            self._active_cache = None

    def _resolve(
        self,
        observation: Observation,
        existing_claims: Sequence[Claim] = (),
    ) -> Resolution:
        candidates = self._candidates(observation)

        if not candidates:
            event_id = self._new_event_id(observation)
            self.events.ensure(event_id, observation.event_time, self.region_id)
            self.events.link_observation(event_id, observation.observation_id)
            return Resolution(
                event_id=event_id,
                is_new_event=True,
                score=1.0,
                reason="no candidate within temporal/geographic constraints",
                decided_by="deterministic",
            )

        scored = [(self.score(observation, c, existing_claims), c) for c in candidates]
        scored.sort(key=lambda pair: pair[0].score, reverse=True)
        best_score, best = scored[0]

        if best_score.score < self.merge_threshold:
            event_id = self._new_event_id(observation)
            self.events.ensure(event_id, observation.event_time, self.region_id)
            self.events.link_observation(event_id, observation.observation_id)
            return Resolution(
                event_id=event_id,
                is_new_event=True,
                score=best_score.score,
                reason=(
                    f"best candidate {best.event_id} scored {best_score.score:.2f} "
                    f"< threshold {self.merge_threshold:.2f} ({best_score.reason})"
                ),
                candidate_event_id=best.event_id,
                evidence=best_score.evidence,
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
        return Resolution(
            event_id=best.event_id,
            is_new_event=False,
            score=best_score.score,
            reason=best_score.reason,
            candidate_event_id=best.event_id,
            evidence=best_score.evidence,
            decided_by=decided_by,
        )

    def merge_events(self, source_event_id: str, target_event_id: str, reason: str) -> None:
        """Section 10: reversible merge with recorded history."""
        self.events.merge(source_event_id, target_event_id, reason)
        log.info("merged %s -> %s (%s)", source_event_id, target_event_id, reason)

    # -- candidate generation (cheap constraints first) --------------------

    def _candidates(self, observation: Observation) -> list[Candidate]:
        # The time window, applied in SQL. This is the constraint the module
        # docstring promises as the first cheap deterministic filter, and it was
        # being computed and thrown away (``_ = window_start``) while the scan
        # below walked every active event. See
        # :meth:`ObservationRepository.event_ids_between` for the arithmetic:
        # per-observation resolution against a full scan is O(observations x
        # events) round-trips, and the supplied feeds produced roughly twelve
        # million of them on a cold ingest.
        margin = self.time_window_min * 60
        window_start = observation.event_time - timedelta(seconds=margin)
        window_end = observation.event_time + timedelta(seconds=margin)

        out: list[Candidate] = []
        for event_id in self.observations.event_ids_between(window_start, window_end):
            if event_id not in self._active_now():
                continue
            state = self._latest_state(event_id)
            if state is None:
                continue
            obs = self.observations.list_for_event(event_id)
            if not obs:
                continue
            if not self._temporally_compatible(observation, obs):
                continue
            if observation.geometry and state.geometry:
                if distance_m(observation.geometry, state.geometry) > self.search_radius_m:
                    continue
            out.append(Candidate(event_id, state, obs, self.claims_repo.claims_for_event(event_id)))
        return out

    def _active_now(self) -> set[str]:
        """Active event ids, refreshed at most once per resolution burst.

        ``active_events()`` is itself a query, and the set only changes when an
        event is opened or closed, so caching it for the duration of one
        resolve call is safe and removes the last full-table read from the hot
        path.
        """

        if self._active_cache is None:
            self._active_cache = set(self.events.active_events())
        return self._active_cache

    def _temporally_compatible(self, observation: Observation, siblings: Sequence[Observation]) -> bool:
        if not siblings:
            return True
        times = [o.event_time for o in siblings]
        earliest, latest = min(times), max(times)
        margin = self.time_window_min * 60
        return (
            (observation.event_time - earliest).total_seconds() <= margin
            and (latest - observation.event_time).total_seconds() <= margin
        )

    # -- scoring ----------------------------------------------------------

    def score(
        self,
        observation: Observation,
        candidate: Candidate,
        existing_claims: Sequence[Claim] = (),
    ) -> Resolution:
        evidence: dict[str, float] = {}

        # 1. spatial overlap / proximity
        spatial = self._spatial(observation, candidate)
        evidence["spatial"] = spatial

        # 2. temporal distance
        temporal = self._temporal(observation, candidate)
        evidence["temporal"] = temporal

        # 3. event-type compatibility
        compatibility = self._type_compatibility(observation, candidate)
        evidence["type_compatibility"] = compatibility

        # 4. shared entities / lexical overlap
        entities = self._entity_overlap(observation, candidate, existing_claims)
        evidence["entity_overlap"] = entities

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
        if not observation.geometry or not candidate.state.geometry:
            # An official record without geometry should not be discarded; treat
            # missing geometry as weak-neutral rather than disqualifying.
            return 0.4 if observation.provenance.authority.value == "official" else 0.15
        d = distance_m(observation.geometry, candidate.state.geometry)
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
        state_types = set(candidate.state.evidence.observation_types)
        if obs_type.value in state_types:
            return min(1.0, affinity + 0.4)
        # Related-but-different types (a closure alongside a gathering) still
        # support association, just weakly.
        return affinity

    def _entity_overlap(
        self, observation: Observation, candidate: Candidate, existing_claims: Sequence[Claim]
    ) -> float:
        text = _tokens(f"{observation.headline} {observation.structured_payload}")
        if not text:
            return 0.2
        candidate_text: set[str] = set()
        for obs in candidate.observations:
            candidate_text |= _tokens(f"{obs.headline} {obs.structured_payload}")
        for claim in candidate.claims:
            if claim.predicate == "location.name" and claim.value:
                candidate_text |= _tokens(str(claim.value))
        if not candidate_text:
            return 0.2
        overlap = len(text & candidate_text) / max(1, len(text))
        return min(1.0, overlap * 2.0)

    def _claim_continuity(
        self,
        observation: Observation,
        candidate: Candidate,
        existing_claims: Sequence[Claim] = (),
    ) -> float:
        """Strong signal: two records asserting the same predicate+value.

        This is what links "police report the event moved north" to the permit
        event, without relying on prose similarity.
        """
        _ = observation
        if not existing_claims or not candidate.claims:
            return 0.2
        incoming = {(c.predicate, _norm(c.value)) for c in existing_claims}
        prior = {(c.predicate, _norm(c.value)) for c in candidate.claims}
        shared = incoming & prior
        if not shared:
            return 0.15
        return min(1.0, 0.5 + 0.2 * len(shared))

    def _movement_compatibility(self, observation: Observation, candidate: Candidate) -> float:
        """Section 10: movement compatibility.

        A new location slightly offset in the direction the event was already
        moving is compatible; a location in the opposite direction is weaker.
        """
        if not observation.geometry or not candidate.state.geometry:
            return 0.4
        movement = candidate.state.movement
        if not movement.moving or movement.direction_deg is None:
            return 0.5
        from ..domain.geo import centroid_of

        a = centroid_of(candidate.state.geometry)
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

    # -- helpers ----------------------------------------------------------

    def _latest_state(self, event_id: str) -> EventState | None:
        return self.states.latest(event_id)

    def _new_event_id(self, observation: Observation) -> str:
        """Deterministic-per-day id keeps replay reproducible."""
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
            "state_type_distribution": candidate.state.event_type_distribution,
            "predicates": sorted({c.predicate for c in candidate.claims}),
            "first_observed": candidate.state.first_observed.isoformat()
            if candidate.state.first_observed
            else None,
        }


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", str(text).lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


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


__all__ = [
    "EVENT_BEARING_TYPES",
    "Candidate",
    "EventResolver",
    "Resolution",
    "new_id",
    "utcnow",
    "InfrastructureDomain",
]
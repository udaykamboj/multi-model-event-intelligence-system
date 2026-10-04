"""World state engine (brief section 12).

An event is not a database row. It is an evolving state reconstructed from the
observation ledger. Every material update appends a new state version; nothing
is ever overwritten, so the full trajectory remains available for training and
replay.

The engine has no notion of "a protest". It reads claims, and whatever the
claims assert becomes state. A quiet demonstration that never touches
transportation simply produces a small state - and that is a valid outcome.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any, Iterable, Sequence

from ..domain.enums import (
    Authority,
    InfrastructureDomain,
    ObservationType,
    TruthStatus,
    Urgency,
)
from ..domain.geo import centroid_of, distance_m, geometry_area_m2, haversine_m, union_bbox
from ..domain.ids import utcnow
from ..domain.schemas import (
    AffectedInfrastructure,
    Claim,
    EventState,
    EvidenceSummary,
    MovementState,
    Observation,
)
from ..storage.repository import StateRepository

log = logging.getLogger(__name__)

#: Observation type -> infrastructure domain it evidences.
DOMAIN_BY_TYPE: dict[ObservationType, InfrastructureDomain] = {
    ObservationType.ROAD_CLOSURE: InfrastructureDomain.ROAD,
    ObservationType.ROAD_CONSTRUCTION: InfrastructureDomain.ROAD,
    ObservationType.TRAFFIC_CONDITION: InfrastructureDomain.ROAD,
    ObservationType.TRAFFIC_FLOW: InfrastructureDomain.ROAD,
    ObservationType.TRAVEL_TIME: InfrastructureDomain.ROAD,
    ObservationType.BRIDGE_RESTRICTION: InfrastructureDomain.ROAD,
    ObservationType.TRANSIT_SERVICE_ALERT: InfrastructureDomain.TRANSIT,
    ObservationType.TRANSIT_DELAY: InfrastructureDomain.TRANSIT,
    ObservationType.VEHICLE_POSITION: InfrastructureDomain.TRANSIT,
    ObservationType.POWER_OUTAGE: InfrastructureDomain.UTILITY,
    ObservationType.WATER_OUTAGE: InfrastructureDomain.UTILITY,
    ObservationType.POLICE_RESPONSE: InfrastructureDomain.PUBLIC_SAFETY,
    ObservationType.FIRE_DISPATCH: InfrastructureDomain.PUBLIC_SAFETY,
    ObservationType.OFFICIAL_EMERGENCY_NOTICE: InfrastructureDomain.PUBLIC_SAFETY,
    ObservationType.FERRY_STATUS: InfrastructureDomain.FERRIES,
}

#: Severity ordering for infrastructure impacts.
_SEVERITY_ORDER = {
    Urgency.NONE: 0,
    Urgency.LOW: 1,
    Urgency.MODERATE: 2,
    Urgency.HIGH: 3,
    Urgency.IMMEDIATE: 4,
}


class WorldStateEngine:
    """Rebuilds ``EventState`` from observations + claims."""

    def __init__(self, states: StateRepository) -> None:
        self.states = states

    def rebuild(
        self,
        event_id: str,
        observations: Sequence[Observation],
        claims: Sequence[Claim],
        previous: EventState | None = None,
        now=None,
    ) -> EventState:
        """Reconstruct the state for one event.

        Pure and deterministic: same observations plus same previous state gives
        the same result, with no clock outside ``now`` and no network. That is
        section 33's replayability guarantee, and it is why no language model is
        consulted anywhere in this module - the section-18 narrative lives on the
        :class:`~infraimpact.domain.schemas.AnalysisRun` instead.
        """

        now = now or utcnow()

        ordered = sorted(observations, key=lambda o: (o.event_time, o.observation_id))
        first = ordered[0].event_time if ordered else now
        last = ordered[-1].observed_at if ordered else now

        geometry, geometry_confidence = self._footprint(ordered)
        movement = self._movement(ordered, previous)
        affected = self._affected_infrastructure(ordered)
        evidence = self._evidence(ordered, claims, affected, now)
        status = self._status(ordered, now)
        distribution = self._type_distribution(ordered)
        derived = self._derived(geometry, ordered, movement, affected, evidence)

        return EventState(
            event_id=event_id,
            state_version=(previous.state_version + 1) if previous else 1,
            event_type_distribution=distribution,
            status=status,
            geometry=geometry,
            geometry_confidence=geometry_confidence,
            first_observed=first,
            last_observed=last,
            reconstructed_at=now,
            movement=movement,
            affected_infrastructure=tuple(affected),
            evidence=evidence,
            observation_ids=tuple(o.observation_id for o in ordered),
            claim_ids=tuple(c.claim_id for c in claims),
            derived=derived,
        )

    def persist(self, state: EventState) -> None:
        self.states.append_state(state)

    # -- components -------------------------------------------------------

    def _footprint(self, observations: Sequence[Observation]) -> tuple[dict[str, Any] | None, float]:
        """Union of observed geometries, weighted by spatial precision.

        Note this is a bounding footprint, not a claim about crowd shape. The
        distinction matters: it is an upper bound on where the event has been
        *observed*, and it never shrinks silently because a later report is
        narrower.
        """
        geoms = [
            (o.geometry, o.quality.spatial_precision)
            for o in observations
            if o.geometry
        ]
        if not geoms:
            return None, 0.0
        bbox = union_bbox([g for g, _ in geoms])
        if bbox is None:
            return None, 0.0
        from ..domain.geo import bbox_polygon

        precision = sum(p for _, p in geoms) / len(geoms)
        count_factor = min(1.0, len(geoms) / 3.0)
        confidence = round(min(1.0, 0.5 * precision + 0.5 * count_factor), 4)
        return bbox_polygon(bbox), confidence

    def _movement(self, observations: Sequence[Observation], previous: EventState | None) -> MovementState:
        points = [
            (o.event_time, centroid_of(o.geometry))
            for o in observations
            if o.geometry and centroid_of(o.geometry)
        ]
        if len(points) < 2:
            if previous and previous.movement.moving:
                return previous.movement
            return MovementState()

        points.sort(key=lambda p: p[0])
        (t0, a), (t1, b) = points[0], points[-1]
        distance = haversine_m(a, b)  # type: ignore[arg-type]
        minutes = max((t1 - t0).total_seconds() / 60.0, 0.5)
        speed = distance / minutes  # metres per minute

        # Threshold scaled by observation interval: two reports 20 minutes apart
        # need more displacement to count as movement than two reports a minute
        # apart.
        threshold = max(50.0, minutes * 10.0)
        moving = distance > threshold

        bearing = None
        if moving:
            from .resolver import _bearing

            bearing = _bearing(a, b)  # type: ignore[arg-type]

        confidence = 0.0 if not moving else round(min(0.9, 0.4 + 0.5 * min(1.0, distance / 1500.0)), 4)
        return MovementState(
            moving=moving,
            direction_deg=round(bearing, 1) if bearing is not None else None,
            speed_estimate_m_per_min=round(speed, 2),
            confidence=confidence,
        )

    def _affected_infrastructure(self, observations: Sequence[Observation]) -> list[AffectedInfrastructure]:
        """Aggregate observations into currently-affected infrastructure.

        Reopen/restore records must retire the earlier closure, otherwise a
        resolved road stays "affected" forever.
        """
        live: dict[str, AffectedInfrastructure] = {}
        for obs in sorted(observations, key=lambda o: (o.event_time, o.observation_id)):
            domain = DOMAIN_BY_TYPE.get(obs.observation_type)
            if domain is None:
                continue
            key = self._infra_key(obs, domain)
            current = live.get(key)

            resolved = self._is_resolution(obs)
            if resolved and current is not None:
                current.observation_ids = current.observation_ids + (obs.observation_id,)
                current.severity = Urgency.LOW
                continue

            severity = self._severity(obs)
            truth = (
                TruthStatus.CONFIRMED
                if obs.provenance.authority is Authority.OFFICIAL
                else TruthStatus.REPORTED
            )
            if current is None:
                live[key] = AffectedInfrastructure(
                    domain=domain,
                    identifier=key,
                    name=self._name(obs),
                    geometry=obs.geometry,
                    severity=severity,
                    truth_status=truth,
                    observation_ids=(obs.observation_id,),
                    # "Detected" is when *the platform* first saw the asset
                    # affected, so the anchor is ``observed_at`` and not
                    # ``event_time``. The two are far apart in exactly the case
                    # that matters here: a replayed snapshot carries the
                    # original event time but is ingested long afterwards, and
                    # anchoring on the event time would claim the platform had
                    # been tracking the closure since it happened.
                    detected_at=obs.observed_at or obs.event_time,
                )
            else:
                bumped = _max_severity(current.severity, severity)
                live[key] = current.model_copy(
                    update={
                        "severity": bumped,
                        "observation_ids": current.observation_ids + (obs.observation_id,),
                        "geometry": obs.geometry or current.geometry,
                        # ``detected_at`` is deliberately *not* updated here. It
                        # is a first-detection stamp, and seeing the same asset
                        # affected again is not a new detection - it is
                        # corroboration, which ``observation_ids`` already
                        # records. An earlier version tried to refresh it from a
                        # non-existent ``Observation.detected_at`` and crashed
                        # every rebuild that saw the same asset twice.
                    }
                )
        return sorted(live.values(), key=lambda a: (-_SEVERITY_ORDER[a.severity], a.identifier))

    def _evidence(
        self,
        observations: Sequence[Observation],
        claims: Sequence[Claim],
        affected: Sequence[AffectedInfrastructure],
        now,
    ) -> EvidenceSummary:
        authorities = Counter(o.provenance.authority.value for o in observations)
        obs_types = Counter(str(o.observation_type) for o in observations)

        # Section 40: independent corroboration discounts dependent sources.
        from ..sources.dependency import SourceDependencyGraph

        corroboration = SourceDependencyGraph().analyze_corroboration(observations)
        independent = corroboration.independent_source_count

        contradictions = 0
        by_predicate: dict[str, set[str]] = defaultdict(set)
        for claim in claims:
            by_predicate[claim.predicate].add(str(claim.value))
        contradictions = sum(1 for values in by_predicate.values() if len(values) > 1)

        freshness = (
            (now - max(o.observed_at for o in observations)).total_seconds()
            if observations
            else None
        )

        vector = {
            "source_authority": _authority_score(authorities),
            "independent_corroboration": round(min(1.0, independent / 3.0), 4),
            "corroboration_score": corroboration.corroboration_score,
            "independence_ratio": corroboration.independence_ratio,
            "freshness": _freshness_score(freshness),
            "spatial_precision": round(
                sum(o.quality.spatial_precision for o in observations) / len(observations), 4
            )
            if observations
            else 0.0,
            "temporal_precision": round(
                sum(o.quality.temporal_precision for o in observations) / len(observations), 4
            )
            if observations
            else 0.0,
            "contradictions": round(min(1.0, contradictions / 3.0), 4),
            "extraction_confidence": round(
                sum(c.extraction_confidence for c in claims) / len(claims), 4
            )
            if claims
            else 0.0,
        }

        return EvidenceSummary(
            source_count=len({o.source_id for o in observations}),
            independent_source_count=independent,
            authorities=dict(authorities),
            observation_types=dict(obs_types),
            contradictions=contradictions,
            freshness_seconds=freshness,
            vector=vector,
        )

    def _status(self, observations: Sequence[Observation], now) -> str:
        if not observations:
            return "candidate"
        latest = max(o.observed_at for o in observations)
        idle = (now - latest).total_seconds()
        has_dispersal = any(
            "dispers" in (o.headline or "").lower()
            or "dispers" in str(o.structured_payload).lower()
            or "reopen" in str(o.structured_payload).lower()
            for o in observations
        )
        if idle > 3600 and has_dispersal:
            return "closed"
        if idle > 3600:
            return "quiescent"
        return "active"

    def _type_distribution(self, observations: Sequence[Observation]) -> dict[str, float]:
        """Soft classification over event types, not a single hard label."""
        if not observations:
            return {"unknown": 1.0}
        weights: dict[str, float] = defaultdict(float)
        for obs in observations:
            weights[str(obs.observation_type)] += obs.quality.composite
        total = sum(weights.values()) or 1.0
        distribution = {k: round(v / total, 4) for k, v in weights.items()}
        if "unknown" not in distribution:
            distribution["unknown"] = 0.0
        return dict(sorted(distribution.items(), key=lambda kv: -kv[1]))

    def _derived(
        self,
        geometry: dict[str, Any] | None,
        observations: Sequence[Observation],
        movement: MovementState,
        affected: Sequence[AffectedInfrastructure],
        evidence: EvidenceSummary | None = None,
    ) -> dict[str, float]:
        footprint = geometry_area_m2(geometry) if geometry else 0.0
        domains = {a.domain for a in affected}
        independent_sources = (
            float(evidence.independent_source_count)
            if evidence is not None
            else float(len({o.source_id for o in observations}))
        )
        return {
            "footprint_area_m2": round(footprint, 1),
            "footprint_radius_m": round((footprint / 3.14159) ** 0.5, 1) if footprint else 0.0,
            "observation_count": float(len(observations)),
            "independent_sources": independent_sources,
            "moving": 1.0 if movement.moving else 0.0,
            "movement_speed": movement.speed_estimate_m_per_min or 0.0,
            "affected_domains": float(len(domains)),
            "severity_peak": float(
                max((_SEVERITY_ORDER[a.severity] for a in affected), default=0)
            ),
            "is_official_guidance": 1.0
            if any(
                o.observation_type == ObservationType.OFFICIAL_EMERGENCY_NOTICE for o in observations
            )
            else 0.0,
        }

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _infra_key(obs: Observation, domain: InfrastructureDomain) -> str:
        p = obs.structured_payload
        for field in ("route", "street", "location", "route_id", "event_name", "area"):
            value = p.get(field)
            if value:
                return f"{domain.value}:{str(value).strip().lower()[:60]}"
        if obs.geometry:
            centre = centroid_of(obs.geometry)
            if centre:
                return f"{domain.value}:{centre[0]:.4f},{centre[1]:.4f}"
        return f"{domain.value}:{obs.source_id}"

    @staticmethod
    def _name(obs: Observation) -> str | None:
        p = obs.structured_payload
        for field in ("route_long", "street", "location", "event_name", "area_description", "headline"):
            value = p.get(field)
            if value:
                return str(value)[:120]
        return obs.headline[:120] or None

    @staticmethod
    def _severity(obs: Observation) -> Urgency:
        p = obs.structured_payload
        raw = str(p.get("severity") or "").lower()
        mapping = {
            "extreme": Urgency.IMMEDIATE,
            "severe": Urgency.HIGH,
            "immediate": Urgency.IMMEDIATE,
            "high": Urgency.HIGH,
            "moderate": Urgency.MODERATE,
            "minor": Urgency.LOW,
            "info": Urgency.LOW,
            "unknown": Urgency.LOW,
        }
        if raw in mapping:
            return mapping[raw]
        if obs.observation_type in {ObservationType.ROAD_CLOSURE, ObservationType.TRANSIT_SERVICE_ALERT}:
            return Urgency.HIGH
        if obs.observation_type in {ObservationType.OFFICIAL_EMERGENCY_NOTICE}:
            return Urgency.IMMEDIATE
        if obs.observation_type in {ObservationType.POLICE_RESPONSE, ObservationType.FIRE_DISPATCH}:
            return Urgency.MODERATE
        return Urgency.LOW

    @staticmethod
    def _is_resolution(obs: Observation) -> bool:
        """Does this record retire a previously reported disruption?"""
        p = obs.structured_payload
        for field in ("closure_type", "alert_type", "status", "outage_type"):
            value = str(p.get(field) or "").lower()
            if any(token in value for token in ("reopen", "restored", "resumed", "cleared", "resolved")):
                return True
        headline = (obs.headline or "").lower()
        return any(token in headline for token in ("reopened", "restored", "resumed", "cleared"))


def _max_severity(a: Urgency, b: Urgency) -> Urgency:
    return a if _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


def _independent_source_count(observations: Iterable[Observation]) -> int:
    """Section 40: syndication is not corroboration.

    Sources agreeing within a short window on an identical normalised payload
    are treated as one origin, not N confirmations.
    """
    by_fingerprint: dict[str, set[str]] = defaultdict(set)
    for obs in observations:
        fingerprint = obs.provenance.content_hash[:16]
        by_fingerprint[fingerprint].add(obs.source_id)
    # Official sources are authoritative and independent by definition; media
    # is discounted toward its syndication cluster size.
    official = {
        o.source_id
        for o in observations
        if o.provenance.authority is Authority.OFFICIAL
    }
    clusters = {frozenset(v) for v in by_fingerprint.values()}
    media = {s for c in clusters for s in c} - official
    return len(official) + min(len(media), max(1, len(clusters)))


def _authority_score(authorities: Counter) -> float:
    if not authorities:
        return 0.0
    weights = {
        Authority.OFFICIAL.value: 1.0,
        Authority.SEMI_OFFICIAL.value: 0.8,
        Authority.ESTABLISHED_MEDIA.value: 0.6,
        Authority.COMMUNITY.value: 0.3,
        Authority.UNVERIFIED.value: 0.1,
        Authority.INTERNAL.value: 0.5,
    }
    total = sum(weights.get(a, 0.2) for a in authorities.elements())
    return round(min(1.0, total / max(1.0, len(authorities) * 1.5)), 4)


def _freshness_score(freshness: float | None) -> float:
    if freshness is None:
        return 0.0
    if freshness <= 300:
        return 1.0
    if freshness >= 7200:
        return 0.0
    return round(max(0.0, 1.0 - (freshness - 300) / 6900.0), 4)


__all__ = ["DOMAIN_BY_TYPE", "WorldStateEngine", "distance_m", "timedelta"]
"""State delta engine (brief sections 32, 33, 54).

Every analysis produces two things: the absolute state, and the delta from the
previous state. This module computes the second.

The important behaviour is *restraint*. A news article saying "demonstration
continues downtown" is stored, appended to the ledger, and produces **no**
material delta - so nothing downstream is recomputed and nobody is notified. An
arterial closure produces a material delta, which triggers re-evaluation of
impacted users and then alert evaluation.

The observation is never discarded. It is suppressed from *notification*, not
from *history*. That distinction is the whole point of section 33.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.enums import TruthStatus, Urgency
from ..domain.geo import centroid_of, distance_m, geometry_area_m2
from ..domain.schemas import (
    AffectedInfrastructure,
    EventState,
    Forecast,
    StateDelta,
    UserExposure,
)

log = logging.getLogger(__name__)

#: Below this magnitude an update is treated as noise: archived and visible in
#: the timeline, never propagated to users.
MATERIALITY_FLOOR = 0.12

#: Geometry displacement (metres) that counts as a full-magnitude movement.
_MOVEMENT_SATURATION_M = 1200.0

_SEVERITY_RANK = {
    Urgency.NONE: 0,
    Urgency.LOW: 1,
    Urgency.MODERATE: 2,
    Urgency.HIGH: 3,
    Urgency.IMMEDIATE: 4,
}

#: Severity changes smaller than this many bands are not worth surfacing.
_SEVERITY_MATERIAL_GAP = 1

#: Forecast movement smaller than this is noise, not a change in belief.
_FORECAST_MATERIAL_GAP = 0.15


@dataclass
class DeltaReport:
    """Result of comparing two consecutive state versions."""

    deltas: tuple[StateDelta, ...] = ()
    material_deltas: tuple[StateDelta, ...] = ()
    causes: tuple[str, ...] = ()
    magnitude: float = 0.0
    novelty: float = 0.0
    confidence: float = 0.0
    is_material: bool = False
    suppressed_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def change_names(self) -> list[str]:
        return [d.change for d in self.deltas]

    def summary(self) -> dict[str, Any]:
        return {
            "is_material": self.is_material,
            "magnitude": round(self.magnitude, 4),
            "novelty": round(self.novelty, 4),
            "confidence": round(self.confidence, 4),
            "suppressed_reason": self.suppressed_reason,
            "changes": [
                {
                    "change": d.change,
                    "domain": d.domain,
                    "magnitude": round(d.magnitude, 4),
                    "urgency": str(d.urgency),
                    "before": d.before,
                    "after": d.after,
                }
                for d in self.deltas
            ],
            "causes": list(self.causes),
        }


class StateDeltaEngine:
    """Compares consecutive ``EventState`` versions.

    Stateless: both versions are handed in, which keeps the engine trivially
    replayable against historical versions (section 49).
    """

    def __init__(self, materiality_floor: float = MATERIALITY_FLOOR) -> None:
        self.materiality_floor = materiality_floor

    # -- public API -------------------------------------------------------

    def compare(
        self,
        previous: EventState | None,
        current: EventState,
        *,
        impacts: Sequence[AffectedInfrastructure] = (),
        forecasts: Sequence[Forecast] = (),
        previous_impacts: Sequence[AffectedInfrastructure] = (),
        previous_forecasts: Sequence[Forecast] = (),
        affected_user_count: int = 0,
    ) -> DeltaReport:
        report = DeltaReport()

        if previous is None:
            # First state for an event. Reported as one initialisation delta
            # rather than N separate changes, so a brand-new event does not
            # masquerade as an event that just changed dramatically.
            report.deltas = (
                StateDelta(
                    change="event_initialised",
                    domain="event",
                    before=None,
                    after=current.status,
                    magnitude=1.0,
                    confidence=current.geometry_confidence,
                    novelty=1.0,
                    causes=current.observation_ids[-8:],
                    urgency=_peak_urgency(impacts),
                    affected_user_count=affected_user_count,
                    official_guidance=has_official_guidance(current),
                ),
            )
            return self._finish(report)

        report.causes = self._new_observations(previous, current)

        deltas: list[StateDelta] = []
        deltas.extend(self._status(previous, current, report.causes))
        deltas.extend(self._geometry(previous, current, report.causes))
        deltas.extend(self._movement(previous, current, report.causes))
        deltas.extend(self._classification(previous, current, report.causes))
        deltas.extend(
            self._infrastructure(
                previous_impacts or previous.affected_infrastructure,
                impacts or current.affected_infrastructure,
                report.causes,
            )
        )
        deltas.extend(self._confidence(previous, current, report.causes))
        deltas.extend(self._forecasts(previous_forecasts, forecasts, report.causes))
        deltas.extend(self._official_guidance(previous, current, report.causes))

        report.deltas = tuple(deltas)
        return self._finish(report)

    # -- individual comparisons -------------------------------------------

    def _status(
        self, previous: EventState, current: EventState, causes: Sequence[str]
    ) -> list[StateDelta]:
        if previous.status == current.status:
            return []
        closed = current.status == "closed"
        return [
            StateDelta(
                change="event_resolved" if closed else "status_changed",
                domain="event",
                before=previous.status,
                after=current.status,
                magnitude=1.0 if closed else 0.55,
                confidence=0.9,
                novelty=1.0 if previous.status == "active" else 0.5,
                causes=tuple(causes),
                urgency=Urgency.NONE if closed else Urgency.MODERATE,
                official_guidance=has_official_guidance(current),
            )
        ]

    def _geometry(
        self, previous: EventState, current: EventState, causes: Sequence[str]
    ) -> list[StateDelta]:
        before_area = geometry_area_m2(previous.geometry)
        after_area = geometry_area_m2(current.geometry)
        displacement = (
            distance_m(previous.geometry, current.geometry)
            if previous.geometry and current.geometry
            else 0.0
        )

        # The footprint is a union of observed geometry, so it grows
        # monotonically as reports arrive. A modest expansion is therefore
        # ordinary reporting, not necessarily movement - both signals are
        # needed before calling this a change.
        growth = (
            max(0.0, (after_area - before_area) / before_area)
            if before_area > 0
            else (1.0 if after_area > 0 else 0.0)
        )
        shift_component = min(1.0, displacement / _MOVEMENT_SATURATION_M)
        if shift_component < 0.02 and growth < 0.05:
            return []

        magnitude = round(min(1.0, 0.6 * shift_component + 0.4 * min(1.0, growth)), 4)
        return [
            StateDelta(
                change=(
                    "event_geometry_displaced"
                    if shift_component >= growth
                    else "event_footprint_expanded"
                ),
                domain="event",
                before={
                    "centroid": _centroid(previous.geometry),
                    "area_m2": round(before_area, 1),
                },
                after={
                    "centroid": _centroid(current.geometry),
                    "area_m2": round(after_area, 1),
                },
                magnitude=magnitude,
                confidence=current.geometry_confidence,
                novelty=1.0,
                causes=tuple(causes),
                urgency=Urgency.MODERATE if magnitude > 0.3 else Urgency.LOW,
                official_guidance=has_official_guidance(current),
            )
        ]

    def _movement(
        self, previous: EventState, current: EventState, causes: Sequence[str]
    ) -> list[StateDelta]:
        a, b = previous.movement, current.movement
        if a.moving == b.moving and a.direction_deg == b.direction_deg:
            return []
        direction_change = (
            a.direction_deg is not None
            and b.direction_deg is not None
            and abs((a.direction_deg - b.direction_deg + 180) % 360 - 180) > 30
        )
        return [
            StateDelta(
                change=(
                    "event_direction_changed"
                    if direction_change
                    else ("movement_started" if b.moving else "movement_stopped")
                ),
                domain="event",
                before={"moving": a.moving, "direction_deg": a.direction_deg},
                after={"moving": b.moving, "direction_deg": b.direction_deg},
                magnitude=0.7 if b.moving else 0.35,
                confidence=b.confidence,
                novelty=1.0,
                causes=tuple(causes),
                urgency=Urgency.HIGH if b.moving else Urgency.LOW,
                official_guidance=has_official_guidance(current),
            )
        ]

    def _classification(
        self, previous: EventState, current: EventState, causes: Sequence[str]
    ) -> list[StateDelta]:
        before = dominant_label(previous.event_type_distribution)
        after = dominant_label(current.event_type_distribution)
        if before == after:
            return []
        confidence = current.event_type_distribution.get(after or "", 0.0)
        return [
            StateDelta(
                change="event_reclassified",
                domain="event",
                before=before,
                after=after,
                magnitude=round(min(1.0, confidence), 4),
                confidence=round(confidence, 4),
                # Reclassification that follows new observations is expected.
                # Reclassification with no new information signals that the
                # classifier is unstable, which is far more interesting.
                novelty=0.4 if causes else 0.9,
                causes=tuple(causes),
                urgency=Urgency.MODERATE,
                official_guidance=has_official_guidance(current),
            )
        ]

    def _infrastructure(
        self,
        before: Sequence[AffectedInfrastructure],
        after: Sequence[AffectedInfrastructure],
        causes: Sequence[str],
    ) -> list[StateDelta]:
        before_map = {i.identifier: i for i in before}
        after_map = {i.identifier: i for i in after}
        out: list[StateDelta] = []

        appeared = [i for k, i in after_map.items() if k not in before_map]
        resolved = [i for k, i in before_map.items() if k not in after_map]

        out.extend(
            self._appear_or_resolve(
                appeared, "impact_appeared", causes, escalating=True
            )
        )
        out.extend(
            self._appear_or_resolve(
                resolved, "impact_resolved", causes, escalating=False
            )
        )

        # Severity movement on something already known to be affected.
        for identifier, current in sorted(after_map.items()):
            prior = before_map.get(identifier)
            if prior is None:
                continue
            gap = _SEVERITY_RANK[current.severity] - _SEVERITY_RANK[prior.severity]
            if abs(gap) < _SEVERITY_MATERIAL_GAP:
                continue
            out.append(
                StateDelta(
                    change="impact_escalated" if gap > 0 else "impact_deescalated",
                    domain=str(current.domain),
                    before=str(prior.severity),
                    after=str(current.severity),
                    magnitude=round(min(1.0, 0.2 * abs(gap) + 0.1), 4),
                    confidence=truth_confidence([current]),
                    novelty=0.6,
                    causes=tuple(causes),
                    urgency=current.severity if gap > 0 else Urgency.LOW,
                    affected_user_count=0,
                )
            )
        return out

    def _appear_or_resolve(
        self,
        group: Sequence[AffectedInfrastructure],
        verb: str,
        causes: Sequence[str],
        *,
        escalating: bool,
    ) -> list[StateDelta]:
        if not group:
            return []
        by_domain: dict[str, list[AffectedInfrastructure]] = {}
        for impact in group:
            by_domain.setdefault(str(impact.domain), []).append(impact)

        out: list[StateDelta] = []
        for domain, impacts in sorted(by_domain.items()):
            impacts = sorted(impacts, key=lambda i: i.identifier)
            peak = _peak_urgency(impacts)
            out.append(
                StateDelta(
                    change=f"{verb}.{domain}",
                    domain=domain,
                    before=None if escalating else [i.identifier for i in impacts],
                    after=[i.identifier for i in impacts] if escalating else None,
                    magnitude=_magnitude_for(peak, len(impacts)),
                    confidence=truth_confidence(impacts),
                    novelty=1.0,
                    causes=tuple(causes),
                    urgency=peak if escalating else Urgency.NONE,
                    # Only confirmed/official impact counts as official
                    # guidance; inferred geometry overlap never does.
                    official_guidance=escalating
                    and any(i.truth_status == TruthStatus.CONFIRMED for i in impacts),
                )
            )
        return out

    def _confidence(
        self, previous: EventState, current: EventState, causes: Sequence[str]
    ) -> list[StateDelta]:
        before = previous.evidence.vector.get("source_authority", 0.0)
        after = current.evidence.vector.get("source_authority", 0.0)
        gap = abs(after - before)
        if gap < 0.08:
            return []
        return [
            StateDelta(
                change="confidence_improved" if after > before else "confidence_degraded",
                domain="evidence",
                before=round(before, 4),
                after=round(after, 4),
                magnitude=round(min(1.0, gap * 1.5), 4),
                confidence=round(after, 4),
                novelty=0.5,
                causes=tuple(causes),
                urgency=Urgency.NONE,
            )
        ]

    def _forecasts(
        self,
        before: Sequence[Forecast],
        after: Sequence[Forecast],
        causes: Sequence[str],
    ) -> list[StateDelta]:
        before_map = {f.target: f for f in before}
        after_map = {f.target: f for f in after}
        out: list[StateDelta] = []

        for target, forecast in sorted(after_map.items()):
            prior = before_map.get(target)
            if prior is None:
                if forecast.probability < 0.15:
                    continue
                out.append(
                    StateDelta(
                        change=f"forecast_appeared.{target}",
                        domain=str(forecast.domain),
                        before=None,
                        after=round(forecast.probability, 4),
                        magnitude=round(forecast.probability, 4),
                        confidence=round(forecast.probability, 4),
                        novelty=1.0,
                        causes=tuple(causes),
                        urgency=Urgency.LOW,
                        # A prediction is never official guidance.
                        official_guidance=False,
                    )
                )
                continue

            gap = abs(forecast.probability - prior.probability)
            if gap < _FORECAST_MATERIAL_GAP:
                continue
            out.append(
                StateDelta(
                    change=f"forecast_moved.{target}",
                    domain=str(forecast.domain),
                    before=round(prior.probability, 4),
                    after=round(forecast.probability, 4),
                    magnitude=round(min(1.0, gap), 4),
                    confidence=round(
                        max(forecast.probability, 1.0 - forecast.probability), 4
                    ),
                    novelty=round(min(1.0, gap), 4),
                    causes=tuple(causes),
                    urgency=Urgency.LOW,
                )
            )
        return out

    def _official_guidance(
        self, previous: EventState, current: EventState, causes: Sequence[str]
    ) -> list[StateDelta]:
        before = has_official_guidance(previous)
        after = has_official_guidance(current)
        if before == after:
            return []
        return [
            StateDelta(
                change=(
                    "official_guidance_issued" if after else "official_guidance_lapsed"
                ),
                domain="public_safety",
                before=before,
                after=after,
                magnitude=1.0 if after else 0.5,
                confidence=1.0,
                novelty=1.0,
                causes=tuple(causes),
                urgency=Urgency.IMMEDIATE if after else Urgency.NONE,
                official_guidance=after,
            )
        ]

    # -- helpers ----------------------------------------------------------

    def _new_observations(
        self, previous: EventState, current: EventState
    ) -> tuple[str, ...]:
        seen = set(previous.observation_ids)
        return tuple(oid for oid in current.observation_ids if oid not in seen)

    def _finish(self, report: DeltaReport) -> DeltaReport:
        if not report.deltas:
            report.suppressed_reason = "no observable change"
            return report

        report.material_deltas = tuple(
            d for d in report.deltas if d.magnitude >= self.materiality_floor
        )
        report.magnitude = max(d.magnitude for d in report.deltas)
        report.novelty = max(d.novelty for d in report.deltas)
        report.confidence = sum(d.confidence for d in report.deltas) / len(report.deltas)
        report.is_material = bool(report.material_deltas)

        if not report.is_material:
            report.suppressed_reason = (
                f"all changes below materiality floor {self.materiality_floor} "
                f"(max magnitude {report.magnitude:.3f})"
            )
        elif not report.causes and report.deltas[0].change != "event_initialised":
            report.notes.append(
                "change detected with no new observations; likely a recomputation "
                "artifact rather than new world information"
            )
        return report


# --------------------------------------------------------------------------
# module-level predicates (shared with the user layer)
# --------------------------------------------------------------------------


def compare_user_exposure(
    previous: UserExposure | None,
    current: UserExposure,
    causes: Sequence[str] = (),
) -> list[StateDelta]:
    """Section 71's ``compare_previous_user_state``, for one user.

    This is what lets the platform say "your route is now affected because a
    new closure was reported" rather than re-explaining the entire event. The
    changes are personal and specific; they are not the event's deltas
    re-labelled.

    A user meeting an event for the first time gets one ``first_seen`` change
    carrying the full magnitude - otherwise a brand new, severe event would
    look identical to "nothing changed since last time".
    """
    if previous is None:
        return [
            StateDelta(
                change="user_first_exposed" if current.exposure_level is not Urgency.NONE else "user_first_seen",
                domain="user",
                before=None,
                after=current.exposure_level.value,
                magnitude=1.0 if current.exposure_level is not Urgency.NONE else 0.3,
                confidence=current.confidence,
                novelty=1.0,
                causes=tuple(causes),
                urgency=current.exposure_level,
                affected_user_count=1,
            )
        ]

    deltas: list[StateDelta] = []

    # Route-level changes: the ones a user actually acts on.
    before_routes = {r.route_id: r for r in previous.route_impacts if r.intersects}
    after_routes = {r.route_id: r for r in current.route_impacts if r.intersects}
    for route_id in sorted(set(before_routes) | set(after_routes)):
        was = before_routes.get(route_id)
        now = after_routes.get(route_id)
        if (was is None) == (now is None):
            continue
        blocked = now is not None
        deltas.append(
            StateDelta(
                change=(
                    "user_route_became_impacted" if blocked else "user_route_cleared"
                ),
                domain="user",
                before=False if blocked else True,
                after=blocked,
                magnitude=0.9 if blocked else 0.5,
                confidence=current.confidence,
                novelty=1.0,
                causes=tuple(causes),
                urgency=Urgency.HIGH if blocked else Urgency.LOW,
                affected_user_count=1,
            )
        )

    # A place that became exposed matters as much as a route that did.
    for place_id, score in sorted(current.saved_place_impacts.items()):
        was = previous.saved_place_impacts.get(place_id, 0.0)
        if score - was < 0.15:
            continue
        deltas.append(
            StateDelta(
                change="user_place_became_impacted",
                domain="user",
                before=round(was, 4),
                after=round(score, 4),
                magnitude=round(min(1.0, score), 4),
                confidence=current.confidence,
                novelty=1.0,
                causes=tuple(causes),
                urgency=current.exposure_level,
                affected_user_count=1,
            )
        )

    # Band change, including de-escalation. Losing an alert is news too.
    gap = _SEVERITY_RANK[current.exposure_level] - _SEVERITY_RANK[previous.exposure_level]
    if gap:
        rising = gap > 0
        deltas.append(
            StateDelta(
                change="user_exposure_escalated" if rising else "user_exposure_deescalated",
                domain="user",
                before=previous.exposure_level.value,
                after=current.exposure_level.value,
                magnitude=round(min(1.0, abs(gap) / 4.0), 4),
                confidence=current.confidence,
                novelty=1.0,
                causes=tuple(causes),
                urgency=current.exposure_level if rising else Urgency.NONE,
                affected_user_count=1,
            )
        )

    return deltas


def truth_confidence(impacts: Sequence[AffectedInfrastructure]) -> float:
    """How much of an impact set rests on something stronger than inference."""
    if not impacts:
        return 0.0
    weight = {
        TruthStatus.CONFIRMED: 1.0,
        TruthStatus.REPORTED: 0.7,
        TruthStatus.PREDICTED: 0.5,
        TruthStatus.INFERRED: 0.35,
    }
    return round(sum(weight.get(i.truth_status, 0.4) for i in impacts) / len(impacts), 4)


def dominant_label(distribution: dict[str, float]) -> str | None:
    """Most probable non-placeholder label in a soft classification."""
    if not distribution:
        return None
    ranked = [
        kv for kv in distribution.items() if kv[0] != "unknown" and kv[1] > 0.0
    ]
    if not ranked:
        return "unknown"
    return max(ranked, key=lambda kv: kv[1])[0]


def has_official_guidance(state: EventState) -> bool:
    """True only when an official emergency notice is actually present.

    Deliberately conservative: inferred geometry overlap or a police report
    must never be promoted to "official guidance" (section 38).
    """
    if state.derived.get("is_official_guidance", 0.0) >= 1.0:
        return True
    return "official_emergency_notice" in state.evidence.observation_types


def peak_urgency(impacts: Sequence[AffectedInfrastructure]) -> Urgency:
    return _peak_urgency(impacts)


def _magnitude_for(peak: Urgency, count: int) -> float:
    severity = _SEVERITY_RANK[peak] / 4.0
    breadth = min(1.0, count / 5.0)
    return round(min(1.0, 0.7 * severity + 0.3 * breadth), 4)


def _peak_urgency(impacts: Sequence[AffectedInfrastructure]) -> Urgency:
    if not impacts:
        return Urgency.NONE
    return max((i.severity for i in impacts), key=lambda s: _SEVERITY_RANK[s])


def _centroid(geometry: dict[str, Any] | None) -> list[float] | None:
    centre = centroid_of(geometry)
    return [round(centre[0], 6), round(centre[1], 6)] if centre else None


__all__ = [
    "MATERIALITY_FLOOR",
    "DeltaReport",
    "StateDeltaEngine",
    "dominant_label",
    "has_official_guidance",
    "peak_urgency",
    "truth_confidence",
]
"""User exposure engine (brief section 35).

Exposure is primarily deterministic and geospatial:

    infrastructure impact + user location/route -> geospatial intersection
    -> exposure

Predictive models are layered on afterwards to *estimate severity*, never to
decide whether the user is geometrically affected. Two users looking at the
same event get different answers because their routes differ - that is the
whole product (section 2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from ..delta.engine import has_official_guidance, truth_confidence
from ..domain.enums import InfrastructureDomain, TruthStatus, Urgency
from ..domain.geo import SpatialIndex, contains, distance_m, intersects
from ..domain.ids import utcnow
from ..domain.schemas import (
    AffectedInfrastructure,
    EventState,
    Forecast,
    RouteImpact,
    RouteProfile,
    SavedPlace,
    UserContext,
    UserExposure,
)
from ..graph.model import InfrastructureGraph

log = logging.getLogger(__name__)

#: Beyond this distance from the footprint, exposure is irrelevant.
EXPOSURE_RADIUS_M = 10_000.0

#: Full route-disruption credit at/inside this distance from an impact.
ROUTE_BLOCKED_M = 100.0

_SEVERITY_SCORE = {
    Urgency.NONE: 0.0,
    Urgency.LOW: 0.25,
    Urgency.MODERATE: 0.5,
    Urgency.HIGH: 0.8,
    Urgency.IMMEDIATE: 1.0,
}

#: Delay estimate per unit of exposure, capped so a closure never implies an
#: implausible delay. Deliberately conservative: under-promise, over-deliver.
_DELAY_PER_EXPOSURE_MIN = 25.0
_MAX_DELAY_MIN = 45.0


@dataclass
class ExposureContext:
    """Everything exposure may read. Passed explicitly, never imported."""

    user: UserContext
    state: EventState
    impacts: Sequence[AffectedInfrastructure]
    forecasts: Sequence[Forecast]
    analysis_run_id: str
    graph: InfrastructureGraph | None = None

    def peak_probability(self, domain: InfrastructureDomain) -> float:
        return max(
            (f.probability for f in self.forecasts if f.domain is domain),
            default=0.0,
        )


class ExposureEngine:
    """Computes ``UserExposure`` for one user against one event."""

    def __init__(self, graph: InfrastructureGraph | None = None) -> None:
        self.graph = graph

    def compute(self, ctx: ExposureContext) -> UserExposure:
        user = ctx.user
        impacts_index = SpatialIndex(
            (imp.identifier, imp.geometry, imp) for imp in ctx.impacts if imp.geometry
        )
        route_impacts = tuple(
            self._route_impact(route, ctx, impacts_index) for route in user.route_profiles
        )
        route_impacts = tuple(r for r in route_impacts if r.intersects)

        place_impacts = {
            place.place_id: round(
                self._place_exposure(place, ctx, impacts_index),
                4,
            )
            for place in user.saved_places
        }
        place_impacts = {k: v for k, v in place_impacts.items() if v > 0.0}

        distance, inside = self._proximity(user, ctx)

        components = self._components(ctx, distance, inside, route_impacts, place_impacts)
        score = self._score(components)

        return UserExposure(
            user_id=user.user_id,
            event_id=ctx.state.event_id,
            analysis_run_id=ctx.analysis_run_id,
            distance_m=round(distance, 1) if distance is not None else None,
            inside_impact_area=inside,
            route_impacts=route_impacts,
            saved_place_impacts=place_impacts,
            exposure_level=exposure_band(score, ctx),
            exposure_score=round(score, 4),
            components=components,
            confidence=round(self._confidence(ctx), 4),
            as_of=utcnow(),
        )

    # -- proximity --------------------------------------------------------

    def _proximity(self, user: UserContext, ctx: ExposureContext) -> tuple[float | None, bool]:
        """Distance from the user's most relevant point to the footprint."""
        candidates: list[dict[str, Any]] = []
        if user.current_location:
            candidates.append(user.current_location)
        candidates.extend(p.geometry for p in user.saved_places)
        candidates.extend(r.geometry for r in user.route_profiles)
        if not candidates:
            return None, False

        distances = [distance_m(g, ctx.state.geometry) for g in candidates]
        nearest = min(distances)
        inside = any(
            g and ctx.state.geometry and contains(ctx.state.geometry, _centre(g))
            for g in candidates
        )
        return (nearest, inside)

    # -- routes -----------------------------------------------------------

    def _route_impact(
        self,
        route: RouteProfile,
        ctx: ExposureContext,
        impacts_index: SpatialIndex[AffectedInfrastructure] | None = None,
    ) -> RouteImpact:
        blocked: list[str] = []
        distances: list[float] = []
        modes = set(route.modes)

        if impacts_index is not None and route.geometry:
            candidates = impacts_index.query_within_distance(route.geometry, ROUTE_BLOCKED_M)
            intersecting = impacts_index.query_intersects(route.geometry)
            candidate_map = {c.identifier: c for c in candidates + intersecting}
            impact_list = list(candidate_map.values())
        else:
            impact_list = list(ctx.impacts)

        for impact in impact_list:
            if not impact.geometry:
                continue
            if not _mode_matches(impact, modes, ctx):
                continue
            d = distance_m(route.geometry, impact.geometry)
            if d <= ROUTE_BLOCKED_M:
                blocked.append(impact.identifier)
                distances.append(d)
            elif intersects(route.geometry, impact.geometry):
                blocked.append(impact.identifier)
                distances.append(0.0)

        if not blocked:
            return RouteImpact(
                route_id=route.route_id,
                route_name=route.name,
                intersects=False,
                evidence_ids=_evidence_for(ctx.impacts, blocked),
            )

        delay = self._delay(blocked, ctx)
        alternative = self._alternative(route, blocked, ctx)
        return RouteImpact(
            route_id=route.route_id,
            route_name=route.name,
            intersects=True,
            delay_estimate_min=round(delay, 1),
            blocked_node_ids=tuple(sorted(blocked)),
            alternative_available=alternative is not None,
            alternative_geometry=alternative,
            evidence_ids=_evidence_for(ctx.impacts, blocked),
        )

    def _alternative(
        self,
        route: RouteProfile,
        blocked: Sequence[str],
        ctx: ExposureContext,
    ) -> dict[str, Any] | None:
        """A reroute exists only if the graph offers a path around the block.

        Returning ``None`` when no alternative exists is the important part:
        the system must never recommend "take another way" when there is no
        other way (section 29).
        """
        if self.graph is None or not route.node_ids:
            return None

        # Section 29: Use GraphRoutingEngine to find and verify alternative paths
        from ..graph.routing import GraphRoutingEngine

        if len(route.node_ids) >= 2:
            router = GraphRoutingEngine(self.graph)
            router.apply_closures(blocked)
            alts = router.alternatives(
                origin_node_id=route.node_ids[0],
                destination_node_id=route.node_ids[-1],
                max_routes=1,
                avoid_disruptions=True,
            )
            for alt in alts:
                if alt.verified_clear_of_disruptions and alt.geometry:
                    return alt.geometry

        blocked_set = set(blocked)
        for node_id in route.node_ids:
            if node_id in blocked_set:
                continue
            node = self.graph.nodes.get(node_id)
            if node is None or node.geometry is None:
                continue
            clear = all(
                distance_m(node.geometry, i.geometry) > ROUTE_BLOCKED_M
                for i in ctx.impacts
                if i.geometry and i.identifier in blocked_set
            )
            if clear:
                return node.geometry
        return None

    def _delay(self, blocked: Sequence[str], ctx: ExposureContext) -> float:
        impacts = [i for i in ctx.impacts if i.identifier in set(blocked)]
        severity = max((_SEVERITY_SCORE[i.severity] for i in impacts), default=0.0)
        breadth = min(1.0, len(impacts) / 3.0)
        predicted = max(
            (ctx.peak_probability(i.domain) for i in impacts), default=0.0
        )
        raw = _DELAY_PER_EXPOSURE_MIN * (0.6 * severity + 0.25 * breadth + 0.15 * predicted)
        return min(_MAX_DELAY_MIN, raw)

    # -- saved places -----------------------------------------------------

    def _place_exposure(
        self,
        place: SavedPlace,
        ctx: ExposureContext,
        impacts_index: SpatialIndex[AffectedInfrastructure] | None = None,
    ) -> float:
        """How exposed a saved place is, in 0..1."""
        if place.geometry and ctx.state.geometry and contains(ctx.state.geometry, _centre(place.geometry)):
            return 1.0

        if not place.geometry:
            return 0.0

        if impacts_index is not None:
            candidates = impacts_index.query_within_distance(place.geometry, EXPOSURE_RADIUS_M)
            if not candidates:
                return 0.0
            relevant_impacts = candidates
        else:
            relevant_impacts = [i for i in ctx.impacts if i.geometry]

        nearest = min(
            (distance_m(place.geometry, i.geometry) for i in relevant_impacts if i.geometry),
            default=float("inf"),
        )
        if nearest > EXPOSURE_RADIUS_M:
            return 0.0

        proximity = max(0.0, 1.0 - (nearest / EXPOSURE_RADIUS_M))
        domain_score = max(
            (
                _SEVERITY_SCORE[i.severity]
                for i in relevant_impacts
                if i.geometry and distance_m(place.geometry, i.geometry) <= EXPOSURE_RADIUS_M
            ),
            default=0.0,
        )
        predicted = max(
            (
                ctx.peak_probability(i.domain)
                for i in relevant_impacts
                if i.geometry and distance_m(place.geometry, i.geometry) <= EXPOSURE_RADIUS_M
            ),
            default=0.0,
        )
        # Home and work carry more consequence than an arbitrary saved pin.
        consequence = {"home": 1.0, "work": 0.9, "school": 0.85}.get(place.kind, 0.6)
        return round(min(1.0, proximity * (0.5 * domain_score + 0.2 * predicted + 0.3)) * consequence, 4)

    # -- scoring ----------------------------------------------------------

    def _components(
        self,
        ctx: ExposureContext,
        distance: float | None,
        inside: bool,
        route_impacts: Sequence[RouteImpact],
        place_impacts: dict[str, float],
    ) -> dict[str, float]:
        components: dict[str, float] = {}

        components["proximity"] = (
            1.0
            if inside
            else (round(max(0.0, 1.0 - (distance / EXPOSURE_RADIUS_M)), 4) if distance is not None else 0.0)
        )

        components["route_impact"] = round(
            min(1.0, len(route_impacts) / 2.0 * (1.0 if route_impacts else 0.0)),
            4,
        )
        components["saved_place_impact"] = round(max(place_impacts.values(), default=0.0), 4)

        relevant = [i for i in ctx.impacts if i.geometry]
        components["infrastructure_severity"] = round(
            max((_SEVERITY_SCORE[i.severity] for i in relevant), default=0.0), 4
        )
        components["forecast_risk"] = round(
            max(
                (
                    ctx.peak_probability(i.domain)
                    for i in relevant
                    if i.truth_status is not TruthStatus.CONFIRMED
                ),
                default=0.0,
            ),
            4,
        )
        components["official_guidance"] = 1.0 if has_official_guidance(ctx.state) else 0.0
        return components

    @staticmethod
    def _score(components: dict[str, float]) -> float:
        """Geospatial exposure dominates; predicted risk modulates it.

        Forecasts contribute only 10% on their own. A prediction must never be
        able to manufacture exposure that geometry does not support.
        """
        geometry = max(
            components["proximity"],
            components["route_impact"],
            components["saved_place_impact"],
        )
        if geometry <= 0.0:
            return 0.0
        severity = components["infrastructure_severity"]
        score = (
            0.45 * geometry
            + 0.25 * geometry * severity
            + 0.10 * components["forecast_risk"]
            + 0.20 * components["official_guidance"] * geometry
        )
        return max(0.0, min(1.0, score))

    @staticmethod
    def _confidence(ctx: ExposureContext) -> float:
        """How confident the platform is that *this user's* exposure is real."""
        base = ctx.state.evidence.vector.get("source_authority", 0.4)
        corroboration = ctx.state.evidence.vector.get("independent_corroboration", 0.0)
        geometry_quality = ctx.state.geometry_confidence
        impact_grounding = truth_confidence(ctx.impacts)
        return round(
            0.3 * base + 0.2 * corroboration + 0.2 * geometry_quality + 0.3 * impact_grounding,
            4,
        )


def exposure_band(score: float, ctx: ExposureContext) -> Urgency:
    """Map an exposure score to a band.

    Official guidance overrides the arithmetic: an issued emergency instruction
    is never downgraded because the user's score happens to be moderate
    (section 38).
    """
    if score <= 0.02:
        return Urgency.NONE
    if has_official_guidance(ctx.state):
        return Urgency.IMMEDIATE
    if score >= 0.72:
        return Urgency.IMMEDIATE
    if score >= 0.48:
        return Urgency.HIGH
    if score >= 0.26:
        return Urgency.MODERATE
    return Urgency.LOW


def _centre(geometry: dict[str, Any]) -> tuple[float, float]:
    from ..domain.geo import centroid_of

    return centroid_of(geometry) or (0.0, 0.0)


def _mode_matches(
    impact: AffectedInfrastructure, modes: set[str], ctx: ExposureContext
) -> bool:
    """Only consider impacts the user could plausibly be using."""
    if not modes:
        return True
    if impact.domain is InfrastructureDomain.TRANSIT:
        return bool(modes & {"transit", "bus", "walk"})
    if impact.domain is InfrastructureDomain.ROAD:
        return bool(modes & {"drive", "bike", "walk", "transit"})
    return True


def _evidence_for(
    impacts: Sequence[AffectedInfrastructure], blocked: Sequence[str]
) -> tuple[str, ...]:
    wanted = set(blocked)
    ids: list[str] = []
    for impact in impacts:
        if impact.identifier in wanted:
            ids.extend(impact.observation_ids)
    return tuple(dict.fromkeys(ids))[:12]


__all__ = ["EXPOSURE_RADIUS_M", "ROUTE_BLOCKED_M", "ExposureContext", "ExposureEngine", "exposure_band"]
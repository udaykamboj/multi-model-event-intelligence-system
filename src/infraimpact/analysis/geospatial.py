"""Geospatial capabilities (brief section 28).

Purely deterministic geometry - this is where "your route intersects a closure"
comes from. No model involved, because no model should be involved.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..domain.enums import CapabilityTier, InfrastructureDomain, NodeClass, ObservationType
from ..domain.geo import distance_m, intersects
from ..domain.ids import utcnow
from ..domain.schemas import AffectedInfrastructure, FeatureValue, TruthStatus, Urgency
from ..graph.model import InfrastructureGraph
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult

ARTERIAL_NEAR_M = 400.0
TRANSIT_NEAR_M = 300.0
CRITICAL_FACILITY_NEAR_M = 1200.0


class RoadOverlapCapability(AnalysisCapability):
    """Which roads are affected, and how badly."""

    capability_id = "road_network_exposure"
    tier = CapabilityTier.TRIGGERED
    description = "Deterministic overlap between event footprint and road network."
    triggered_by = frozenset(
        {
            ObservationType.ROAD_CLOSURE,
            ObservationType.ROAD_CONSTRUCTION,
            ObservationType.TRAFFIC_CONDITION,
        }
    )
    domains = frozenset({InfrastructureDomain.ROAD})
    estimated_latency_ms = 40
    estimated_cost = 0.05

    def __init__(self, graph: InfrastructureGraph | None = None) -> None:
        self.graph = graph

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        if self.graph is None or ctx.state.geometry is None:
            return result

        geometry = ctx.state.geometry
        intersecting = self.graph.nodes_intersecting(
            geometry, [NodeClass.ROAD_SEGMENT, NodeClass.INTERSECTION, NodeClass.BRIDGE]
        )
        nearby = self.graph.nodes_near(
            geometry, ARTERIAL_NEAR_M, [NodeClass.ROAD_SEGMENT]
        )

        arterial_hit = [n for n in intersecting if n.attributes.get("arterial")]
        arterial_near = [n for n in nearby if n.attributes.get("arterial")]

        for node in intersecting:
            severity = Urgency.HIGH if node.attributes.get("arterial") else Urgency.MODERATE
            result.impacts.append(
                AffectedInfrastructure(
                    domain=InfrastructureDomain.ROAD,
                    identifier=node.node_id,
                    name=node.name,
                    geometry=node.geometry,
                    severity=severity,
                    truth_status=TruthStatus.INFERRED,
                    observation_ids=ctx.evidence_ids(),
                    detected_at=utcnow(),
                )
            )

        nearest = min((distance_m(n.geometry, geometry) for n in nearby), default=None)
        blocked_nodes = {n.node_id for n in intersecting}
        blocked_edges = [
            e.edge_id for e in self.graph.edges.values()
            if e.source_node_id in blocked_nodes or e.target_node_id in blocked_nodes
        ]
        connectivity = self.graph.connectivity_ratio(blocked_nodes)
        # Nodes still mutually reachable = largest connected component.
        reachable_count = int(round(connectivity * len(self.graph.nodes)))

        # Deterministic detour for the severed corridor. Endpoints are derived
        # from the blocked infrastructure itself, never hardcoded.
        detour_info = self.graph.corridor_detour(blocked_nodes)

        result.features.update(
            {
                "road_overlap_count": FeatureValue(
                    name="road_overlap_count", value=float(len(intersecting))
                ),
                "arterial_overlap_count": FeatureValue(
                    name="arterial_overlap_count", value=float(len(arterial_hit))
                ),
                "arterial_proximity_count": FeatureValue(
                    name="arterial_proximity_count", value=float(len(arterial_near))
                ),
                "nearest_road_m": FeatureValue(
                    name="nearest_road_m",
                    value=round(nearest, 1) if nearest is not None else None,
                    unit="metres",
                ),
                "network_connectivity_ratio": FeatureValue(
                    name="network_connectivity_ratio",
                    value=connectivity,
                    unit="ratio",
                ),
                "reachable_infrastructure_count": FeatureValue(
                    name="reachable_infrastructure_count",
                    value=float(reachable_count),
                ),
                "blocked_edges": FeatureValue(
                    name="blocked_edges",
                    value=blocked_edges,
                ),
                "alternate_routes_available": FeatureValue(
                    name="alternate_routes_available",
                    value=float(1 if detour_info["alternate_available"] else 0),
                ),
                "primary_route_distance_m": FeatureValue(
                    name="primary_route_distance_m",
                    value=float(detour_info["nominal_distance_m"]),
                    unit="metres",
                ),
                "detour_distance_m": FeatureValue(
                    name="detour_distance_m",
                    value=float(detour_info["detour_distance_m"]),
                    unit="metres",
                ),
                "detour_percentage": FeatureValue(
                    name="detour_percentage",
                    value=float(detour_info["detour_percentage"]),
                    unit="percent",
                ),
                "additional_travel_time_s": FeatureValue(
                    name="additional_travel_time_s",
                    value=float(detour_info["additional_travel_time_s"]),
                    unit="seconds",
                ),
                "alternate_route_path": FeatureValue(
                    name="alternate_route_path",
                    value=detour_info["detour_path"],
                ),
            }
        )
        if arterial_hit:
            result.notes.append(
                f"{len(arterial_hit)} arterial road(s) intersect the observed footprint"
            )
        if detour_info["alternate_available"] and detour_info["detour_distance_m"] > 0:
            result.notes.append(
                f"detour available: +{detour_info['detour_distance_m']}m (+{detour_info['detour_percentage']}%), "
                f"+{detour_info['additional_travel_time_s']}s"
            )
        return result


class TransitOverlapCapability(AnalysisCapability):
    """Which transit routes and stops are affected."""

    capability_id = "transit_disruption"
    tier = CapabilityTier.TRIGGERED
    description = "Transit routes and stops overlapping the event footprint."
    triggered_by = frozenset(
        {
            ObservationType.TRANSIT_SERVICE_ALERT,
            ObservationType.TRANSIT_DELAY,
            ObservationType.ROAD_CLOSURE,
        }
    )
    domains = frozenset({InfrastructureDomain.TRANSIT})
    estimated_latency_ms = 40
    estimated_cost = 0.05

    def __init__(self, graph: InfrastructureGraph | None = None) -> None:
        self.graph = graph

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        if self.graph is None or ctx.state.geometry is None:
            return result

        geometry = ctx.state.geometry
        routes = self.graph.nodes_intersecting(
            geometry, [NodeClass.TRANSIT_ROUTE, NodeClass.TRANSIT_STOP, NodeClass.STATION]
        )
        nearby = self.graph.nodes_near(geometry, TRANSIT_NEAR_M, [NodeClass.TRANSIT_STOP])

        for node in routes:
            result.impacts.append(
                AffectedInfrastructure(
                    domain=InfrastructureDomain.TRANSIT,
                    identifier=node.node_id,
                    name=node.name,
                    geometry=node.geometry,
                    severity=Urgency.HIGH,
                    truth_status=TruthStatus.INFERRED,
                    observation_ids=ctx.evidence_ids(),
                    detected_at=utcnow(),
                )
            )

        result.features.update(
            {
                "transit_route_overlap": FeatureValue(
                    name="transit_route_overlap", value=float(len(routes))
                ),
                "transit_stop_proximity": FeatureValue(
                    name="transit_stop_proximity", value=float(len(nearby))
                ),
                "transit_route_redundancy": FeatureValue(
                    name="transit_route_redundancy",
                    value=round(_redundancy(self.graph, [n.node_id for n in routes]), 4),
                    unit="ratio",
                ),
            }
        )
        if len(routes) > 1:
            result.notes.append(f"{len(routes)} transit route(s)/stop(s) overlap the footprint")
        return result


class CriticalFacilityExposureCapability(AnalysisCapability):
    """Which hospitals, schools and emergency facilities are exposed."""

    capability_id = "critical_facility_exposure"
    tier = CapabilityTier.TRIGGERED
    description = "Exposure of critical facilities to the observed footprint."
    triggered_by = frozenset(
        {
            ObservationType.POLICE_RESPONSE,
            ObservationType.FIRE_DISPATCH,
            ObservationType.OFFICIAL_EMERGENCY_NOTICE,
            ObservationType.ROAD_CLOSURE,
            ObservationType.TRANSIT_SERVICE_ALERT,
            ObservationType.EARTHQUAKE,
            ObservationType.SEVERE_WEATHER,
        }
    )
    domains = frozenset({InfrastructureDomain.PUBLIC_SAFETY, InfrastructureDomain.PUBLIC_FACILITY})
    estimated_latency_ms = 60
    estimated_cost = 0.08

    def __init__(self, graph: InfrastructureGraph | None = None) -> None:
        self.graph = graph

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        if self.graph is None or ctx.state.geometry is None:
            return result

        geometry = ctx.state.geometry
        facilities = self.graph.nodes_near(
            geometry, CRITICAL_FACILITY_NEAR_M, [NodeClass.HOSPITAL, NodeClass.SCHOOL, NodeClass.EMERGENCY_FACILITY]
        )

        inside = [f for f in facilities if f.geometry and intersects(f.geometry, geometry)]
        for node in facilities:
            d = distance_m(node.geometry, geometry)
            severity = Urgency.HIGH if node in inside else (Urgency.MODERATE if d < 700 else Urgency.LOW)
            result.impacts.append(
                AffectedInfrastructure(
                    domain=_facility_domain(node.node_class),
                    identifier=node.node_id,
                    name=node.name,
                    geometry=node.geometry,
                    severity=severity,
                    truth_status=TruthStatus.INFERRED,
                    observation_ids=ctx.evidence_ids(),
                    detected_at=utcnow(),
                )
            )

        result.features.update(
            {
                "critical_facility_proximity": FeatureValue(
                    name="critical_facility_proximity", value=float(len(facilities))
                ),
                "critical_facility_inside": FeatureValue(
                    name="critical_facility_inside", value=float(len(inside))
                ),
                "emergency_facility_inside": FeatureValue(
                    name="emergency_facility_inside",
                    value=float(
                        len([f for f in inside if f.node_class == NodeClass.EMERGENCY_FACILITY])
                    ),
                ),
                "hospital_proximity": FeatureValue(
                    name="hospital_proximity",
                    value=float(len([f for f in facilities if f.node_class == NodeClass.HOSPITAL])),
                ),
            }
        )
        if inside:
            result.notes.append(
                f"{len(inside)} critical facilit{'y' if len(inside) == 1 else 'ies'} inside the footprint"
            )
        return result


class UserRouteExposureCapability(AnalysisCapability):
    """Per-user route intersection. Cheap and deterministic.

    Runs at CHEAP tier because it must answer "is this user affected?" on every
    analysis, including the frequent ones.
    """

    capability_id = "user_route_exposure"
    tier = CapabilityTier.CHEAP
    description = "Intersection between user routes/places and the affected footprint."
    domains = frozenset(
        {InfrastructureDomain.ROAD, InfrastructureDomain.TRANSIT, InfrastructureDomain.PUBLIC_FACILITY}
    )
    estimated_latency_ms = 30
    estimated_cost = 0.03

    def __init__(self, graph: InfrastructureGraph | None = None) -> None:
        self.graph = graph

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        """Fills a per-event index of impacted user routes.

        The authoritative per-user computation lives in
        ``infraimpact.users.exposure`` because it needs the UserContext, which
        this capability deliberately does not receive. This stage records the
        impacted infrastructure set that the user stage intersects against.
        """
        result = CapabilityResult()
        impacted = [i for i in ctx.state.affected_infrastructure]
        routes_blocked = [i.identifier for i in impacted if i.domain == InfrastructureDomain.ROAD]
        transit_hit = [i.identifier for i in impacted if i.domain == InfrastructureDomain.TRANSIT]

        result.features.update(
            {
                "user_route_blocked_candidates": FeatureValue(
                    name="user_route_blocked_candidates", value=float(len(routes_blocked))
                ),
                "user_route_transit_candidates": FeatureValue(
                    name="user_route_transit_candidates", value=float(len(transit_hit))
                ),
                "route_redundancy": FeatureValue(
                    name="route_redundancy",
                    value=round(_redundancy(self.graph, routes_blocked), 4),
                    unit="ratio",
                ),
            }
        )
        return result


def _facility_domain(node_class: NodeClass) -> InfrastructureDomain:
    if node_class == NodeClass.EMERGENCY_FACILITY:
        return InfrastructureDomain.PUBLIC_SAFETY
    return InfrastructureDomain.PUBLIC_FACILITY


def _redundancy(graph: InfrastructureGraph | None, blocked: Sequence[str]) -> float:
    """How much alternative capacity remains (section 24/29).

    A closure on one arterial among many is survivable; a closure with no
    alternative is not. This is what stops the system recommending "take the
    other way" when there is no other way.
    """
    if graph is None or not blocked:
        return 1.0
    class_values = {
        node_id: node.attributes.get("classification")
        for node_id, node in graph.nodes.items()
    }
    blocked_class = class_values.get(blocked[0])
    if blocked_class != "arterial":
        return 0.75
    arterials = [nid for nid, cls in class_values.items() if cls == "arterial"]
    remaining = [nid for nid in arterials if nid not in set(blocked)]
    if not arterials:
        return 0.5
    return round(len(remaining) / len(arterials), 4)


__all__ = [
    "CriticalFacilityExposureCapability",
    "RoadOverlapCapability",
    "TransitOverlapCapability",
    "UserRouteExposureCapability",
    "Any",
]
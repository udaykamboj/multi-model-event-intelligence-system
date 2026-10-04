"""Spatial propagation through the infrastructure graph (brief section 27).

    ROAD EDGE -> INTERSECTION -> ARTERIAL -> BRIDGE -> TRANSIT ROUTE

Impacts are not circles. A closure propagates along the network, and transit
routes inherit delay from the roads they depend on. Baseline is graph
algorithms; GNN / temporal-graph models are a later research step measured
against this baseline, not a replacement for it.
"""

from __future__ import annotations

from ..domain.enums import CapabilityTier, InfrastructureDomain, NodeClass, ObservationType
from ..domain.ids import utcnow
from ..domain.schemas import AffectedInfrastructure, FeatureValue, TruthStatus, Urgency
from ..graph.model import InfrastructureGraph
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult


class PropagationCapability(AnalysisCapability):
    """Propagate impact from directly-affected nodes through the network."""

    capability_id = "infrastructure_propagation"
    tier = CapabilityTier.TRIGGERED
    description = "Weighted BFS propagation of disruption through connected infrastructure."
    triggered_by = frozenset(
        {
            ObservationType.ROAD_CLOSURE,
            ObservationType.ROAD_CONSTRUCTION,
            ObservationType.TRANSIT_SERVICE_ALERT,
            ObservationType.TRANSIT_DELAY,
            ObservationType.OFFICIAL_EMERGENCY_NOTICE,
            ObservationType.EARTHQUAKE,
            ObservationType.FIRE_DISPATCH,
        }
    )
    domains = frozenset({InfrastructureDomain.ROAD, InfrastructureDomain.TRANSIT})
    estimated_latency_ms = 90
    estimated_cost = 0.15

    def __init__(self, graph: InfrastructureGraph | None = None, max_depth: int = 3) -> None:
        self.graph = graph
        self.max_depth = max_depth

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        if self.graph is None:
            return result

        seeds = self._seed_nodes(ctx)
        if not seeds:
            return result

        hops = self.graph.propagate(seeds, max_depth=self.max_depth, max_weight=6.0)

        secondary: list[AffectedInfrastructure] = []
        for hop in hops:
            if hop.depth == 0:
                continue
            node = self.graph.nodes[hop.node_id]
            # Decay with distance: depth 1 is "likely", depth 3 is "possible".
            base = {1: 0.7, 2: 0.45, 3: 0.25}.get(hop.depth, 0.15)
            score = max(0.05, base * max(0.3, 1.0 - hop.cumulative_weight / 8.0))
            severity = _severity_from(score, node.node_class)
            secondary.append(
                AffectedInfrastructure(
                    domain=hop.domain,
                    identifier=hop.node_id,
                    name=node.name,
                    geometry=node.geometry,
                    severity=severity,
                    truth_status=TruthStatus.INFERRED,
                    observation_ids=ctx.evidence_ids(),
                    detected_at=utcnow(),
                )
            )

        result.impacts.extend(secondary)
        reach = len({h.node_id for h in hops})
        result.features.update(
            {
                "propagation_reach": FeatureValue(name="propagation_reach", value=float(reach)),
                "propagation_depth": FeatureValue(
                    name="propagation_depth", value=float(max((h.depth for h in hops), default=0))
                ),
                "propagation_score": FeatureValue(
                    name="propagation_score",
                    value=round(
                        sum(h.cumulative_weight and 1.0 / (1.0 + h.cumulative_weight) for h in hops)
                        / max(1, len(hops)),
                        4,
                    ),
                ),
                "secondary_impact_count": FeatureValue(
                    name="secondary_impact_count", value=float(len(secondary))
                ),
                "transit_dependency_impact": FeatureValue(
                    name="transit_dependency_impact",
                    value=float(
                        len([h for h in hops if h.domain == InfrastructureDomain.TRANSIT and h.depth > 0])
                    ),
                ),
            }
        )
        if secondary:
            result.notes.append(
                f"disruption propagates to {len(secondary)} connected infrastructure nodes"
            )
        return result

    def _seed_nodes(self, ctx: CapabilityContext) -> list[str]:
        """Seed from geometry where possible, else from reported identifiers."""
        seeds: list[str] = []
        if ctx.state.geometry is not None:
            seeds.extend(
                node.node_id
                for node in self.graph.nodes_intersecting(ctx.state.geometry)
                if node.node_class
                in {
                    NodeClass.ROAD_SEGMENT,
                    NodeClass.INTERSECTION,
                    NodeClass.BRIDGE,
                    NodeClass.TRANSIT_ROUTE,
                    NodeClass.TRANSIT_STOP,
                }
            )
        # Also seed from explicitly reported identifiers ("Route 40", "4th Ave").
        for impact in ctx.state.affected_infrastructure:
            if impact.identifier in self.graph.nodes and impact.identifier not in seeds:
                seeds.append(impact.identifier)
        return list(dict.fromkeys(seeds))


def _severity_from(score: float, node_class: NodeClass) -> Urgency:
    if node_class in {NodeClass.HOSPITAL, NodeClass.EMERGENCY_FACILITY}:
        bump = 0.15
    elif node_class in {NodeClass.TRANSIT_ROUTE, NodeClass.TRANSIT_STOP}:
        bump = 0.05
    else:
        bump = 0.0
    total = score + bump
    if total >= 0.6:
        return Urgency.HIGH
    if total >= 0.35:
        return Urgency.MODERATE
    if total >= 0.15:
        return Urgency.LOW
    return Urgency.NONE


__all__ = ["PropagationCapability"]
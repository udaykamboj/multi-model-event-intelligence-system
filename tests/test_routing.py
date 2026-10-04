"""Tests for routing provider, disruption avoidance, and alternative routes (section 29)."""

import pytest

from infraimpact.domain.enums import EdgeKind, NodeClass
from infraimpact.domain.geo import point
from infraimpact.domain.schemas import GraphEdge, GraphNode, RouteProfile, UserContext
from infraimpact.graph.model import InfrastructureGraph
from infraimpact.graph.routing import GraphRoutingEngine, RoutingResult
from infraimpact.users.exposure import ExposureContext, ExposureEngine


def _build_test_graph() -> InfrastructureGraph:
    """Creates a small 4-node grid:
         (N1) --- (N2)
           |        |
         (N3) --- (N4)
    """
    g = InfrastructureGraph()
    g.add_node(GraphNode(node_id="N1", node_class=NodeClass.INTERSECTION, name="1st & Pike", geometry=point(-122.338, 47.609)))
    g.add_node(GraphNode(node_id="N2", node_class=NodeClass.INTERSECTION, name="2nd & Pike", geometry=point(-122.336, 47.609)))
    g.add_node(GraphNode(node_id="N3", node_class=NodeClass.INTERSECTION, name="1st & Pine", geometry=point(-122.338, 47.611)))
    g.add_node(GraphNode(node_id="N4", node_class=NodeClass.INTERSECTION, name="2nd & Pine", geometry=point(-122.336, 47.611)))

    g.add_edge(GraphEdge(edge_id="e1_2", kind=EdgeKind.CONNECTS_TO, source_node_id="N1", target_node_id="N2", weight=1.0))
    g.add_edge(GraphEdge(edge_id="e2_1", kind=EdgeKind.CONNECTS_TO, source_node_id="N2", target_node_id="N1", weight=1.0))

    g.add_edge(GraphEdge(edge_id="e1_3", kind=EdgeKind.CONNECTS_TO, source_node_id="N1", target_node_id="N3", weight=1.0))
    g.add_edge(GraphEdge(edge_id="e3_1", kind=EdgeKind.CONNECTS_TO, source_node_id="N3", target_node_id="N1", weight=1.0))

    g.add_edge(GraphEdge(edge_id="e2_4", kind=EdgeKind.CONNECTS_TO, source_node_id="N2", target_node_id="N4", weight=1.0))
    g.add_edge(GraphEdge(edge_id="e4_2", kind=EdgeKind.CONNECTS_TO, source_node_id="N4", target_node_id="N2", weight=1.0))

    g.add_edge(GraphEdge(edge_id="e3_4", kind=EdgeKind.CONNECTS_TO, source_node_id="N3", target_node_id="N4", weight=1.0))
    g.add_edge(GraphEdge(edge_id="e4_3", kind=EdgeKind.CONNECTS_TO, source_node_id="N4", target_node_id="N3", weight=1.0))

    return g


def test_routing_basic_route():
    g = _build_test_graph()
    engine = GraphRoutingEngine(g)

    res = engine.route("N1", "N4")
    assert res is not None
    assert res.path_node_ids in (("N1", "N2", "N4"), ("N1", "N3", "N4"))
    assert res.travel_time_min > 0.0
    assert res.verified_clear_of_disruptions is True


def test_routing_closure_rerouting_and_verification():
    g = _build_test_graph()
    engine = GraphRoutingEngine(g)

    # Close N2: path N1 -> N2 -> N4 is blocked
    engine.apply_closures(["N2"])

    # Compute alternatives: must avoid N2 and route via N3
    alts = engine.alternatives("N1", "N4", max_routes=2, avoid_disruptions=True)
    assert len(alts) >= 1
    for alt in alts:
        assert "N2" not in alt.path_node_ids
        assert alt.verified_clear_of_disruptions is True
        assert alt.intersecting_disruptions == ()


def test_routing_isochrone():
    g = _build_test_graph()
    engine = GraphRoutingEngine(g)

    reachable = engine.isochrone("N1", minutes=5.0)
    assert "N1" in reachable
    assert "N2" in reachable
    assert "N3" in reachable
    assert "N4" in reachable

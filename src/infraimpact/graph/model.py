"""Infrastructure graph (brief section 30).

The city is represented as a network, not a set of circles. An event does not
affect a point; it affects connected infrastructure, so disruption can reach a
road three hops away.

    ROAD EDGE -> INTERSECTION -> ARTERIAL -> BRIDGE -> TRANSIT ROUTE

A dedicated graph database is deliberately *not* required for V1: PostGIS plus
these structures are sufficient. Benchmarks decide if that changes later.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..domain.enums import EdgeKind, InfrastructureDomain, NodeClass
from ..domain.geo import Geometry, distance_m, intersects
from ..domain.schemas import GraphEdge, GraphNode


@dataclass
class PropagationHop:
    node_id: str
    node_class: NodeClass
    domain: InfrastructureDomain
    depth: int
    path: tuple[str, ...]
    cumulative_weight: float


class InfrastructureGraph:
    """In-memory adjacency over PostGIS-backed nodes and edges."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        # ``_out`` is node adjacency (what ``neighbours`` returns); ``_out_edges``
        # is edge adjacency, needed by propagation which must read each edge's
        # kind and weight.
        self._out: dict[str, list[str]] = defaultdict(list)
        self._in: dict[str, list[str]] = defaultdict(list)
        self._out_edges: dict[str, list[str]] = defaultdict(list)

    # -- construction ------------------------------------------------------

    def add_node(self, node: GraphNode) -> GraphNode:
        self.nodes[node.node_id] = node
        self._out.setdefault(node.node_id, [])
        self._in.setdefault(node.node_id, [])
        self._out_edges.setdefault(node.node_id, [])
        return node

    def add_edge(self, edge: GraphEdge) -> GraphEdge:
        self.edges[edge.edge_id] = edge
        self._out[edge.source_node_id].append(edge.target_node_id)
        self._out_edges[edge.source_node_id].append(edge.edge_id)
        self._in[edge.target_node_id].append(edge.source_node_id)
        return edge

    def connect(
        self,
        source_id: str,
        target_id: str,
        kind: EdgeKind = EdgeKind.CONNECTS_TO,
        weight: float = 1.0,
        edge_id: str | None = None,
        **attributes: Any,
    ) -> GraphEdge:
        return self.add_edge(
            GraphEdge(
                edge_id=edge_id or f"{kind.value}:{source_id}->{target_id}",
                kind=kind,
                source_node_id=source_id,
                target_node_id=target_id,
                weight=weight,
                attributes=attributes,
            )
        )

    # -- queries -----------------------------------------------------------

    def neighbours(self, node_id: str) -> list[str]:
        return list(self._out.get(node_id, ()))

    def nodes_near(
        self, geometry: Geometry | None, radius_m: float, node_classes: Sequence[NodeClass] | None = None
    ) -> list[GraphNode]:
        out = []
        for node in self.nodes.values():
            if node_classes and node.node_class not in node_classes:
                continue
            d = distance_m(node.geometry, geometry)
            if d <= radius_m:
                out.append(node)
        return out

    def nodes_intersecting(
        self, geometry: Geometry | None, node_classes: Sequence[NodeClass] | None = None
    ) -> list[GraphNode]:
        if geometry is None:
            return []
        return [
            node
            for node in self.nodes.values()
            if (not node_classes or node.node_class in node_classes)
            and node.geometry is not None
            and intersects(node.geometry, geometry)
        ]

    def propagate(
        self,
        seed_node_ids: Iterable[str],
        max_depth: int = 3,
        max_weight: float = 6.0,
        domains: Sequence[InfrastructureDomain] | None = None,
        edge_kinds: Sequence[EdgeKind] | None = None,
    ) -> list[PropagationHop]:
        """Breadth-first weighted propagation.

        Weight accumulates along the path, so a distant node reached by a long
        chain scores lower than a near one. This is what distinguishes
        "affected by association" from "merely nearby".
        """
        hops: list[PropagationHop] = []
        visited: dict[str, float] = {}
        queue: deque[tuple[str, int, float, tuple[str, ...]]] = deque()

        for seed in seed_node_ids:
            if seed not in self.nodes:
                continue
            queue.append((seed, 0, 0.0, (seed,)))
            visited[seed] = 0.0
            hops.append(
                PropagationHop(
                    node_id=seed,
                    node_class=self.nodes[seed].node_class,
                    domain=_domain_of(self.nodes[seed]),
                    depth=0,
                    path=(seed,),
                    cumulative_weight=0.0,
                )
            )

        while queue:
            current, depth, weight, path = queue.popleft()
            if depth >= max_depth:
                continue
            for edge_id in self._out_edges.get(current, ()):
                edge = self.edges[edge_id]
                if edge_kinds and edge.kind not in edge_kinds:
                    continue
                target = edge.target_node_id
                if target not in self.nodes or target in path:
                    continue
                new_weight = weight + edge.weight
                if new_weight > max_weight:
                    continue
                node = self.nodes[target]
                domain = _domain_of(node)
                if domains and domain not in domains:
                    continue
                previous = visited.get(target)
                if previous is not None and previous <= new_weight:
                    continue
                visited[target] = new_weight
                hops.append(
                    PropagationHop(
                        node_id=target,
                        node_class=node.node_class,
                        domain=domain,
                        depth=depth + 1,
                        path=(*path, target),
                        cumulative_weight=round(new_weight, 4),
                    )
                )
                queue.append((target, depth + 1, new_weight, (*path, target)))

        return sorted(hops, key=lambda h: (h.depth, h.cumulative_weight))

    def dependency_chain(self, user_route_node_ids: Sequence[str]) -> list[GraphNode]:
        """Section 14: which infrastructure nodes does this user depend upon?"""
        out: list[GraphNode] = []
        for node_id in user_route_node_ids:
            node = self.nodes.get(node_id)
            if node:
                out.append(node)
        return out

    def stats(self) -> dict[str, Any]:
        by_class: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            by_class[str(node.node_class)] += 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "nodes_by_class": dict(sorted(by_class.items())),
        }

    def __len__(self) -> int:
        return len(self.nodes)


_DOMAIN_BY_CLASS = {
    NodeClass.ROAD_SEGMENT: InfrastructureDomain.ROAD,
    NodeClass.INTERSECTION: InfrastructureDomain.ROAD,
    NodeClass.BRIDGE: InfrastructureDomain.ROAD,
    NodeClass.TUNNEL: InfrastructureDomain.ROAD,
    NodeClass.TRANSIT_ROUTE: InfrastructureDomain.TRANSIT,
    NodeClass.TRANSIT_STOP: InfrastructureDomain.TRANSIT,
    NodeClass.STATION: InfrastructureDomain.TRANSIT,
    NodeClass.FERRY_TERMINAL: InfrastructureDomain.FERRIES,
    NodeClass.HOSPITAL: InfrastructureDomain.PUBLIC_FACILITY,
    NodeClass.SCHOOL: InfrastructureDomain.PUBLIC_FACILITY,
    NodeClass.EMERGENCY_FACILITY: InfrastructureDomain.PUBLIC_SAFETY,
    NodeClass.UTILITY_AREA: InfrastructureDomain.UTILITY,
    NodeClass.USER_ROUTE_SEGMENT: InfrastructureDomain.ROAD,
}


def _domain_of(node: GraphNode) -> InfrastructureDomain:
    return _DOMAIN_BY_CLASS.get(node.node_class, InfrastructureDomain.UNKNOWN)


def build_puget_sound_graph() -> InfrastructureGraph:
    """A small but structurally honest downtown Seattle network.

    Enough topology to demonstrate that propagation works through connected
    infrastructure rather than a radius. Replace with the OSM/GTFS import in
    production; the interface does not change.
    """
    from ..domain.geo import line_string, point, polygon

    graph = InfrastructureGraph()

    def road(node_id: str, name: str, coords: list[tuple[float, float]], arterial: bool) -> None:
        graph.add_node(
            GraphNode(
                node_id=node_id,
                node_class=NodeClass.ROAD_SEGMENT,
                name=name,
                geometry=line_string([list(c) for c in coords]),
                attributes={"arterial": arterial, "classification": "arterial" if arterial else "local"},
            )
        )

    def intersection(node_id: str, lonlat: tuple[float, float]) -> None:
        graph.add_node(
            GraphNode(
                node_id=node_id,
                node_class=NodeClass.INTERSECTION,
                geometry=point(*lonlat),
                attributes={"signalised": True},
            )
        )

    # 4th Ave (arterial, the march route)
    road(
        "road:4th-ave",
        "4th Ave",
        [(-122.3380, 47.6200), (-122.3370, 47.6162), (-122.3370, 47.6090), (-122.3368, 47.6020)],
        arterial=True,
    )
    road(
        "road:3rd-ave",
        "3rd Ave",
        [(-122.3400, 47.6180), (-122.3385, 47.6080), (-122.3382, 47.6010)],
        arterial=True,
    )
    road(
        "road:5th-ave",
        "5th Ave",
        [(-122.3345, 47.6175), (-122.3342, 47.6095), (-122.3340, 47.6020)],
        arterial=True,
    )
    road(
        "road:pine-st",
        "Pine St",
        [(-122.3390, 47.6120), (-122.3300, 47.6105), (-122.3220, 47.6095)],
        arterial=False,
    )
    road(
        "road:university-st",
        "University St",
        [(-122.3380, 47.6075), (-122.3300, 47.6060), (-122.3130, 47.6100)],
        arterial=True,
    )
    road(
        "road:seneca-st",
        "Seneca St",
        [(-122.3395, 47.6055), (-122.3300, 47.6050), (-122.3180, 47.6075)],
        arterial=False,
    )
    road(
        "road:i5-downtown",
        "I-5 (downtown)",
        [(-122.3330, 47.6250), (-122.3345, 47.6100), (-122.3340, 47.5950)],
        arterial=True,
    )

    for node_id, lonlat in {
        "int:4th-union": (-122.3370, 47.6090),
        "int:4th-westlake": (-122.3370, 47.6162),
        "int:3rd-pike": (-122.3385, 47.6080),
        "int:5th-pike": (-122.3342, 47.6095),
        "int:4th-pine": (-122.3370, 47.6120),
        "int:university-4th": (-122.3380, 47.6075),
        "int:seneca-4th": (-122.3380, 47.6055),
        "int:pine-3rd": (-122.3390, 47.6100),
    }.items():
        intersection(node_id, lonlat)

    # Branches
    graph.connect("road:4th-ave", "int:4th-westlake", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:4th-ave", "int:4th-union", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:4th-ave", "int:4th-pine", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:4th-ave", "int:seneca-4th", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:3rd-ave", "int:3rd-pike", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:3rd-ave", "int:seneca-4th", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:5th-ave", "int:5th-pike", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:5th-ave", "int:4th-union", EdgeKind.INTERSECTS, 1.0)
    graph.connect("road:pine-st", "int:4th-pine", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:pine-st", "int:pine-3rd", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:university-st", "int:university-4th", EdgeKind.CONNECTS_TO, 1.0)
    graph.connect("road:university-st", "int:seneca-4th", EdgeKind.INTERSECTS, 1.0)
    graph.connect("int:4th-union", "int:3rd-pike", EdgeKind.CONNECTS_TO, 0.5)
    graph.connect("int:4th-union", "int:5th-pike", EdgeKind.CONNECTS_TO, 0.5)
    graph.connect("int:4th-union", "int:pine-3rd", EdgeKind.CONNECTS_TO, 0.5)
    graph.connect("int:3rd-pike", "int:pine-3rd", EdgeKind.CONNECTS_TO, 0.5)
    graph.connect("int:4th-union", "road:i5-downtown", EdgeKind.CONNECTS_TO, 1.5)
    graph.connect("road:i5-downtown", "road:university-st", EdgeKind.CONNECTS_TO, 1.5)

    # Bridges / bottleneck
    graph.add_node(
        GraphNode(
            node_id="bridge:montlake",
            node_class=NodeClass.BRIDGE,
            name="Montlake Bridge",
            geometry=point(-122.3300, 47.6440),
            attributes={"bottleneck": True},
        )
    )
    graph.connect("road:i5-downtown", "bridge:montlake", EdgeKind.DEPENDS_ON, 2.0)

    # Transit
    graph.add_node(
        GraphNode(
            node_id="transit:route-40",
            node_class=NodeClass.TRANSIT_ROUTE,
            name="Route 40 - UW/Seattle Center-Downtown-Ballard",
            geometry=line_string(
                [
                    [-122.3130, 47.6100],
                    [-122.3250, 47.6120],
                    [-122.3370, 47.6090],
                    [-122.3420, 47.6090],
                ]
            ),
            attributes={"agency": "King County Metro", "headway_min": 12},
        )
    )
    graph.add_node(
        GraphNode(
            node_id="transit:route-7",
            node_class=NodeClass.TRANSIT_ROUTE,
            name="Route 7 - Downtown Seattle-Rainier Beach",
            geometry=line_string(
                [
                    [-122.3370, 47.6090],
                    [-122.3300, 47.5990],
                    [-122.3120, 47.5900],
                ]
            ),
            attributes={"agency": "King County Metro", "headway_min": 15},
        )
    )
    for stop_id, name, lonlat in (
        ("stop:westlake", "Westlake Station", (-122.3370, 47.6162)),
        ("stop:union-4th", "4th & Union", (-122.3370, 47.6090)),
        ("stop:pike-3rd", "3rd & Pike", (-122.3385, 47.6080)),
        ("stop:uw", "University of Washington Station", (-122.3130, 47.6100)),
    ):
        graph.add_node(
            GraphNode(
                node_id=stop_id,
                node_class=NodeClass.TRANSIT_STOP,
                name=name,
                geometry=point(*lonlat),
            )
        )
    graph.connect("transit:route-40", "stop:union-4th", EdgeKind.SERVES, 0.4)
    graph.connect("transit:route-40", "stop:westlake", EdgeKind.SERVES, 0.4)
    graph.connect("transit:route-40", "stop:pike-3rd", EdgeKind.SERVES, 0.4)
    graph.connect("transit:route-40", "stop:uw", EdgeKind.SERVES, 0.4)
    graph.connect("transit:route-40", "road:4th-ave", EdgeKind.DEPENDS_ON, 1.0)
    graph.connect("transit:route-40", "road:university-st", EdgeKind.DEPENDS_ON, 1.0)
    graph.connect("transit:route-7", "stop:union-4th", EdgeKind.SERVES, 0.4)
    graph.connect("transit:route-7", "stop:pike-3rd", EdgeKind.SERVES, 0.4)
    graph.connect("transit:route-7", "road:3rd-ave", EdgeKind.DEPENDS_ON, 1.0)

    # Critical facilities
    graph.add_node(
        GraphNode(
            node_id="facility:hospital-harborview",
            node_class=NodeClass.HOSPITAL,
            name="Harborview Medical Center",
            geometry=point(-122.3080, 47.6025),
            attributes={"trauma_level": 1, "beds": 413},
        )
    )
    graph.add_node(
        GraphNode(
            node_id="facility:fire-station-6",
            node_class=NodeClass.EMERGENCY_FACILITY,
            name="Fire Station 6",
            geometry=point(-122.3290, 47.6140),
        )
    )
    graph.add_node(
        GraphNode(
            node_id="facility:fire-station-5",
            node_class=NodeClass.EMERGENCY_FACILITY,
            name="Fire Station 5",
            geometry=point(-122.3130, 47.6190),
        )
    )
    graph.add_node(
        GraphNode(
            node_id="facility:school-garfield",
            node_class=NodeClass.SCHOOL,
            name="Garfield High School",
            geometry=point(-122.3170, 47.6180),
            attributes={"enrolment": 1300},
        )
    )
    graph.add_node(
        GraphNode(
            node_id="facility:school-chief-seattle",
            node_class=NodeClass.SCHOOL,
            name="Chief Seattle High School",
            geometry=point(-122.3120, 47.5930),
        )
    )
    graph.add_node(
        GraphNode(
            node_id="utility:pioneer-square",
            node_class=NodeClass.UTILITY_AREA,
            name="Seattle City Light - Pioneer Square",
            geometry=polygon(
                [
                    [-122.3450, 47.6020],
                    [-122.3330, 47.6020],
                    [-122.3330, 47.6100],
                    [-122.3450, 47.6100],
                    [-122.3450, 47.6020],
                ]
            ),
        )
    )

    # Emergency access: facilities depend on the road network.
    for facility_id, via in (
        ("facility:fire-station-6", "road:university-st"),
        ("facility:fire-station-5", "road:i5-downtown"),
        ("facility:hospital-harborview", "road:i5-downtown"),
        ("facility:school-garfield", "road:university-st"),
        ("facility:school-chief-seattle", "road:i5-downtown"),
    ):
        graph.connect(facility_id, via, EdgeKind.DEPENDS_ON, 1.0)
    graph.connect("facility:fire-station-6", "facility:hospital-harborview", EdgeKind.CONNECTS_TO, 2.0)
    graph.connect("utility:pioneer-square", "road:seneca-st", EdgeKind.NEAR, 0.5)

    return graph


__all__ = ["InfrastructureGraph", "PropagationHop", "build_puget_sound_graph", "field"]
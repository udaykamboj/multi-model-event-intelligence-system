"""Routing engine and alternate route verification (brief section 29).

Section 29 requirements:
  - Abstract routing provider interface:
      route()
      alternatives()
      isochrone()
      apply_closures()
      travel_time()
  - Dynamic road closures and penalties must be applied to the route graph.
  - "A user recommendation cannot simply say: Take Route B. The system must verify
     Route B does not intersect another current disruption."
"""

from __future__ import annotations

import heapq
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.enums import InfrastructureDomain, NodeClass
from ..domain.geo import distance_m, line_string
from ..domain.schemas import Geometry, GraphNode
from .model import InfrastructureGraph

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteLeg:
    from_node_id: str
    to_node_id: str
    distance_m: float
    travel_time_min: float


@dataclass(frozen=True)
class RoutingResult:
    """The result of a route calculation or alternative recommendation."""

    path_node_ids: tuple[str, ...]
    distance_m: float
    travel_time_min: float
    geometry: Geometry | None = None
    is_alternative: bool = False
    verified_clear_of_disruptions: bool = True
    intersecting_disruptions: tuple[str, ...] = ()
    additional_travel_time_min: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "path_node_ids": list(self.path_node_ids),
            "distance_m": round(self.distance_m, 1),
            "travel_time_min": round(self.travel_time_min, 1),
            "additional_travel_time_min": round(self.additional_travel_time_min, 1),
            "geometry": self.geometry,
            "is_alternative": self.is_alternative,
            "verified_clear_of_disruptions": self.verified_clear_of_disruptions,
            "intersecting_disruptions": list(self.intersecting_disruptions),
        }


class RoutingProvider(ABC):
    """Abstract contract for route calculation and alternative verification."""

    @abstractmethod
    def route(
        self, origin_node_id: str, destination_node_id: str, preferences: dict[str, Any] | None = None
    ) -> RoutingResult | None: ...

    @abstractmethod
    def alternatives(
        self,
        origin_node_id: str,
        destination_node_id: str,
        max_routes: int = 3,
        avoid_disruptions: bool = True,
    ) -> list[RoutingResult]: ...

    @abstractmethod
    def apply_closures(self, closed_node_ids: Sequence[str], penalty_weight: float = 1000.0) -> None: ...

    @abstractmethod
    def clear_closures(self) -> None: ...

    @abstractmethod
    def travel_time(self, path_node_ids: Sequence[str]) -> float: ...

    @abstractmethod
    def isochrone(self, origin_node_id: str, minutes: float) -> list[str]: ...


class GraphRoutingEngine(RoutingProvider):
    """Deterministic routing provider operating directly over the InfrastructureGraph."""

    def __init__(self, graph: InfrastructureGraph) -> None:
        self.graph = graph
        self._closure_penalties: dict[str, float] = {}
        self._active_closures: set[str] = set()

    def apply_closures(self, closed_node_ids: Sequence[str], penalty_weight: float = 1000.0) -> None:
        """Section 29: apply dynamic road closures and penalties to the route graph."""
        for nid in closed_node_ids:
            self._active_closures.add(nid)
            self._closure_penalties[nid] = penalty_weight

    def clear_closures(self) -> None:
        self._active_closures.clear()
        self._closure_penalties.clear()

    def route(
        self, origin_node_id: str, destination_node_id: str, preferences: dict[str, Any] | None = None
    ) -> RoutingResult | None:
        """Find the optimal route between origin and destination."""
        if origin_node_id not in self.graph.nodes or destination_node_id not in self.graph.nodes:
            return None

        path, cost = self._dijkstra(origin_node_id, destination_node_id, avoid_closed=False)
        if not path:
            return None

        time_min = self.travel_time(path)
        dist_m = self._path_distance(path)
        geom = self._path_geometry(path)

        # Check intersections with active closures
        hits = tuple(sorted(set(path) & self._active_closures))

        return RoutingResult(
            path_node_ids=tuple(path),
            distance_m=dist_m,
            travel_time_min=time_min,
            geometry=geom,
            is_alternative=False,
            verified_clear_of_disruptions=len(hits) == 0,
            intersecting_disruptions=hits,
            additional_travel_time_min=0.0,
        )

    def alternatives(
        self,
        origin_node_id: str,
        destination_node_id: str,
        max_routes: int = 3,
        avoid_disruptions: bool = True,
    ) -> list[RoutingResult]:
        """Find alternative routes and verify each does NOT intersect active disruptions.

        Section 29:
        "The system must verify Route B does not intersect another current disruption."
        """
        if origin_node_id not in self.graph.nodes or destination_node_id not in self.graph.nodes:
            return []

        # Find primary base route first
        primary = self.route(origin_node_id, destination_node_id)
        base_time = primary.travel_time_min if primary else 10.0

        candidates: list[RoutingResult] = []
        excluded_edges: set[str] = set()

        for _ in range(max_routes * 2):
            path, cost = self._dijkstra(
                origin_node_id,
                destination_node_id,
                avoid_closed=avoid_disruptions,
                excluded_edges=excluded_edges,
            )
            if not path:
                break

            hits = tuple(sorted(set(path) & self._active_closures))
            if avoid_disruptions and hits:
                # Discard any candidate that intersects active closures!
                continue

            time_min = self.travel_time(path)
            dist_m = self._path_distance(path)
            geom = self._path_geometry(path)
            add_time = max(0.0, time_min - base_time)

            res = RoutingResult(
                path_node_ids=tuple(path),
                distance_m=dist_m,
                travel_time_min=time_min,
                geometry=geom,
                is_alternative=True,
                verified_clear_of_disruptions=len(hits) == 0,
                intersecting_disruptions=hits,
                additional_travel_time_min=round(add_time, 1),
            )

            # Prevent duplicate paths
            if not any(c.path_node_ids == res.path_node_ids for c in candidates):
                candidates.append(res)
                if len(candidates) >= max_routes:
                    break

            # Penalize edges in found path to force alternative diversity
            if len(path) > 2:
                mid_src = path[len(path) // 2]
                mid_dst = path[len(path) // 2 + 1]
                excluded_edges.add(f"{mid_src}->{mid_dst}")

        return candidates

    def travel_time(self, path_node_ids: Sequence[str]) -> float:
        """Estimate travel time in minutes based on segment distances and arterial speed."""
        if len(path_node_ids) < 2:
            return 1.0

        total_min = 0.0
        for i in range(len(path_node_ids) - 1):
            src = self.graph.nodes.get(path_node_ids[i])
            dst = self.graph.nodes.get(path_node_ids[i + 1])
            if src and dst and src.geometry and dst.geometry:
                dist = distance_m(src.geometry, dst.geometry)
                is_arterial = src.attributes.get("arterial") or dst.attributes.get("arterial")
                # Average speeds: 35 km/h for arterial (~583 m/min), 20 km/h local (~333 m/min)
                speed_m_per_min = 550.0 if is_arterial else 350.0
                total_min += max(0.5, dist / speed_m_per_min)
            else:
                total_min += 1.5

        return round(total_min, 1)

    def isochrone(self, origin_node_id: str, minutes: float) -> list[str]:
        """Find all nodes reachable within given travel time."""
        if origin_node_id not in self.graph.nodes:
            return []

        reachable: list[str] = [origin_node_id]
        dist_heap: list[tuple[float, str]] = [(0.0, origin_node_id)]
        visited: dict[str, float] = {origin_node_id: 0.0}

        while dist_heap:
            cur_time, cur_node = heapq.heappop(dist_heap)
            if cur_time > minutes:
                continue

            for neighbor in self.graph.neighbours(cur_node):
                if neighbor in self._active_closures:
                    continue
                step_time = self.travel_time([cur_node, neighbor])
                new_time = cur_time + step_time
                if new_time <= minutes and (neighbor not in visited or new_time < visited[neighbor]):
                    visited[neighbor] = new_time
                    heapq.heappush(dist_heap, (new_time, neighbor))
                    if neighbor not in reachable:
                        reachable.append(neighbor)

        return reachable

    def _dijkstra(
        self,
        start: str,
        goal: str,
        avoid_closed: bool = True,
        excluded_edges: set[str] | None = None,
    ) -> tuple[list[str], float]:
        queue: list[tuple[float, str, list[str]]] = [(0.0, start, [start])]
        visited: dict[str, float] = {start: 0.0}
        excluded = excluded_edges or set()

        while queue:
            cost, current, path = heapq.heappop(queue)
            if current == goal:
                return path, cost

            for neighbor in self.graph.neighbours(current):
                edge_key = f"{current}->{neighbor}"
                if edge_key in excluded:
                    continue

                is_closed = neighbor in self._active_closures
                if avoid_closed and is_closed:
                    continue

                penalty = self._closure_penalties.get(neighbor, 0.0)
                edge_cost = 1.0 + penalty
                new_cost = cost + edge_cost

                if neighbor not in visited or new_cost < visited[neighbor]:
                    visited[neighbor] = new_cost
                    heapq.heappush(queue, (new_cost, neighbor, [*path, neighbor]))

        return [], float("inf")

    def _path_distance(self, path: list[str]) -> float:
        total = 0.0
        for i in range(len(path) - 1):
            s = self.graph.nodes.get(path[i])
            d = self.graph.nodes.get(path[i + 1])
            if s and d and s.geometry and d.geometry:
                total += distance_m(s.geometry, d.geometry)
        return total

    def _path_geometry(self, path: list[str]) -> Geometry | None:
        coords: list[list[float]] = []
        for nid in path:
            node = self.graph.nodes.get(nid)
            if node and node.geometry:
                geom = node.geometry
                if geom.get("type") == "Point":
                    coords.append(geom["coordinates"])
                elif geom.get("type") == "LineString":
                    coords.extend(geom["coordinates"])
        if len(coords) >= 2:
            return line_string([tuple(c) for c in coords])
        return None


__all__ = ["GraphRoutingEngine", "RouteLeg", "RoutingProvider", "RoutingResult"]

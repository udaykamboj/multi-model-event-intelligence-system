"""Tests for open-source intelligence components (Design Platform §2, §3, §10, §13).

Validates:
1. NetworkX-backed graph algorithms:
   - shortest path with haversine edge costs
   - connected components connectivity ratio
   - corridor detour calculation when critical nodes/corridors are blocked
2. Ruptures-backed statistical change point and numpy trend detection:
   - PELT change point detection on time-series regime shifts
   - linear trend rate-of-change calculation
   - graceful fallback on insufficient data (< 8 points)
3. Shapely-backed GEOS geometry operations:
   - polygon containment and intersection
   - linestring buffering and intersection
   - accurate metric area and distance calculations
"""

from __future__ import annotations

from datetime import datetime, timedelta
import numpy as np
import pytest

from infraimpact.analysis.capabilities import CapabilityContext
from infraimpact.analysis.statistics import (
    StatisticalChangeCapability,
    detect_change_points,
    linear_trend,
)
from infraimpact.domain.enums import Authority, InfrastructureDomain, ObservationType, SourceType
from conftest import line, point

from infraimpact.domain.geo import (
    Geometry,
    buffer_geometry,
    contains,
    distance_m,
    geometry_area_m2,
    intersects,
)
from infraimpact.domain.ids import utcnow
from infraimpact.domain.schemas import (
    Claim,
    EventState,
    MovementState,
    Observation,
    ObservationQuality,
    Provenance,
)
from infraimpact.graph.model import InfrastructureGraph, build_puget_sound_graph


# ============================================================================
# 1. NetworkX Graph Algorithms
# ============================================================================


class TestNetworkXGraphIntelligence:
    def test_puget_sound_baseline_connectivity(self):
        graph = build_puget_sound_graph()
        base_conn = graph.connectivity_ratio()
        assert 0.0 < base_conn <= 1.0
        assert graph.reachable_count() >= 26

    def test_bridge_severance_drops_connectivity(self):
        graph = build_puget_sound_graph()
        base_conn = graph.connectivity_ratio()

        # Sever key arterial intersection and highway
        blocked = {"int:4th-union", "road:i5-downtown"}
        sev_conn = graph.connectivity_ratio(blocked_node_ids=blocked)
        sev_reach = graph.reachable_count(blocked_node_ids=blocked)

        assert sev_conn < base_conn
        assert sev_reach < len(graph.nodes)

    def test_corridor_detour_computation(self):
        graph = build_puget_sound_graph()

        # Block a major corridor node
        detour = graph.corridor_detour(["int:seneca-4th", "int:4th-pine"])
        assert detour["alternate_available"] is True
        assert detour["nominal_distance_m"] > 0
        assert detour["detour_distance_m"] >= detour["nominal_distance_m"]
        assert detour["detour_percentage"] >= 0
        assert len(detour["detour_path"]) >= 2
        # Verify blocked nodes are not in detour path
        for b in ["int:seneca-4th", "int:4th-pine"]:
            assert b not in detour["detour_path"]

    def test_networkx_shortest_path_metric(self):
        graph = build_puget_sound_graph()
        path, dist = graph.shortest_path("int:seneca-4th", "int:4th-pine")
        assert len(path) >= 2
        assert dist > 0.0


# ============================================================================
# 2. Ruptures Change-Point Detection & Statistical Trend
# ============================================================================


class TestRupturesStatisticalIntelligence:
    def test_detect_change_points_with_regime_shift(self):
        # 10 points at 40 mph, followed by 10 points at 12 mph
        series = [40.0 + (i % 3) * 0.5 for i in range(10)] + [12.0 - (i % 2) * 0.5 for i in range(10)]
        bkps = detect_change_points(series, penalty=2.0)
        assert len(bkps) >= 1
        # The change point should be near index 10
        assert any(8 <= b <= 12 for b in bkps)

    def test_detect_change_points_short_series_returns_empty(self):
        short = [30.0, 31.0, 29.0]
        assert detect_change_points(short) == []

    def test_linear_trend_slope(self):
        # Exactly 5 units per hour drop
        times = [i * 3600.0 for i in range(5)]
        vals = [50.0 - 5.0 * i for i in range(5)]
        slope = linear_trend(times, vals)
        assert pytest.approx(slope, rel=1e-3) == -5.0

    def test_statistical_capability_full_run(self):
        now = utcnow()
        observations = []
        for i in range(12):
            t = now - timedelta(minutes=(12 - i) * 5)
            # Speed drops from 35 to 8
            spd = 35.0 if i < 6 else 8.0
            obs = Observation(
                observation_id=f"obs_stat_{i}",
                source_id="synthetic.puget_sound",
                source_record_id=f"rec_{i}",
                event_time=t,
                observed_at=t,
                ingested_at=t,
                source_type=SourceType.OFFICIAL_MACHINE_READABLE,
                observation_type=ObservationType.TRAFFIC_FLOW,
                geometry=Geometry(type="Point", coordinates=(-122.335, 47.608)),
                structured_payload={"speed_mph": spd},
                provenance=Provenance(authority=Authority.OFFICIAL, retrieval_method="feed", content_hash=f"h{i}"),
                quality=ObservationQuality(source_reliability=0.9, spatial_precision=0.9, temporal_precision=0.9, extraction_confidence=0.9),
            )
            observations.append(obs)

        state = EventState(
            event_id="evt_stat_test",
            state_version=1,
            event_type_distribution={"protest": 1.0},
            status="active",
            geometry=Geometry(type="Point", coordinates=(-122.335, 47.608)),
            first_observed=observations[0].observed_at,
            last_observed=observations[-1].observed_at,
            movement=MovementState(moving=False, confidence=0.8),
        )

        cap = StatisticalChangeCapability()
        ctx = CapabilityContext(
            region_id="puget-sound",
            state=state,
            observations=observations,
            claims=[],
        )
        app, _ = cap.is_applicable(ctx)
        assert app is True
        result = cap.run(ctx)
        assert "traffic_change_points" in result.features
        assert result.features["traffic_regime_shift"].value["mean_before"] > result.features["traffic_regime_shift"].value["mean_after"]
        assert "traffic_trend_per_hour" in result.features
        assert result.features["traffic_trend_per_hour"].value < 0.0

    def test_statistical_capability_insufficient_data(self):
        now = utcnow()
        obs = [
            Observation(
                observation_id="obs_single",
                source_id="synthetic.puget_sound",
                source_record_id="rec_single",
                event_time=now,
                observed_at=now,
                ingested_at=now,
                source_type=SourceType.OFFICIAL_MACHINE_READABLE,
                observation_type=ObservationType.TRAFFIC_FLOW,
                geometry=Geometry(type="Point", coordinates=(-122.335, 47.608)),
                structured_payload={"speed_mph": 20.0},
                provenance=Provenance(authority=Authority.OFFICIAL, retrieval_method="feed", content_hash="h"),
                quality=ObservationQuality(source_reliability=0.9, spatial_precision=0.9, temporal_precision=0.9, extraction_confidence=0.9),
            )
        ]
        state = EventState(
            event_id="evt_stat_test2",
            state_version=1,
            event_type_distribution={"protest": 1.0},
            status="active",
            geometry=Geometry(type="Point", coordinates=(-122.335, 47.608)),
            first_observed=now,
            last_observed=now,
            movement=MovementState(moving=False, confidence=0.8),
        )
        cap = StatisticalChangeCapability()
        ctx = CapabilityContext(
            region_id="puget-sound",
            state=state,
            observations=obs,
            claims=[],
        )
        result = cap.run(ctx)
        assert result.features["statistics_status"].value == "insufficient_data"


# ============================================================================
# 3. Shapely GEOS Parity & Operations
# ============================================================================


class TestShapelyGEOSIntelligence:
    def test_polygon_buffering_and_intersection(self):
        p = point(-122.335, 47.608)
        buffered = buffer_geometry(p, 250.0)
        assert buffered["type"] == "Polygon"
        area = geometry_area_m2(buffered)
        # Expected pi * 250^2 ~ 196,350 m2
        assert 170_000 < area < 230_000

        # An intersecting road line passing through the buffer
        road = line((-122.336, 47.608), (-122.334, 47.608))
        assert intersects(road, buffered) is True

        # A distant road line
        far_road = line((-122.400, 47.608), (-122.405, 47.608))
        assert intersects(far_road, buffered) is False
        assert distance_m(far_road, buffered) > 4000.0

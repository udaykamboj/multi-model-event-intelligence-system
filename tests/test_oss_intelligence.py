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


# ============================================================================
# 4. Shapely STRtree Spatial Indexing
# ============================================================================


class TestSTRtreeSpatialIndexing:
    def test_spatial_index_intersects_and_distance(self):
        from infraimpact.domain.geo import SpatialIndex, point, polygon

        idx: SpatialIndex[str] = SpatialIndex()
        idx.insert("p1", point(-122.335, 47.608), "downtown_seattle")
        idx.insert("p2", point(-122.315, 47.615), "capitol_hill")
        idx.insert("p3", point(-122.120, 47.674), "redmond")

        assert len(idx) == 3

        # Intersects query with bounding polygon around downtown
        downtown_poly = polygon([
            [-122.340, 47.600],
            [-122.330, 47.600],
            [-122.330, 47.615],
            [-122.340, 47.615],
            [-122.340, 47.600],
        ])
        results = idx.query_intersects(downtown_poly)
        assert results == ["downtown_seattle"]

        # Within distance query (3000m from downtown should include downtown & capitol hill, but not redmond)
        near = idx.query_within_distance(point(-122.335, 47.608), radius_m=3000.0)
        assert "downtown_seattle" in near
        assert "capitol_hill" in near
        assert "redmond" not in near

        # Nearest neighbor query
        nearest = idx.query_nearest(point(-122.125, 47.670))
        assert nearest == "redmond"

    def test_graph_uses_spatial_index_for_fast_queries(self):
        graph = build_puget_sound_graph()
        p = point(-122.335, 47.608)

        # Query nodes near downtown Seattle
        near_nodes = graph.nodes_near(p, radius_m=800.0)
        assert len(near_nodes) >= 3
        assert all(distance_m(n.geometry, p) <= 800.0 for n in near_nodes)

        # Nodes intersecting
        downtown_poly = buffer_geometry(p, 300.0)
        intersecting = graph.nodes_intersecting(downtown_poly)
        assert len(intersecting) >= 1
        assert all(intersects(n.geometry, downtown_poly) for n in intersecting)


# ============================================================================
# 5. Scikit-Learn Historical NearestNeighbors
# ============================================================================


class TestHistoricalNearestNeighbors:
    def test_nearest_neighbors_retrieval(self):
        from infraimpact.analysis.similarity import HistoricalSimilarityEngine

        engine = HistoricalSimilarityEngine()
        analogues = engine.find_analogues(
            target_features={
                "crowd_estimate": 1500,
                "duration_hours": 4.0,
                "arterial_overlap_count": 2,
                "transit_route_overlap": 4,
                "moving": 1.0,
                "rush_hour": 1.0,
            },
            centroid=(-122.3370, 47.6115),
            top_k=2,
        )

        assert len(analogues) == 2
        # Downtown Seattle 2020 Westlake demonstration is the exact closest match
        assert analogues[0].event_id == "hist_sea_2020_0530"
        assert analogues[0].similarity_score > 0.85
        assert len(analogues[0].observed_trajectory) >= 2


# ============================================================================
# 6. Bayesian Evidence Fusion & Source Reliability
# ============================================================================


class TestBayesianEvidenceIntelligence:
    def test_bayesian_tracker_updating(self):
        from infraimpact.analysis.uncertainty import BayesianSourceReliabilityTracker

        tracker = BayesianSourceReliabilityTracker()
        # Official source baseline reliability
        r_init = tracker.expected_reliability("wsdot.alerts", Authority.OFFICIAL)
        assert r_init >= 0.90

        # Unverified source baseline
        r_unv = tracker.expected_reliability("social.anon", Authority.UNVERIFIED)
        assert r_unv < 0.50

        # Register confirmations
        tracker.register_observation("social.anon", Authority.UNVERIFIED, confirmed=True, weight=10.0)
        r_updated = tracker.expected_reliability("social.anon", Authority.UNVERIFIED)
        assert r_updated > r_unv

    def test_bayesian_evidence_fusion_probabilities(self):
        from infraimpact.analysis.uncertainty import fuse_evidence_probabilities

        # Single moderate source -> slight increase
        single = fuse_evidence_probabilities([(True, 0.70)], prior_probability=0.5)
        assert 0.65 < single < 0.75

        # Three independent reliable sources confirming -> high confidence
        corroborated = fuse_evidence_probabilities(
            [(True, 0.85), (True, 0.80), (True, 0.90)], prior_probability=0.5
        )
        assert corroborated > 0.95

        # Contradicted by authoritative source
        conflicted = fuse_evidence_probabilities(
            [(True, 0.60), (False, 0.90)], prior_probability=0.5
        )
        assert conflicted < 0.25


# ============================================================================
# 7. DBSCAN Spatiotemporal Observation Clustering
# ============================================================================


class TestDBSCANSpatiotemporalClustering:
    def test_cluster_burst_observations(self):
        from infraimpact.events.resolver import cluster_unresolved_observations

        t0 = datetime.fromisoformat("2026-10-04T12:00:00+00:00")
        t_soon = t0 + timedelta(minutes=5)
        t_later = t0 + timedelta(hours=8)

        # Three observations in downtown Seattle within 5 minutes
        obs1 = Observation(
            observation_id="c_obs_1",
            source_id="news.1",
            source_record_id="r1",
            source_type=SourceType.ESTABLISHED_NEWS,
            observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
            observed_at=t0,
            ingested_at=t0,
            event_time=t0,
            geometry=point(-122.335, 47.608),
            provenance=Provenance(authority=Authority.ESTABLISHED_MEDIA, content_hash="h1"),
            quality=ObservationQuality(source_reliability=0.8),
        )
        obs2 = Observation(
            observation_id="c_obs_2",
            source_id="police.cad",
            source_record_id="r2",
            source_type=SourceType.OFFICIAL_MACHINE_READABLE,
            observation_type=ObservationType.POLICE_RESPONSE,
            observed_at=t_soon,
            ingested_at=t_soon,
            event_time=t_soon,
            geometry=point(-122.336, 47.609),
            provenance=Provenance(authority=Authority.OFFICIAL, content_hash="h2"),
            quality=ObservationQuality(source_reliability=0.9),
        )
        # One observation 20 km away in Bellevue
        obs3 = Observation(
            observation_id="c_obs_3",
            source_id="wsdot.east",
            source_record_id="r3",
            source_type=SourceType.OFFICIAL_MACHINE_READABLE,
            observation_type=ObservationType.ROAD_CLOSURE,
            observed_at=t0,
            ingested_at=t0,
            event_time=t0,
            geometry=point(-122.190, 47.610),
            provenance=Provenance(authority=Authority.OFFICIAL, content_hash="h3"),
            quality=ObservationQuality(source_reliability=0.9),
        )

        clusters = cluster_unresolved_observations([obs1, obs2, obs3], eps_m=1000.0)
        assert len(clusters) == 2

        # Cluster 0 should contain obs1 and obs2 together
        downtown_cluster = next(c for c in clusters if len(c.observations) == 2)
        assert {"c_obs_1", "c_obs_2"} == {o.observation_id for o in downtown_cluster.observations}
        assert downtown_cluster.centroid is not None
        assert abs(downtown_cluster.centroid[0] - (-122.3355)) < 0.01

        # Cluster 1 contains the distant Bellevue observation
        bellevue_cluster = next(c for c in clusters if len(c.observations) == 1)
        assert bellevue_cluster.observations[0].observation_id == "c_obs_3"

"""Tests for comprehensive intermediate analytical metrics tracking (Design Platform §10, §17, §18, §21-32, §43-47).

Validates that the system explicitly tracks, stores, and exposes:
1. Event / Situation Analysis
2. Infrastructure Impact (categorized by roads, highways, bridges, drawbridges, transit, ferries, work zones, facilities)
3. Traffic Analysis (speed, anomaly, delay, congestion)
4. Transit Analysis (routes, stops, alerts, cancellations, alternates)
5. Geographic / Network Analysis (connectivity, blocked edges, detours, cascades)
6. Temporal Analysis (age, rate of change, acceleration trend, horizons)
7. Historical Analysis (analogues, similarity, consequences, deviations)
8. Prediction Analysis (Models A-G, complete metadata, MODEL_REQUIRED when untrained)
9. Confidence / Evidence (unbundled multi-dimensional decomposition)
10. State Delta Analysis (previous -> current -> delta breakdown)
And validates public and internal API exposure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
import pytest
from starlette.testclient import TestClient

from infraimpact.analysis.metrics import (
    ConfidenceBreakdown,
    EventAnalysisMetrics,
    GeographicNetworkAnalysis,
    HistoricalAnalysis,
    InfrastructureImpactAnalysis,
    InfrastructureItem,
    PredictionAnalysis,
    PredictionModelRecord,
    SituationAnalysis,
    StateDeltaAnalysis,
    StateDeltaRecord,
    TemporalAnalysis,
    TrafficAnalysis,
    TransitAnalysis,
    derive_analysis_metrics,
)
from infraimpact.delta.engine import DeltaReport, StateDeltaEngine
from infraimpact.domain.enums import (
    Authority,
    InfrastructureDomain,
    ObservationType,
    SourceType,
    TruthStatus,
    Urgency,
)
from infraimpact.domain.geo import Geometry
from infraimpact.domain.ids import utcnow
from infraimpact.domain.schemas import (
    AffectedInfrastructure,
    AnalysisRun,
    Claim,
    EventState,
    FeatureValue,
    Forecast,
    ModelOutput,
    MovementState,
    Observation,
    ObservationQuality,
    StateDelta,
)
from infraimpact.graph.model import InfrastructureGraph, NodeClass
from infraimpact.models.base import ModelContext, ModelTaskType
from infraimpact.models.portfolio import build_default_model_registry


def _make_sample_state(
    event_id: str = "evt_metrics_1",
    state_version: int = 2,
    event_type: str = "protest",
    lon: float = -122.335,
    lat: float = 47.608,
) -> EventState:
    now = utcnow()
    return EventState(
        event_id=event_id,
        state_version=state_version,
        event_type_distribution={event_type: 0.88, "demonstration": 0.12},
        status="active",
        geometry=Geometry(type="Point", coordinates=(lon, lat)),
        geometry_confidence=0.92,
        first_observed=now - timedelta(hours=2),
        last_observed=now,
        movement=MovementState(
            moving=True,
            direction_deg=180.0,
            speed_estimate_m_per_min=30.0,
            confidence=0.85,
        ),
        affected_infrastructure=(
            AffectedInfrastructure(
                domain=InfrastructureDomain.ROAD,
                identifier="road:4th-ave",
                name="4th Avenue Arterial",
                geometry=Geometry(type="Point", coordinates=(lon, lat)),
                severity=Urgency.HIGH,
                truth_status=TruthStatus.CONFIRMED,
            ),
            AffectedInfrastructure(
                domain=InfrastructureDomain.ROAD,
                identifier="road:fremont-bridge",
                name="Fremont Bridge Drawbridge",
                geometry=Geometry(type="Point", coordinates=(lon + 0.01, lat + 0.01)),
                severity=Urgency.MODERATE,
                truth_status=TruthStatus.INFERRED,
            ),
            AffectedInfrastructure(
                domain=InfrastructureDomain.TRANSIT,
                identifier="route:kcm-7",
                name="King County Metro Route 7",
                geometry=Geometry(type="Point", coordinates=(lon, lat)),
                severity=Urgency.HIGH,
                truth_status=TruthStatus.CONFIRMED,
            ),
            AffectedInfrastructure(
                domain=InfrastructureDomain.PUBLIC_FACILITY,
                identifier="facility:harborview",
                name="Harborview Medical Center",
                geometry=Geometry(type="Point", coordinates=(lon + 0.005, lat - 0.005)),
                severity=Urgency.MODERATE,
                truth_status=TruthStatus.CONFIRMED,
            ),
        ),
    )


def _make_sample_graph() -> InfrastructureGraph:
    from infraimpact.domain.schemas import GraphNode
    g = InfrastructureGraph()
    g.add_node(GraphNode(node_id="road:4th-ave", node_class=NodeClass.ROAD_SEGMENT, name="4th Avenue", attributes={"domain": "roads"}))
    g.add_node(GraphNode(node_id="road:fremont-bridge", node_class=NodeClass.BRIDGE, name="Fremont Bridge", attributes={"domain": "roads"}))
    g.add_node(GraphNode(node_id="route:kcm-7", node_class=NodeClass.TRANSIT_ROUTE, name="KCM Route 7", attributes={"domain": "transit"}))
    g.add_node(GraphNode(node_id="facility:harborview", node_class=NodeClass.HOSPITAL, name="Harborview Medical", attributes={"domain": "public_safety"}))
    return g


def _make_observation(
    observation_id: str,
    observation_type: ObservationType = ObservationType.ROAD_CLOSURE,
    headline: str = "road closure",
    observed_at: datetime | None = None,
) -> Observation:
    from infraimpact.domain.schemas import Provenance
    now = observed_at or utcnow()
    return Observation(
        observation_id=observation_id,
        source_id="synthetic.puget_sound",
        source_record_id=f"rec_{observation_id}",
        event_time=now,
        observed_at=now,
        ingested_at=now,
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=observation_type,
        geometry=Geometry(type="Point", coordinates=(-122.335, 47.608)),
        headline=headline,
        provenance=Provenance(
            authority=Authority.OFFICIAL,
            retrieval_method="feed",
            content_hash="dummy_hash",
        ),
        quality=ObservationQuality(
            source_reliability=0.95,
            spatial_precision=0.90,
            temporal_precision=0.85,
            extraction_confidence=0.92,
        ),
    )


class TestAnalysisMetricsDimensions:
    """Test that all 10 analytical dimensions are derived accurately with full mathematical depth."""

    def test_derive_all_10_dimensions(self):
        state = _make_sample_state()
        prev_state = _make_sample_state(state_version=1)
        prev_state.movement.speed_estimate_m_per_min = 10.0

        obs1 = _make_observation(
            "obs_m_1",
            ObservationType.ROAD_CLOSURE,
            observed_at=utcnow() - timedelta(minutes=45),
        )
        obs2 = _make_observation(
            "obs_m_2",
            ObservationType.TRANSIT_SERVICE_ALERT,
            headline="Route 7 rolling reroute in effect",
            observed_at=utcnow() - timedelta(minutes=10),
        )

        claims = [
            Claim(
                claim_id="clm_1",
                observation_id="obs_m_1",
                event_id=state.event_id,
                predicate="road_blocked",
                value={"subject": "road:4th-ave", "status": "blocked"},
                extraction_confidence=0.92,
            )
        ]

        delta_report = DeltaReport(
            deltas=(
                StateDelta(
                    change="traffic_anomaly_detected",
                    domain="traffic",
                    before="normal",
                    after="congested",
                    magnitude=0.75,
                    confidence=0.88,
                    causes=("obs_m_1",),
                ),
            ),
            causes=("obs_m_1",),
            magnitude=0.75,
            novelty=0.7,
            confidence=0.88,
            is_material=True,
        )

        features = {
            "event_class": FeatureValue(name="event_class", value="protest"),
            "event_class_confidence": FeatureValue(name="event_class_confidence", value=0.91),
            "crowd_estimate": FeatureValue(name="crowd_estimate", value=750.0),
            "historical_similarity_score": FeatureValue(name="historical_similarity_score", value=0.84),
            "historical_consequences": FeatureValue(name="historical_consequences", value=["transit_reroute", "corridor_delay"]),
            "historical_outcome_frequencies": FeatureValue(name="historical_outcome_frequencies", value={"transit_reroute": 0.85, "corridor_delay": 0.70}),
            "historical_baseline_conditions": FeatureValue(name="historical_baseline_conditions", value={"typical_volume_vph": 1200.0, "typical_speed_mph": 28.0}),
            "historical_deviations": FeatureValue(name="historical_deviations", value={"delay_increase_pct": 45.0, "speed_drop_pct": 35.0}),
            "historical_analogues": FeatureValue(name="historical_analogues", value=[{"event_id": "hist_01", "similarity": 0.84}]),
            "propagation_reach": FeatureValue(name="propagation_reach", value=3.0),
            "route_redundancy": FeatureValue(name="route_redundancy", value=2.0),
            "traffic_current_speed_mph": FeatureValue(name="traffic_current_speed_mph", value=12.5),
            "traffic_expected_baseline_speed_mph": FeatureValue(name="traffic_expected_baseline_speed_mph", value=28.0),
            "traffic_speed_anomaly_ratio": FeatureValue(name="traffic_speed_anomaly_ratio", value=0.45),
            "traffic_delay_seconds": FeatureValue(name="traffic_delay_seconds", value=420.0),
            "traffic_delay_percentage": FeatureValue(name="traffic_delay_percentage", value=122.0),
            "traffic_congestion_level": FeatureValue(name="traffic_congestion_level", value="SEVERE"),
            "traffic_affected_road_segments": FeatureValue(name="traffic_affected_road_segments", value=["road:4th-ave"]),
            "network_connectivity_ratio": FeatureValue(name="network_connectivity_ratio", value=0.8),
            "reachable_infrastructure_count": FeatureValue(name="reachable_infrastructure_count", value=4),
            "blocked_edges": FeatureValue(name="blocked_edges", value=["road:4th-ave"]),
            "alternate_routes_available": FeatureValue(name="alternate_routes_available", value=True),
            "detour_distance_m": FeatureValue(name="detour_distance_m", value=350.0),
            "detour_percentage": FeatureValue(name="detour_percentage", value=18.5),
            "transit_alternate_recommendations": FeatureValue(name="transit_alternate_recommendations", value=["Link Light Rail 1 Line", "Route 40"]),
        }

        registry = build_default_model_registry()
        model_ctx = ModelContext(
            event_id=state.event_id,
            state_version=state.state_version,
            features=features,
            state=state,
            observations=[obs1, obs2],
        )

        # Run portfolio models
        model_outputs = []
        for task_type in ModelTaskType:
            champ = registry.get_champion(task_type)
            if champ:
                pred = champ.predict(model_ctx)
                model_outputs.append(champ.to_model_output(pred, state.state_version))

        graph = _make_sample_graph()

        metrics = derive_analysis_metrics(
            state=state,
            previous_state=prev_state,
            observations=[obs1, obs2],
            claims=claims,
            impacts=list(state.affected_infrastructure),
            forecasts=[],
            delta_report=delta_report,
            features=features,
            model_outputs=model_outputs,
            graph=graph,
            model_registry=registry,
            notes=["4th Ave march moving south towards Pioneer Square"],
            analysis_run_id="run_test_123",
        )

        assert isinstance(metrics, EventAnalysisMetrics)
        assert metrics.event_id == state.event_id
        assert metrics.analysis_run_id == "run_test_123"

        # 1. Situation Analysis verification
        sit = metrics.situation
        assert sit.event_class == "protest"
        assert sit.classification_confidence >= 0.90
        assert sit.geographic_footprint_m2 > 0
        assert sit.radius_m > 0
        assert sit.is_moving is True
        assert sit.speed_mps == 0.5  # 30 m/min / 60
        assert sit.heading_degrees == 180.0
        assert sit.movement_vector[1] < 0  # 180 deg moves South (negative y)
        assert sit.event_size_estimate == 750
        assert sit.duration_hours >= 1.9
        assert len(sit.trajectory_points) >= 1
        assert sit.historical_similarity_score == 0.84

        # 2. Infrastructure Impact verification
        infra = metrics.infrastructure_impact
        assert infra.total_affected_count == 4
        assert len(infra.roads) >= 1
        assert len(infra.drawbridges) >= 1
        assert any(db.name == "Fremont Bridge Drawbridge" for db in infra.drawbridges)
        assert any(r.name == "4th Avenue Arterial" for r in infra.roads)
        assert len(infra.transit_routes) >= 1
        assert any(tr.name == "King County Metro Route 7" for tr in infra.transit_routes)
        assert len(infra.critical_facilities) >= 1
        assert any(cf.name == "Harborview Medical Center" for cf in infra.critical_facilities)
        assert infra.max_severity in {"high", "immediate"}

        # 3. Traffic Analysis verification
        traf = metrics.traffic
        assert traf.current_speed_mph is not None
        assert traf.expected_baseline_speed_mph is not None
        assert traf.speed_anomaly_ratio is not None
        assert traf.delay_seconds is not None
        assert traf.delay_percentage is not None
        assert traf.congestion_level in {"NORMAL", "MINOR", "MODERATE", "SEVERE", "GRIDLOCK"}

        # 4. Transit Analysis verification
        trans = metrics.transit
        assert len(trans.service_alerts) >= 1
        assert "route:kcm-7" in trans.affected_routes
        assert len(trans.alternate_transit_recommendations) >= 1
        assert any("Link Light Rail" in alt for alt in trans.alternate_transit_recommendations)

        # 5. Geographic Network Analysis verification
        geo = metrics.geographic_network
        assert len(geo.affected_geographic_areas) >= 1
        assert geo.network_connectivity_ratio > 0.0
        assert geo.reachable_infrastructure_count >= 4
        assert len(geo.blocked_edges) >= 1
        assert geo.detour_distance_m > 0
        assert geo.detour_percentage > 0
        assert geo.network_propagation_reach_hops == 3

        # 6. Temporal Analysis verification
        temp = metrics.temporal
        assert temp.first_observation is not None
        assert temp.latest_observation is not None
        assert temp.state_version == 2
        assert temp.previous_state_version == 1
        assert temp.state_delta_count == 1
        assert temp.acceleration_trend in {"accelerating", "decelerating", "steady"}
        assert temp.prediction_horizons_minutes == [5, 15, 30, 60]

        # 7. Historical Analysis verification
        hist = metrics.historical
        assert hist.similarity_score > 0.0
        assert len(hist.outcome_frequencies) > 0
        assert "typical_volume_vph" in hist.baseline_conditions
        assert "delay_increase_pct" in hist.deviations_from_baseline

        # 8. Prediction Analysis verification
        pred = metrics.predictions
        assert pred.total_models_evaluated >= 7
        assert pred.champion_models_count >= 7
        # Untrained models must explicitly record MODEL_REQUIRED (zero fake predictions)
        for m in pred.models:
            assert isinstance(m, PredictionModelRecord)
            assert m.model_status in {"COMPLETED", "MODEL_REQUIRED"}
            if m.is_placeholder:
                assert m.model_status == "MODEL_REQUIRED"
        assert pred.trained_models_count + len(pred.models_requiring_training) == pred.total_models_evaluated

        # 9. Confidence Breakdown verification
        conf = metrics.confidence
        assert isinstance(conf, ConfidenceBreakdown)
        assert 0.0 <= conf.source_authority_score <= 1.0
        assert 0.0 <= conf.source_reliability_score <= 1.0
        assert 0.0 <= conf.freshness_score <= 1.0
        assert 0.0 <= conf.composite_confidence <= 1.0
        assert len(conf.dimension_weights) >= 4

        # 10. State Delta Analysis verification
        delta_a = metrics.state_delta
        assert delta_a.previous_state_version == 1
        assert delta_a.current_state_version == 2
        assert delta_a.delta_count == 1
        assert delta_a.is_material is True
        assert len(delta_a.deltas) == 1
        assert delta_a.deltas[0].change == "traffic_anomaly_detected"

    def test_model_required_for_untrained_models(self):
        """Zero fake predictions: models that are not trained must explicitly record MODEL_REQUIRED."""
        state = _make_sample_state()
        registry = build_default_model_registry()
        model_ctx = ModelContext(
            event_id=state.event_id,
            state_version=state.state_version,
            features={},
            state=state,
            observations=[],
        )

        outputs = []
        for task_type in ModelTaskType:
            champ = registry.get_champion(task_type)
            if champ:
                p = champ.predict(model_ctx)
                outputs.append(champ.to_model_output(p, state.state_version))

        delta_report = DeltaReport()
        metrics = derive_analysis_metrics(
            state=state,
            previous_state=None,
            observations=[],
            claims=[],
            impacts=[],
            forecasts=[],
            delta_report=delta_report,
            features={},
            model_outputs=outputs,
            model_registry=registry,
        )

        for model_rec in metrics.predictions.models:
            if model_rec.is_placeholder:
                assert model_rec.model_status == "MODEL_REQUIRED"
                assert "MODEL_REQUIRED" in model_rec.explanation or "Placeholder" in model_rec.explanation


@pytest.fixture
def app_client(repo, bus):
    from infraimpact.api.app import create_app
    from infraimpact.config import Settings
    from infraimpact.domain.schemas import UserContext

    repo.events.ensure("evt_1", datetime.now(UTC), "puget-sound")
    obs = _make_observation("obs_1", ObservationType.ROAD_CLOSURE, headline="I-5 closed downtown")
    repo.observations.append(obs)
    repo.events.link_observation("evt_1", "obs_1")
    state = _make_sample_state(event_id="evt_1")
    repo.states.append_state(state)
    repo.users.upsert(
        UserContext(
            user_id="u_downtown",
            current_location=Geometry(type="Point", coordinates=(-122.335, 47.608)),
        )
    )
    settings = Settings(database_url="sqlite:///unused.db", region_id="puget-sound")
    with TestClient(create_app(settings, repo, bus, run_loop=False)) as client:
        yield client


class TestMetricsApiEndpoints:
    """Test that analytical metrics are exposed via dedicated and internal endpoints."""

    def test_analysis_endpoint_includes_metrics(self, app_client: TestClient):
        # Trigger an on-demand analysis via internal endpoint
        res = app_client.post("/internal/analysis/request", json={"event_id": "evt_1"})
        assert res.status_code == 200
        data = res.json()
        assert "metrics" in data
        assert data["metrics"] is not None
        assert data["metrics"]["event_id"] == "evt_1"
        assert "situation" in data["metrics"]
        assert "infrastructure_impact" in data["metrics"]
        assert "traffic" in data["metrics"]
        assert "transit" in data["metrics"]
        assert "predictions" in data["metrics"]
        assert "confidence" in data["metrics"]

        # GET /v1/events/{event_id}/analysis exposes metrics on each run
        analysis_res = app_client.get("/v1/events/evt_1/analysis")
        assert analysis_res.status_code == 200
        analysis_data = analysis_res.json()
        assert analysis_data["count"] >= 1
        latest_run = analysis_data["runs"][0]
        assert "metrics" in latest_run
        assert latest_run["metrics"] is not None
        assert latest_run["metrics"]["event_id"] == "evt_1"

    def test_dedicated_metrics_endpoint(self, app_client: TestClient):
        # First ensure an analysis run has completed
        app_client.post("/internal/analysis/request", json={"event_id": "evt_1"})

        # GET /v1/events/{event_id}/metrics
        res = app_client.get("/v1/events/evt_1/metrics")
        assert res.status_code == 200
        data = res.json()
        assert data["event_id"] == "evt_1"
        assert "analysis_run_id" in data
        metrics = data["metrics"]
        assert metrics["event_id"] == "evt_1"
        assert "situation" in metrics
        assert "infrastructure_impact" in metrics
        assert "traffic" in metrics
        assert "transit" in metrics
        assert "geographic_network" in metrics
        assert "temporal" in metrics
        assert "historical" in metrics
        assert "predictions" in metrics
        assert "confidence" in metrics
        assert "state_delta" in metrics

    def test_internal_analysis_deep_inspection(self, app_client: TestClient):
        # Ensure analysis run exists
        app_client.post("/internal/analysis/request", json={"event_id": "evt_1"})

        # GET /internal/analysis/{event_id}
        res = app_client.get("/internal/analysis/evt_1")
        assert res.status_code == 200
        deep = res.json()
        assert deep["event_id"] == "evt_1"
        assert "features" in deep
        assert "model_outputs" in deep
        assert "metrics" in deep
        assert deep["metrics"] is not None
        assert "capabilities_invoked" in deep
        assert "hypotheses" in deep
        assert "provenance" in deep["metrics"]
        assert "supporting_sources" in deep["metrics"]["provenance"]


class TestMultiSystemIntelligenceRoles:
    """Verifies that each intelligence system performs its designated role per Design Platform MD."""

    def test_graph_geospatial_deterministic_computation(self):
        """Graph / Geo must compute actual shortest path detours and connectivity ratios."""
        from infraimpact.graph.model import build_puget_sound_graph

        graph = build_puget_sound_graph()
        # Normal path from Seneca to Pine via 4th Ave
        path, cost = graph.shortest_path("int:seneca-4th", "int:4th-pine", blocked_node_ids=None)
        assert path is not None
        assert "road:4th-ave" in path

        # Detour path when 4th Ave is blocked by an event (routes via 3rd Ave and Pine St)
        detour_path, detour_cost = graph.shortest_path(
            "int:seneca-4th", "int:4th-pine", blocked_node_ids={"road:4th-ave"}
        )
        assert detour_path is not None
        assert "road:4th-ave" not in detour_path
        # Detour cost should be greater than or equal to nominal path cost
        assert detour_cost >= cost
        assert "road:3rd-ave" in detour_path

        # Detour computation wrapper
        detour_info = graph.compute_detour(
            "int:seneca-4th", "int:4th-pine", blocked_node_ids={"road:4th-ave"}
        )
        assert detour_info["alternate_available"] is True
        assert detour_info["detour_distance_m"] >= 0.0
        assert detour_info["nominal_distance_m"] > 0.0

        # Connectivity ratio calculation
        conn = graph.connectivity_ratio({"road:4th-ave"})
        assert 0.0 < conn < 1.0

    def test_traffic_analysis_capability_computes_features(self):
        """Traffic analysis computes speed and delay from reported measurements.

        Absolute speeds and travel times require the feed to report them, plus a
        free-flow reference to compare against. A bare ratio is enough to
        classify congestion but not enough to state a speed or a delay, and
        those are not inferred from a generic default.
        """
        from infraimpact.analysis.capabilities import CapabilityContext
        from infraimpact.analysis.prediction import TrafficAnomalyCapability

        cap = TrafficAnomalyCapability()
        state = _make_sample_state()
        obs = [
            _make_observation("obs_t1", ObservationType.TRAFFIC_FLOW),
            _make_observation("obs_t2", ObservationType.ROAD_CLOSURE),
            _make_observation("obs_t3", ObservationType.TRAVEL_TIME),
        ]
        obs[0].structured_payload["speed_ratio"] = 0.30
        obs[0].structured_payload["speed_mph"] = 15.0
        obs[0].structured_payload["free_flow_speed_mph"] = 50.0
        obs[2].structured_payload["travel_time_seconds"] = 900.0
        obs[2].structured_payload["free_flow_travel_time_seconds"] = 300.0

        ctx = CapabilityContext(
            region_id="puget-sound",
            state=state,
            observations=obs,
            claims=[],
        )
        res = cap.run(ctx)
        assert "traffic_current_speed_mph" in res.features
        assert "traffic_expected_baseline_speed_mph" in res.features
        assert "traffic_delay_seconds" in res.features
        assert "traffic_congestion_level" in res.features
        assert res.features["traffic_current_speed_mph"].value == 15.0
        assert res.features["traffic_expected_baseline_speed_mph"].value == 50.0
        # Speed ratio 0.30 should classify as GRIDLOCK
        assert res.features["traffic_congestion_level"].value == "GRIDLOCK"
        # 900s observed against a 300s free flow.
        assert res.features["traffic_delay_seconds"].value == 600.0

    def test_traffic_with_no_measurement_invents_nothing(self):
        """A closure with no traffic data must not become a traffic observation.

        This capability used to report 0.45 x baseline at confidence 0.70
        whenever a road closure was present, and to name "road:4th-ave" when no
        segment had been identified. Both were fabrications that were
        indistinguishable from measurements, in the one domain where acting on a
        wrong number sends someone the wrong way.
        """
        from infraimpact.analysis.capabilities import CapabilityContext
        from infraimpact.analysis.prediction import TrafficAnomalyCapability

        cap = TrafficAnomalyCapability()
        state = _make_sample_state()
        obs = [_make_observation("obs_c1", ObservationType.ROAD_CLOSURE)]

        ctx = CapabilityContext(
            region_id="puget-sound",
            state=state,
            observations=obs,
            claims=[],
        )
        res = cap.run(ctx)

        # Nothing measured, so nothing measured-shaped is emitted.
        assert res.features["traffic_measurement_available"].value is False
        assert res.features["traffic_confidence"].value == 0.0
        for fabricated in (
            "traffic_observed_speed_ratio",
            "traffic_anomaly",
            "traffic_current_speed_mph",
            "traffic_expected_baseline_speed_mph",
            "traffic_delay_seconds",
            "traffic_delay_percentage",
            "traffic_travel_time_seconds",
            "traffic_congestion_level",
        ):
            assert fabricated not in res.features, f"{fabricated} was invented"

        # Segments are reported only when the state carries real ones. The
        # previous code fell back to the literal "road:4th-ave" whenever the
        # state had none, so the identifier appeared whether or not anything had
        # observed it.
        segments = res.features["traffic_affected_road_segments"].value
        expected_ids = [
            i.identifier
            for i in state.affected_infrastructure
            if i.domain == InfrastructureDomain.ROAD
        ]
        assert segments == expected_ids

        # A closure with nothing observed and no affected infrastructure: the
        # capability used to name a street here out of thin air.
        bare = _make_sample_state().model_copy(
            update={"affected_infrastructure": ()}
        )
        bare_res = cap.run(
            CapabilityContext(region_id="puget-sound", state=bare, observations=obs, claims=[])
        )
        assert "traffic_affected_road_segments" not in bare_res.features

        # The expectation is still reportable; it is an expectation.
        assert "traffic_baseline_expected_ratio" in res.features

        output = res.model_outputs[0].output
        assert output["status"] == "NO_MEASUREMENT"
        assert output["observed_ratio"] is None
        assert output["congestion_level"] is None
        assert output["road_closure_present"] is True
        assert output["measurement_available"] is False

        assert any("not reportable" in note or "no traffic measurement" in note for note in res.notes)

    def test_transit_disruption_capability_computes_features(self):
        """Transit capability must compute routes, delays, alerts, and alternate corridors."""
        from infraimpact.analysis.capabilities import CapabilityContext
        from infraimpact.analysis.transit import TransitDisruptionCapability

        cap = TransitDisruptionCapability()
        state = _make_sample_state()
        obs = [
            _make_observation("obs_tr1", ObservationType.TRANSIT_SERVICE_ALERT, headline="Route 7 reroute downtown"),
            _make_observation("obs_tr2", ObservationType.VEHICLE_POSITION),
        ]
        ctx = CapabilityContext(
            region_id="puget-sound",
            state=state,
            observations=obs,
            claims=[],
        )
        res = cap.run(ctx)
        assert "transit_vehicle_locations_count" in res.features
        assert "transit_service_alerts" in res.features
        assert "transit_mean_delay_seconds" in res.features
        assert "transit_alternate_recommendations" in res.features
        assert len(res.features["transit_service_alerts"].value) == 1

    def test_historical_similarity_computes_consequences_and_frequencies(self):
        """Historical retrieval must output structured consequences and outcome frequencies."""
        from infraimpact.analysis.capabilities import CapabilityContext
        from infraimpact.analysis.similarity import HistoricalSimilarityCapability

        cap = HistoricalSimilarityCapability()
        state = _make_sample_state()
        ctx = CapabilityContext(
            region_id="puget-sound",
            state=state,
            observations=[_make_observation("obs_h1", ObservationType.ROAD_CLOSURE)],
            claims=[],
        )
        res = cap.run(ctx)
        assert "historical_similarity_score" in res.features
        assert "historical_consequences" in res.features
        assert "historical_outcome_frequencies" in res.features
        assert "historical_mean_duration_hours" in res.features
        freqs = res.features["historical_outcome_frequencies"].value
        assert "transit_reroute" in freqs
        assert "highway_ramp_closure" in freqs

    def test_provenance_answers_traceability_questions(self):
        """Provenance must answer all 10 traceability questions required by Design Platform MD."""
        state = _make_sample_state()
        obs = [_make_observation("obs_p1", ObservationType.ROAD_CLOSURE)]
        claims = [Claim(
            claim_id="clm_p1",
            observation_id="obs_p1",
            predicate="road_status",
            value="closed",
            source_id="synthetic.puget_sound",
            truth_status=TruthStatus.CONFIRMED,
        )]
        delta = StateDeltaEngine().compare(None, state)
        metrics = derive_analysis_metrics(
            state=state,
            previous_state=None,
            observations=obs,
            claims=claims,
            impacts=list(state.affected_infrastructure),
            forecasts=[],
            delta_report=delta,
            features={"traffic_current_speed_mph": FeatureValue(name="traffic_current_speed_mph", value=15.0)},
            model_outputs=[],
            notes=["[traffic_anomaly] Anomaly detected", "llm: advised capability prioritization"],
            analysis_run_id="run_prov_test",
            jev_decisions={"primary_impact_domain": {"decision": "road"}},
        )

        prov = metrics.provenance
        # 1. Why does the system believe this?
        assert len(prov.supporting_sources) >= 1
        # 2. What changed?
        assert metrics.state_delta.is_material is True
        # 3. Which sources supported it?
        assert "synthetic.puget_sound" in prov.supporting_sources
        # 4. Which capabilities produced this?
        assert "traffic_anomaly" in prov.capabilities_used
        # 5. Did the LLM contribute?
        assert any("llm" in c.lower() for c in prov.llm_contributions)
        # 6. Did Jev make a decision?
        assert "primary_impact_domain" in prov.jev_decisions_summary
        # 7. What deterministic/geospatial calculations were performed?
        assert len(prov.deterministic_calculations) >= 3
        # 8. How confident is the result?
        assert metrics.confidence.composite_confidence > 0.0
        # 9. How did this affect the user?
        assert "affected" in prov.user_impact_summary


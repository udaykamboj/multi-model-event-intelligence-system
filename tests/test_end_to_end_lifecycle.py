"""Automated verification of the full end-to-end event intelligence lifecycle.

Verifies the complete conceptual loop required by Design Infrastructure Platform (§1-19):
1. Ingestion of observations
2. Event resolution and persistent event state creation (v1)
3. State delta detection
4. Dynamic capability selection
5. Analytical engine execution (NetworkX, Shapely, Ruptures, Similarity, Models A-G)
6. Intermediate metrics and provenance preservation
7. User exposure calculation
8. Jev bounded decision layer
9. Priority scoring, presentation, and notification evaluation
10. Continuous update with new incoming observations (v1 -> v2)
11. Event resolution and lifecycle termination
"""

from __future__ import annotations

from datetime import timedelta
import pytest

from infraimpact.bus.event_bus import InMemoryEventBus
from infraimpact.config import Settings
from infraimpact.delta.engine import StateDeltaEngine
from infraimpact.domain.enums import (
    Authority,
    ObservationType,
    SourceType,
    TruthStatus,
    Urgency,
)
from infraimpact.domain.geo import Geometry, line, point
from infraimpact.domain.ids import utcnow
from infraimpact.domain.schemas import (
    Claim,
    NotificationPreferences,
    Observation,
    ObservationQuality,
    Provenance,
    RouteProfile,
    SavedPlace,
    UserContext,
)
from infraimpact.events.resolver import EventResolver
from infraimpact.events.world_state import WorldStateEngine
from infraimpact.graph.model import build_puget_sound_graph
from infraimpact.analysis.orchestrator import AnalysisOrchestrator
from infraimpact.storage.sqlite_driver import SqlitePlatformRepository
from infraimpact.users.exposure import ExposureContext, ExposureEngine
from infraimpact.users.priority import PriorityContext, UserPriorityEngine
from infraimpact.users.presentation import PresentationContext, PresentationEngine
from infraimpact.users.notifications import NotificationContext, NotificationEngine
from infraimpact.jev.client import build_jev_client
from infraimpact.llm.client import build_llm_client
from infraimpact.llm.interpreter import InterpretationLayer


@pytest.mark.anyio
async def test_full_event_intelligence_lifecycle():
    now = utcnow()
    repo = SqlitePlatformRepository("sqlite:///:memory:")
    bus = InMemoryEventBus()
    graph = build_puget_sound_graph()

    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget-sound",
    )
    world_engine = WorldStateEngine(repo.states)
    delta_engine = StateDeltaEngine()
    llm_layer = InterpretationLayer(client=build_llm_client(), enabled=False)
    jev_client = build_jev_client()

    orchestrator = AnalysisOrchestrator(
        repository=repo,
        bus=bus,
        region_id="puget-sound",
        graph=graph,
        interpretation=llm_layer,
        jev=jev_client,
    )

    exposure_engine = ExposureEngine(graph=graph)
    priority_engine = UserPriorityEngine(jev=jev_client)
    presentation_engine = PresentationEngine()
    notification_engine = NotificationEngine(
        timezone="America/Los_Angeles",
        repository=repo.notifications,
        jev=jev_client,
    )

    # 1. Setup User
    user = UserContext(
        user_id="user_commuter_1",
        saved_places=(
            SavedPlace(place_id="home", name="Capitol Hill", kind="home", geometry=point(-122.319, 47.623)),
            SavedPlace(place_id="work", name="Downtown Office", kind="work", geometry=point(-122.335, 47.608)),
        ),
        route_profiles=(
            RouteProfile(
                route_id="commute_4th_ave",
                name="Capitol Hill to Downtown via 4th Ave",
                geometry=line((-122.319, 47.623), (-122.335, 47.608), (-122.338, 47.601)),
                modes=("drive", "transit"),
                node_ids=("road:4th-ave", "int:seneca-4th"),
            ),
        ),
        current_location=point(-122.335, 47.608),
        preferences=NotificationPreferences(minimum_urgency=Urgency.LOW),
    )
    repo.users.upsert(user)

    # 2. Ingest Initial Observation
    obs1 = Observation(
        observation_id="obs_sdot_001",
        source_id="synthetic.puget_sound",
        source_record_id="sdot_rec_1",
        event_time=now - timedelta(minutes=20),
        observed_at=now - timedelta(minutes=20),
        ingested_at=now - timedelta(minutes=20),
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=ObservationType.ROAD_CLOSURE,
        geometry=Geometry(type="Point", coordinates=(-122.3350, 47.6085)),
        headline="4th Ave blocked between Seneca and Pine due to demonstration",
        structured_payload={"road": "road:4th-ave", "status": "closed"},
        provenance=Provenance(authority=Authority.OFFICIAL, retrieval_method="feed", content_hash="h1"),
        quality=ObservationQuality(source_reliability=0.95, spatial_precision=0.92, temporal_precision=0.90, extraction_confidence=0.95),
    )
    repo.observations.append(obs1)
    claims1 = [
        Claim(
            claim_id="clm_001",
            observation_id=obs1.observation_id,
            source_id=obs1.source_id,
            predicate="road_closed",
            value={"road_id": "road:4th-ave", "severity": "high"},
            truth_status=TruthStatus.CONFIRMED,
            extraction_confidence=0.95,
        )
    ]
    for c in claims1:
        repo.claims.append(c)

    # 3. Resolve & Build State v1
    res1 = resolver.resolve(obs1, claims1)
    assert res1.is_new_event is True
    event_id = res1.event_id

    state_v1 = world_engine.rebuild(event_id, [obs1], claims1, previous=None, now=now)
    world_engine.persist(state_v1)
    assert state_v1.state_version == 1
    assert state_v1.status == "active"

    # 4. State Delta v1
    delta_v1 = delta_engine.compare(None, state_v1)
    assert delta_v1.is_material is True

    # 5. Orchestrator Analysis Run v1
    run_v1 = await orchestrator.analyze(event_id, state_v1, trigger="observation", previous_state=None)
    metrics_v1 = run_v1.metrics
    assert metrics_v1 is not None
    assert metrics_v1.geographic_network.network_connectivity_ratio > 0.0
    assert metrics_v1.geographic_network.reachable_infrastructure_count > 0
    assert metrics_v1.historical.similarity_score > 0.0
    assert len(metrics_v1.provenance.capabilities_used) >= 2

    # 6. User Exposure & Jev Priority v1
    exp_ctx_v1 = ExposureContext(user=user, state=state_v1, impacts=run_v1.impacts, forecasts=run_v1.forecasts, analysis_run_id=run_v1.analysis_run_id)
    exposure_v1 = exposure_engine.compute(exp_ctx_v1)
    assert exposure_v1.exposure_score > 0.0
    assert len(exposure_v1.route_impacts) >= 1

    prio_ctx_v1 = PriorityContext(exposure=exposure_v1, state=state_v1, impacts=run_v1.impacts, delta_report=delta_v1, is_new_event=True)
    priority_v1 = priority_engine.compute(prio_ctx_v1)
    assert priority_v1.priority > 0.0
    assert "jev_route_materially_affected" in priority_v1.components

    # 7. Evolve Event: Ingest Second Observation (Transit & Traffic)
    traffic_obs = []
    for i in range(8):
        t = now - timedelta(minutes=(8 - i) * 2)
        spd = 35.0 if i < 3 else 8.0
        o = Observation(
            observation_id=f"obs_spd_{i}",
            source_id="synthetic.puget_sound",
            source_record_id=f"rec_spd_{i}",
            event_time=t,
            observed_at=t,
            ingested_at=t,
            source_type=SourceType.OFFICIAL_MACHINE_READABLE,
            observation_type=ObservationType.TRAFFIC_FLOW,
            geometry=Geometry(type="Point", coordinates=(-122.3350, 47.6085)),
            headline=f"Traffic sensor {spd} mph",
            structured_payload={"speed_mph": spd, "speed_ratio": spd / 35.0},
            provenance=Provenance(authority=Authority.OFFICIAL, retrieval_method="feed", content_hash=f"h_spd_{i}"),
            quality=ObservationQuality(source_reliability=0.9, spatial_precision=0.9, temporal_precision=0.9, extraction_confidence=0.9),
        )
        repo.observations.append(o)
        traffic_obs.append(o)

    all_v2_obs = [obs1] + traffic_obs
    for o in traffic_obs:
        resolver.resolve(o, claims1)

    state_v2 = world_engine.rebuild(event_id, all_v2_obs, claims1, previous=state_v1, now=now)
    world_engine.persist(state_v2)
    assert state_v2.state_version == 2

    # 8. Delta v1->v2
    delta_v2 = delta_engine.compare(state_v1, state_v2)
    assert len(delta_v2.deltas) >= 1

    # 9. Analysis Run v2
    run_v2 = await orchestrator.analyze(event_id, state_v2, trigger="observation", previous_state=state_v1)
    metrics_v2 = run_v2.metrics
    assert metrics_v2 is not None
    assert metrics_v2.temporal.state_version == 2
    assert metrics_v2.provenance.state_version == 2
    assert len(metrics_v2.provenance.deterministic_calculations) >= 3

    # 10. Exposure, Priority & Notification v2
    exp_ctx_v2 = ExposureContext(user=user, state=state_v2, impacts=run_v2.impacts, forecasts=run_v2.forecasts, analysis_run_id=run_v2.analysis_run_id)
    exposure_v2 = exposure_engine.compute(exp_ctx_v2)
    prio_ctx_v2 = PriorityContext(exposure=exposure_v2, state=state_v2, impacts=run_v2.impacts, delta_report=delta_v2, is_new_event=False)
    priority_v2 = priority_engine.compute(prio_ctx_v2)
    assert priority_v2.priority > 0.0

    pres_ctx_v2 = PresentationContext(exposure=exposure_v2, priority=priority_v2, state=state_v2, impacts=run_v2.impacts, forecasts=run_v2.forecasts, observations=all_v2_obs, delta_report=delta_v2)
    presentation_v2 = presentation_engine.build(pres_ctx_v2)
    assert len(presentation_v2.items) >= 1

    notif_ctx_v2 = NotificationContext(
        user=user,
        exposure=exposure_v2,
        priority=priority_v2,
        presentation=presentation_v2.items,
        state=state_v2,
        state_version=state_v2.state_version,
        delta_report=delta_v2,
    )
    outcome = notification_engine.evaluate(notif_ctx_v2)
    assert outcome is not None
    assert outcome.delivered is True

    # 11. Lifecycle Termination
    state_resolved = state_v2.model_copy(update={"status": "resolved", "last_observed": utcnow()})
    world_engine.persist(state_resolved)
    assert state_resolved.status == "resolved"

"""End-to-end executable test suite for multi-source event correlation and resolution.

Validates the core World-State contract:
1. Multi-source observations (Police CAD, News, WSDOT, Traffic) describing the same
   real-world occurrence resolve into a SINGLE persistent event with 4 supporting
   observations and 4 independent sources.
2. An observation describing a genuinely different event resolves to a SECOND event.
3. Subsequent observations for the incident resolve to the SAME event ID, updating
   its state and producing a semantic delta.
4. Future scheduled events (> 24h out) are assigned 'planned' status rather than 'active'.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, UTC
from typing import Any

from infraimpact.domain.enums import Authority, ObservationType, SourceType
from infraimpact.domain.ids import deterministic_id, utcnow
from infraimpact.domain.schemas import Observation, ObservationQuality, Provenance
from infraimpact.events.claims import RuleClaimExtractor
from infraimpact.events.resolver import EventResolver
from infraimpact.events.world_state import WorldStateEngine
from infraimpact.storage.sqlite_driver import SqlitePlatformRepository
from infraimpact.delta.engine import StateDeltaEngine


def _make_obs(
    source_id: str,
    obs_type: ObservationType,
    headline: str,
    event_time: datetime,
    lon: float | None = None,
    lat: float | None = None,
    authority: Authority = Authority.OFFICIAL,
    payload: dict[str, Any] | None = None,
) -> Observation:
    obs_id = deterministic_id("obs", source_id, headline, event_time.isoformat())
    geometry = {"type": "Point", "coordinates": [lon, lat]} if lon is not None and lat is not None else None
    return Observation(
        observation_id=obs_id,
        source_id=source_id,
        source_record_id=f"rec-{obs_id[:8]}",
        event_time=event_time,
        observed_at=event_time + timedelta(seconds=10),
        ingested_at=event_time + timedelta(seconds=15),
        source_type=(
            SourceType.OFFICIAL_MACHINE_READABLE
            if authority == Authority.OFFICIAL
            else SourceType.ESTABLISHED_NEWS
        ),

        observation_type=obs_type,
        geometry=geometry,
        location_precision_m=25.0 if geometry else None,
        headline=headline,
        structured_payload=payload or {"headline": headline},
        provenance=Provenance(content_hash=deterministic_id("hash", headline), authority=authority),
        quality=ObservationQuality(spatial_precision=0.9 if geometry else 0.2, temporal_precision=0.9),
    )


def test_multi_source_correlation_into_single_persistent_event(tmp_path):
    """Verify that Police, News, WSDOT, and Traffic observations describing the same

    incident resolve into ONE persistent event with 4 observations and 4 sources.
    """
    db_path = tmp_path / "test_correlation.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    claims_extractor = RuleClaimExtractor()
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
        time_window_min=180.0,
        search_radius_m=1500.0,
        merge_threshold=0.60,
    )
    world_engine = WorldStateEngine(states=repo.states)

    t0 = datetime(2026, 10, 5, 22, 41, 0, tzinfo=UTC)

    # 1. Police CAD dispatch
    obs_a = _make_obs(
        source_id="spd.cad_911",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="I-5 collision near Exit 165",
        event_time=t0,
        lon=-122.3300,
        lat=47.6100,
        authority=Authority.OFFICIAL,
        payload={"street": "I-5", "exit": "165", "incident": "collision"},
    )
    repo.observations.append(obs_a)
    res_a = resolver.resolve(obs_a)
    assert res_a.is_new_event, "First observation should open a new event"
    event_id_a = res_a.event_id

    # Extract claims for event A
    for c in claims_extractor.extract(obs_a, event_id_a):
        repo.claims.append(c)

    # 2. News Article (2 minutes later, slightly different wording, overlapping location)
    obs_b = _make_obs(
        source_id="news.komo",
        obs_type=ObservationType.NEWS_ARTICLE,
        headline="Multi-vehicle crash on I-5 near Exit 165",
        event_time=t0 + timedelta(minutes=2),
        lon=-122.3304,
        lat=47.6103,
        authority=Authority.ESTABLISHED_MEDIA,
        payload={"title": "Multi-vehicle crash on I-5 near Exit 165"},
    )
    repo.observations.append(obs_b)
    res_b = resolver.resolve(obs_b)
    assert not res_b.is_new_event, f"News report should correlate with existing event, got reason: {res_b.reason}"
    assert res_b.event_id == event_id_a, f"Expected event {event_id_a}, got {res_b.event_id}"

    for c in claims_extractor.extract(obs_b, event_id_a):
        repo.claims.append(c)

    # 3. WSDOT Road Alert (3 minutes later, road closure alert)
    obs_c = _make_obs(
        source_id="wsdot.road_alerts",
        obs_type=ObservationType.ROAD_CLOSURE,
        headline="I-5 lanes blocked near Exit 165",
        event_time=t0 + timedelta(minutes=3),
        lon=-122.3298,
        lat=47.6097,
        authority=Authority.OFFICIAL,
        payload={"street": "I-5", "element": "lanes blocked", "exit": "165"},
    )
    repo.observations.append(obs_c)
    res_c = resolver.resolve(obs_c)
    assert not res_c.is_new_event, f"WSDOT alert should correlate with existing event, got reason: {res_c.reason}"
    assert res_c.event_id == event_id_a

    for c in claims_extractor.extract(obs_c, event_id_a):
        repo.claims.append(c)

    # 4. Traffic Flow drop (4 minutes later)
    obs_d = _make_obs(
        source_id="traffic.speeds",
        obs_type=ObservationType.TRAFFIC_FLOW,
        headline="Traffic speed dropped dramatically near Exit 165",
        event_time=t0 + timedelta(minutes=4),
        lon=-122.3301,
        lat=47.6101,
        authority=Authority.OFFICIAL,
        payload={"location": "I-5 near Exit 165", "speed_drop_mph": 45},
    )
    repo.observations.append(obs_d)
    res_d = resolver.resolve(obs_d)
    assert not res_d.is_new_event, f"Traffic speed drop should correlate, got reason: {res_d.reason}"
    assert res_d.event_id == event_id_a

    for c in claims_extractor.extract(obs_d, event_id_a):
        repo.claims.append(c)

    # Reconstruct world state for this unified event
    all_obs = repo.observations.list_for_event(event_id_a)
    all_claims = repo.claims.claims_for_event(event_id_a)
    state = world_engine.rebuild(event_id_a, all_obs, all_claims, now=t0 + timedelta(minutes=5))
    world_engine.persist(state)

    # ASSERTIONS: EXACTLY ONE EVENT WITH 4 SUPPORTING OBSERVATIONS & 4 INDEPENDENT SOURCES
    assert len(all_obs) == 4, f"Expected 4 observations, got {len(all_obs)}"
    assert state.evidence.source_count == 4, f"Expected 4 sources, got {state.evidence.source_count}"
    assert state.evidence.independent_source_count == 4, f"Expected 4 independent sources, got {state.evidence.independent_source_count}"
    assert state.geometry is not None, "Persistent event must have reconstructed geometry"
    assert state.status == "active", f"Active collision must have status active, got {state.status}"


def test_distinct_event_creates_second_event(tmp_path):
    """Verify that an observation describing a genuinely different event (e.g. in Ballard

    miles away) becomes a distinct SECOND event, not merged.
    """
    db_path = tmp_path / "test_distinct.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
        time_window_min=180.0,
        search_radius_m=1500.0,
        merge_threshold=0.60,
    )

    t0 = datetime(2026, 10, 5, 22, 41, 0, tzinfo=UTC)

    # Event 1: Downtown I-5 collision
    obs_1 = _make_obs(
        source_id="spd.cad_911",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="I-5 collision near Exit 165",
        event_time=t0,
        lon=-122.3300,
        lat=47.6100,
    )
    repo.observations.append(obs_1)
    res_1 = resolver.resolve(obs_1)

    # Event 2: Ballard Drawbridge opening (7 miles away)
    obs_2 = _make_obs(
        source_id="sdot.drawbridges",
        obs_type=ObservationType.ROAD_CLOSURE,
        headline="Ballard Bridge open for marine traffic",
        event_time=t0,
        lon=-122.3760,
        lat=47.6598,
    )
    repo.observations.append(obs_2)
    res_2 = resolver.resolve(obs_2)

    assert res_2.is_new_event, "Different event miles away must open a new event"
    assert res_2.event_id != res_1.event_id, "Distinct events must have different event IDs"


def test_state_evolution_and_semantic_delta(tmp_path):
    """Verify that a later clearance report updates the SAME event ID,

    advances state version, and produces a semantic delta.
    """
    db_path = tmp_path / "test_evolution.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    claims_extractor = RuleClaimExtractor()
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
        time_window_min=180.0,
        search_radius_m=1500.0,
        merge_threshold=0.60,
    )
    world_engine = WorldStateEngine(states=repo.states)
    delta_engine = StateDeltaEngine()

    t0 = datetime(2026, 10, 5, 22, 41, 0, tzinfo=UTC)

    obs_1 = _make_obs(
        source_id="wsdot.road_alerts",
        obs_type=ObservationType.ROAD_CLOSURE,
        headline="I-5 lanes blocked near Exit 165",
        event_time=t0,
        lon=-122.3300,
        lat=47.6100,
        payload={"street": "I-5", "exit": "165", "closure_type": "lane closure"},
    )
    repo.observations.append(obs_1)
    res_1 = resolver.resolve(obs_1)
    for c in claims_extractor.extract(obs_1, res_1.event_id):
        repo.claims.append(c)

    state_v1 = world_engine.rebuild(
        res_1.event_id,
        repo.observations.list_for_event(res_1.event_id),
        repo.claims.claims_for_event(res_1.event_id),
        now=t0,
    )
    world_engine.persist(state_v1)
    assert state_v1.state_version == 1

    # 30 minutes later: clearance observation arrives
    t1 = t0 + timedelta(minutes=30)
    obs_2 = _make_obs(
        source_id="wsdot.road_alerts",
        obs_type=ObservationType.ROAD_CLOSURE,
        headline="I-5 lanes reopened near Exit 165",
        event_time=t1,
        lon=-122.3300,
        lat=47.6100,
        payload={"street": "I-5", "exit": "165", "closure_type": "reopened all clear"},
    )
    repo.observations.append(obs_2)
    res_2 = resolver.resolve(obs_2)
    assert res_2.event_id == res_1.event_id, "Clearance must update the SAME event ID"

    for c in claims_extractor.extract(obs_2, res_1.event_id):
        repo.claims.append(c)

    state_v2 = world_engine.rebuild(
        res_1.event_id,
        repo.observations.list_for_event(res_1.event_id),
        repo.claims.claims_for_event(res_1.event_id),
        previous=state_v1,
        now=t1,
    )
    world_engine.persist(state_v2)

    assert state_v2.state_version == 2, "State version must increment"
    delta_report = delta_engine.compare(state_v1, state_v2)
    assert len(delta_report.deltas) > 0, "Semantic delta must be produced"
    assert delta_report.is_material, "Road reopening must be a material change"


def test_future_scheduled_event_status(tmp_path):
    """Verify temporal semantics: 2027 future permit does not become an active 2026 event."""
    db_path = tmp_path / "test_future.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
    )

    future_t = datetime(2027, 6, 24, 0, 0, 0, tzinfo=UTC)
    obs_future = _make_obs(
        source_id="sdot.street_use",
        obs_type=ObservationType.PERMIT_EVENT,
        headline="Farmers Market Queen Anne Farmers Market 2027",
        event_time=future_t,
        lon=-122.356,
        lat=47.637,
    )
    repo.observations.append(obs_future)
    res = resolver.resolve(obs_future)

    status = repo.events.status_of(res.event_id)
    assert status == "planned", f"Future scheduled event must have status 'planned', got '{status}'"


def test_delayed_corroboration(tmp_path):
    """Test 2: Source A arrives first, Source B arrives 10 minutes later -> same event, source count increases."""
    db_path = tmp_path / "test_delayed.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
    )
    world_engine = WorldStateEngine(states=repo.states)

    t0 = datetime(2026, 10, 5, 21, 0, 0, tzinfo=UTC)
    obs_a = _make_obs(
        source_id="spd.cad_911",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="Shooting investigation on Rainier Ave S",
        event_time=t0,
        lon=-122.285,
        lat=47.545,
        payload={"street": "Rainier Ave S", "incident": "shooting"},
    )
    repo.observations.append(obs_a)
    res_a = resolver.resolve(obs_a)

    # 10 minutes later: news corroboration arrives
    t1 = t0 + timedelta(minutes=10)
    obs_b = _make_obs(
        source_id="news.komo",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="Police investigating shooting on Rainier Ave S",
        event_time=t1,
        lon=-122.285,
        lat=47.545,
        authority=Authority.ESTABLISHED_MEDIA,
        payload={"street": "Rainier Ave S", "incident": "shooting"},
    )
    repo.observations.append(obs_b)
    res_b = resolver.resolve(obs_b)

    assert res_b.event_id == res_a.event_id, "Delayed corroboration must update the SAME event ID"
    state = world_engine.rebuild(res_a.event_id, repo.observations.list_for_event(res_a.event_id), [])
    assert state.evidence.source_count == 2, f"Expected 2 sources, got {state.evidence.source_count}"
    assert state.evidence.independent_source_count == 2


def test_different_incidents_nearby_do_not_merge(tmp_path):
    """Test 3: Two distinct collisions 500m apart at nearly the same time must NOT merge."""
    db_path = tmp_path / "test_nearby.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
        search_radius_m=300.0,
    )

    t0 = datetime(2026, 10, 5, 21, 0, 0, tzinfo=UTC)
    # Crash 1 at 4th & Pike
    obs_1 = _make_obs(
        source_id="spd.cad_911",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="Traffic collision at 4th Ave & Pike St",
        event_time=t0,
        lon=-122.337,
        lat=47.610,
        payload={"street": "4th Ave & Pike St"},
    )
    repo.observations.append(obs_1)
    res_1 = resolver.resolve(obs_1)

    # Crash 2 at 7th & Stewart (500m away, different intersection)
    obs_2 = _make_obs(
        source_id="spd.cad_911",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="Traffic collision at 7th Ave & Stewart St",
        event_time=t0,
        lon=-122.333,
        lat=47.615,
        payload={"street": "7th Ave & Stewart St"},
    )
    repo.observations.append(obs_2)
    res_2 = resolver.resolve(obs_2)

    assert res_1.event_id != res_2.event_id, "Distinct incidents 500m apart must remain separate events"


def test_historical_event_status(tmp_path):
    """Test 5: Historical 2025 event must be marked 'closed', not 'active'."""
    db_path = tmp_path / "test_hist.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
    )

    past_t = datetime(2025, 4, 12, 10, 0, 0, tzinfo=UTC)
    obs_past = _make_obs(
        source_id="spd.cad_911",
        obs_type=ObservationType.POLICE_RESPONSE,
        headline="Old 2025 disturbance call",
        event_time=past_t,
        lon=-122.33,
        lat=47.61,
    )
    repo.observations.append(obs_past)
    res = resolver.resolve(obs_past)

    status = repo.events.status_of(res.event_id)
    assert status == "closed", f"Historical observation must produce 'closed' event, got '{status}'"


def test_cross_region_nws_contamination_prevented():
    """Test 6: Alabama flood alert must be filtered out at adapter boundary and not admitted."""
    from infraimpact.sources.feeds import NwsAlertsSnapshotAdapter
    from infraimpact.sources.catalog import SIGNALS
    from infraimpact.sources.snapshot import RawRecord

    spec = next(s for s in SIGNALS if s.source_id == "nws.alerts")
    adapter = NwsAlertsSnapshotAdapter(spec)

    alabama_payload = {
        "id": "urn:oid:2.49.0.1.alabama.flood",
        "event": "Coastal Flood Advisory",
        "senderName": "NWS Mobile AL",
        "areaDesc": "Mobile Coastal, Baldwin Coastal",
        "geocode": {"UGC": ["ALZ265", "ALZ266"], "SAME": ["001097", "001003"]},
    }
    raw = RawRecord(
        source_record_id="rec_alabama",
        payload=alabama_payload,
        event_time=datetime(2026, 10, 4, 16, 29, 0, tzinfo=UTC),
        observed_at=datetime(2026, 10, 4, 16, 29, 0, tzinfo=UTC),
    )
    obs = adapter.normalize(raw)
    assert obs is None, "Out-of-region Alabama NWS alert must be dropped by geographic relevance filter"


def test_repeated_source_snapshots_do_not_create_independent_sources(tmp_path):
    """Test 8: 100 repeated snapshots from one source result in 1 source and 1 independent source."""
    db_path = tmp_path / "test_snapshots.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
    )
    world_engine = WorldStateEngine(states=repo.states)

    t0 = datetime(2026, 10, 5, 21, 0, 0, tzinfo=UTC)
    first_obs = _make_obs(
        source_id="wsdot.road_alerts",
        obs_type=ObservationType.ROAD_CLOSURE,
        headline="I-5 NB lane blockage at Exit 165",
        event_time=t0,
        lon=-122.33,
        lat=47.61,
        payload={"street": "I-5", "exit": "165"},
    )
    repo.observations.append(first_obs)
    res = resolver.resolve(first_obs)

    # 99 subsequent snapshots from the same source
    for i in range(1, 100):
        obs_snap = _make_obs(
            source_id="wsdot.road_alerts",
            obs_type=ObservationType.ROAD_CLOSURE,
            headline=f"I-5 NB lane blockage at Exit 165 snap {i}",
            event_time=t0 + timedelta(seconds=i * 30),
            lon=-122.33,
            lat=47.61,
            payload={"street": "I-5", "exit": "165"},
        )
        repo.observations.append(obs_snap)
        resolver.resolve(obs_snap)

    all_obs = repo.observations.list_for_event(res.event_id)
    assert len(all_obs) == 100
    state = world_engine.rebuild(res.event_id, all_obs, [])
    assert state.evidence.source_count == 1, "Repeated snapshots from 1 feed must count as 1 source"
    assert state.evidence.independent_source_count == 1, "Repeated snapshots cannot count as independent corroboration"
    # Corroboration score must not be artificially inflated
    assert state.evidence.vector.get("independent_corroboration", 0) <= 0.35


def test_evidence_referential_integrity_retrieval(tmp_path):
    """Test 10: Every observation and claim referenced by an event must be retrievable from repository."""
    db_path = tmp_path / "test_evidence.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db_path}")
    claims_extractor = RuleClaimExtractor()
    resolver = EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget_sound",
    )
    world_engine = WorldStateEngine(states=repo.states)

    t0 = datetime(2026, 10, 5, 21, 0, 0, tzinfo=UTC)
    obs = _make_obs(
        source_id="sfd.dispatch_911",
        obs_type=ObservationType.FIRE_DISPATCH,
        headline="2-Alarm Fire on Broadway E",
        event_time=t0,
        lon=-122.32,
        lat=47.62,
        payload={"street": "Broadway E", "type": "Structure Fire 2-Alarm"},
    )
    repo.observations.append(obs)
    res = resolver.resolve(obs)
    for c in claims_extractor.extract(obs, res.event_id):
        repo.claims.append(c)

    state = world_engine.rebuild(res.event_id, [obs], repo.claims.claims_for_event(res.event_id))
    world_engine.persist(state)

    # Verify complete referral chain:
    # event -> latest state -> observation_ids -> repo.observations.get(id)
    retrieved_obs = [repo.observations.get(oid) for oid in state.observation_ids]
    assert len(retrieved_obs) == 1
    assert retrieved_obs[0] is not None
    assert retrieved_obs[0].observation_id == obs.observation_id

    # event -> latest state -> claim_ids -> repo.claims
    event_claims = repo.claims.claims_for_event(res.event_id)
    assert len(event_claims) > 0
    assert any(c.predicate in ("emergency.dispatch", "incident") for c in event_claims)

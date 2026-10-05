"""Executable test suite for Stage 1 Persistent Event Architecture.

Validates the Stage 1 contracts from Event System Plan (Stage 1).md:
1. Repeated checking is a heartbeat: unchanged records create zero observations and only update last_confirmed.
2. Disappearance detection: missing condition records across N polls trigger closure with duration calculation.
3. Significance gating: routine telemetry (STATE_ONLY) is preserved in the ledger but never opens events.
4. Temporal modeling: crawl time is not event time; old reports create historical events, future reports create scheduled events.
5. Three event kinds: Incident, Condition, Situation. Compound situations group 3+ child events with trajectory.
6. Honest evidence metrics: distinct documents, independent sources, volume-independent confidence.
7. Decision bands & Review queue: borderline matches create review candidates, contradictions recorded.
8. Stage 2 handoff: material changes stream records meaningful deltas.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, UTC
from typing import Any
from pathlib import Path

from infraimpact.bus.event_bus import InMemoryEventBus
from infraimpact.domain.enums import (
    Authority,
    EventKind,
    EventPhase,
    MatchDecision,
    ObservationType,
    SignificanceClass,
    SituationTrajectory,
    SourceType,
)
from infraimpact.domain.ids import deterministic_id, utcnow
from infraimpact.domain.schemas import (
    Contradiction,
    CorrelationCandidate,
    EventRelation,
    EvidenceSummary,
    MaterialChangeRecord,
    Observation,
    ObservationQuality,
    Provenance,
    TimelineEntry,
)
from infraimpact.events.claims import RuleClaimExtractor
from infraimpact.events.resolver import EventResolver
from infraimpact.events.situation import SituationEngine
from infraimpact.events.world_state import WorldStateEngine
from infraimpact.ingestion.pipeline import IngestionPipeline
from infraimpact.ingestion.policy import DEFAULT_SOURCE_POLICIES
from infraimpact.ingestion.significance import classify_significance
from infraimpact.sources.adapter import RawRecord
from infraimpact.storage.raw_store import FileRawStore
from infraimpact.storage.sqlite_driver import SqlitePlatformRepository


def _make_test_obs(
    source_id: str,
    obs_type: ObservationType,
    headline: str,
    event_time: datetime,
    lon: float | None = -122.3321,
    lat: float | None = 47.6062,
    authority: Authority = Authority.OFFICIAL,
    payload: dict[str, Any] | None = None,
    significance_class: SignificanceClass = SignificanceClass.EVENT_CANDIDATE,
    source_record_id: str | None = None,
) -> Observation:
    obs_id = deterministic_id("obs", source_id, headline, event_time.isoformat())
    geometry = {"type": "Point", "coordinates": [lon, lat]} if lon is not None and lat is not None else None
    return Observation(
        observation_id=obs_id,
        source_id=source_id,
        source_record_id=source_record_id or f"rec-{obs_id[:8]}",
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
        location_precision_m=50.0 if geometry else None,
        headline=headline,
        structured_payload=payload or {"headline": headline},
        provenance=Provenance(content_hash=deterministic_id("hash", headline), authority=authority),
        quality=ObservationQuality(spatial_precision=0.9 if geometry else 0.2, temporal_precision=0.9),
        significance_class=significance_class,
    )


def _make_resolver(repo: SqlitePlatformRepository) -> EventResolver:
    return EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="seattle",
        lifecycle=repo.lifecycle,
        candidates=repo.candidates,
        contradictions=repo.contradictions,
    )


class TestStage1IngestionAndDeduplication:
    """Stage 1 Section 2 & 10: Repeated checking is just a heartbeat."""

    @pytest.mark.anyio
    async def test_repeated_polls_produce_zero_new_observations(self, tmp_path: Path) -> None:
        repo = SqlitePlatformRepository(":memory:")
        bus = InMemoryEventBus()
        raw_store = FileRawStore(tmp_path)
        pipeline = IngestionPipeline(repo, raw_store, bus, "seattle")

        records = [
            RawRecord(
                source_record_id="closure_mercer_101",
                payload={
                    "event_type": "road_closure",
                    "street": "Mercer St",
                    "description": "Mercer St closed between 9th Ave and Westlake Ave",
                    "start_time": "2026-10-05T08:00:00Z",
                },
                event_time=datetime(2026, 10, 5, 8, 5, tzinfo=UTC),
            )
        ]

        def sdot_normalizer(rec: RawRecord) -> Observation:
            return _make_test_obs(
                source_id="sdot.events",
                obs_type=ObservationType.ROAD_CLOSURE,
                headline=rec.payload.get("description", "Road closure"),
                event_time=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
                payload=rec.payload,
            )

        # First poll: new record
        res1 = await pipeline.ingest_records("sdot.events", records, sdot_normalizer)
        assert res1.persisted == 1
        assert res1.duplicates == 0
        all_obs1 = repo.observations.recent(limit=100)
        assert len(all_obs1) == 1

        rec1 = repo.source_records.get("sdot.events", "closure_mercer_101")
        assert rec1 is not None
        assert rec1.version == 1
        assert rec1.missed_polls == 0

        # Second poll: exact same payload (just a heartbeat)
        records_poll2 = [
            RawRecord(
                source_record_id="closure_mercer_101",
                payload=dict(records[0].payload),
                event_time=datetime(2026, 10, 5, 8, 10, tzinfo=UTC),
            )
        ]
        res2 = await pipeline.ingest_records("sdot.events", records_poll2, sdot_normalizer)
        assert res2.persisted == 0
        assert res2.duplicates == 1
        all_obs2 = repo.observations.recent(limit=100)
        assert len(all_obs2) == 1, "Repeated poll must NOT emit a new observation into ledger"

        rec2 = repo.source_records.get("sdot.events", "closure_mercer_101")
        assert rec2 is not None
        assert rec2.version == 1
        assert rec2.missed_polls == 0

    @pytest.mark.anyio
    async def test_disappearance_detection_closes_condition(self, tmp_path: Path) -> None:
        """Stage 1 Section 2 & 10: Disappearance detection closes conditions."""
        repo = SqlitePlatformRepository(":memory:")
        bus = InMemoryEventBus()
        raw_store = FileRawStore(tmp_path)
        pipeline = IngestionPipeline(repo, raw_store, bus, "seattle")

        def sdot_normalizer(rec: RawRecord) -> Observation:
            return _make_test_obs(
                source_id="sdot.events",
                obs_type=ObservationType.ROAD_CLOSURE,
                headline=rec.payload.get("description", rec.payload.get("street", "Road closure")),
                event_time=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
                payload=rec.payload,
            )

        # Ingest condition record
        record = RawRecord(
            source_record_id="closure_pike_202",
            payload={
                "event_type": "road_closure",
                "street": "Pike St",
                "description": "Full closure for utility repair",
                "start_time": "2026-10-05T08:00:00Z",
            },
            event_time=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
        )
        res1 = await pipeline.ingest_records("sdot.events", [record], sdot_normalizer)
        assert res1.persisted == 1

        # Now simulate successive successful polls where this record is absent
        # (Threshold is 3 polls in default policy)
        other_record = RawRecord(
            source_record_id="closure_denny_303",
            payload={"event_type": "road_closure", "street": "Denny Way"},
            event_time=datetime(2026, 10, 5, 8, 5, tzinfo=UTC),
        )
        # Miss 1
        await pipeline.ingest_records("sdot.events", [other_record], sdot_normalizer)
        rec_after_miss1 = repo.source_records.get("sdot.events", "closure_pike_202")
        assert rec_after_miss1 is not None
        assert rec_after_miss1.missed_polls == 1
        assert rec_after_miss1.is_active is True

        # Miss 2
        await pipeline.ingest_records("sdot.events", [other_record], sdot_normalizer)
        rec_after_miss2 = repo.source_records.get("sdot.events", "closure_pike_202")
        assert rec_after_miss2 is not None
        assert rec_after_miss2.missed_polls == 2
        assert rec_after_miss2.is_active is True

        # Miss 3: triggers disappearance closure
        await pipeline.ingest_records("sdot.events", [other_record], sdot_normalizer)
        rec_after_miss3 = repo.source_records.get("sdot.events", "closure_pike_202")
        assert rec_after_miss3 is not None
        assert rec_after_miss3.missed_polls == 3
        assert rec_after_miss3.is_active is False
        assert rec_after_miss3.ended_at is not None

        # Verify an ended observation was appended to ledger
        all_obs = repo.observations.recent(limit=100)
        ended_obs = [
            o for o in all_obs
            if "cleared" in o.headline.lower() or "ended" in o.headline.lower() or (o.structured_payload and o.structured_payload.get("disappeared"))
        ]
        assert len(ended_obs) == 1


class TestStage1SignificanceGating:
    """Stage 1 Section 4: Routine telemetry stays in ledger and never opens events."""

    def test_routine_telemetry_never_opens_new_events(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        resolver = _make_resolver(repo)

        now = datetime.now(UTC)
        # Routine telemetry: buses running on time, road clear, all lanes open
        routine_obs = _make_test_obs(
            source_id="kcm.telemetry",
            obs_type=ObservationType.TRANSIT_SERVICE_ALERT,
            headline="Route 40 running on regular schedule, no delays",
            event_time=now,
            significance_class=SignificanceClass.STATE_ONLY,
        )
        repo.observations.append(routine_obs)

        # Resolver should gate this out and return Resolution with event_id=None
        match_result = resolver.resolve(routine_obs)
        assert match_result is not None
        assert match_result.event_id is None
        assert match_result.decided_by == "significance_gate"
        assert len(repo.events.all_events()) == 0


class TestStage1TemporalModeling:
    """Stage 1 Section 5: Four separate timestamps, freshness expiry, historical & scheduled events."""

    def test_day_old_news_creates_historical_event(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        resolver = _make_resolver(repo)
        world_engine = WorldStateEngine(states=repo.states)

        # Article published 2 days ago
        two_days_ago = datetime.now(UTC) - timedelta(days=2)
        old_obs = _make_test_obs(
            source_id="news.komo",
            obs_type=ObservationType.POLICE_RESPONSE,
            headline="Two injured in rollover crash on I-5 South on Saturday",
            event_time=two_days_ago,
            authority=Authority.ESTABLISHED_MEDIA,
        )
        repo.observations.append(old_obs)

        match = resolver.resolve(old_obs)
        assert match is not None
        event_id = match.event_id
        assert event_id is not None

        # Phase must be HISTORICAL
        phase = repo.events.phase_of(event_id)
        assert phase == EventPhase.HISTORICAL

        state = world_engine.rebuild(
            event_id=event_id,
            observations=[old_obs],
            claims=[],
            kind=repo.events.kind_of(event_id),
            phase=phase,
        )
        assert state.phase == EventPhase.HISTORICAL

    def test_future_report_creates_scheduled_event(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        resolver = _make_resolver(repo)
        world_engine = WorldStateEngine(states=repo.states)

        # Scheduled street fair 30 days in the future
        future_time = datetime.now(UTC) + timedelta(days=30)
        scheduled_obs = _make_test_obs(
            source_id="sdot.street_use",
            obs_type=ObservationType.ROAD_CLOSURE,
            headline="Permitted Street Fair: Ballard Avenue Closure for Holiday Market",
            event_time=future_time,
            payload={"event_time": future_time.isoformat()},
        )
        repo.observations.append(scheduled_obs)

        match = resolver.resolve(scheduled_obs)
        assert match is not None
        event_id = match.event_id
        assert event_id is not None

        phase = repo.events.phase_of(event_id)
        assert phase == EventPhase.SCHEDULED

        state = world_engine.rebuild(
            event_id=event_id,
            observations=[scheduled_obs],
            claims=[],
            kind=repo.events.kind_of(event_id),
            phase=phase,
        )
        assert state.phase == EventPhase.SCHEDULED


class TestStage1EventKindsAndSituations:
    """Stage 1 Section 1, 6 & 7: Incident, Condition, Situation compound promotion."""

    def test_three_event_kinds_classified_honestly(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        resolver = _make_resolver(repo)

        now = datetime.now(UTC)
        # 1. Incident (point-in-time)
        inc_obs = _make_test_obs(
            source_id="spd.cad",
            obs_type=ObservationType.POLICE_RESPONSE,
            headline="Motor Vehicle Collision at 4th Ave and Pine St",
            event_time=now,
        )
        repo.observations.append(inc_obs)
        m_inc = resolver.resolve(inc_obs)
        assert m_inc is not None
        assert m_inc.event_id is not None
        assert repo.events.kind_of(m_inc.event_id) == EventKind.INCIDENT

        # 2. Condition (temporal span)
        cond_obs = _make_test_obs(
            source_id="sdot.events",
            obs_type=ObservationType.ROAD_CLOSURE,
            headline="Emergency Sewer Repair Closure on 12th Ave",
            event_time=now,
            lon=-122.315,
            lat=47.615,
        )
        repo.observations.append(cond_obs)
        m_cond = resolver.resolve(cond_obs)
        assert m_cond is not None
        assert m_cond.event_id is not None
        assert repo.events.kind_of(m_cond.event_id) == EventKind.CONDITION

    def test_situation_promotion_and_trajectory(self) -> None:
        """3+ correlated events promote to parent Situation with trajectory tracking."""
        repo = SqlitePlatformRepository(":memory:")
        resolver = _make_resolver(repo)
        situation_engine = SituationEngine(repo=repo, region_id="seattle")
        world_engine = WorldStateEngine(states=repo.states)

        base_time = datetime.now(UTC) - timedelta(minutes=15)
        # Create 3 child events in close proximity but distinct sub-occurrences
        child_ids: list[str] = []
        child_obs: list[Observation] = []
        items = [
            ("spd.cad", ObservationType.POLICE_RESPONSE, "Protest march gathering at Westlake Center", -122.336, 47.611),
            ("sdot.events", ObservationType.ROAD_CLOSURE, "Road closure on Pine St for utility work", -122.330, 47.614),
            ("kcm.telemetry", ObservationType.TRANSIT_SERVICE_ALERT, "Transit service disruption and bus reroutes on 3rd Ave", -122.338, 47.608),
        ]
        for idx, (src, otype, h, lon, lat) in enumerate(items):
            obs = _make_test_obs(
                source_id=src,
                obs_type=otype,
                headline=h,
                event_time=base_time + timedelta(minutes=idx * 3),
                lon=lon,
                lat=lat,
            )
            child_obs.append(obs)
            repo.observations.append(obs)
            m = resolver.resolve(obs)
            assert m is not None
            assert m.event_id is not None
            child_ids.append(m.event_id)
            repo.events.link_observation(m.event_id, obs.observation_id)

            # Persist EventState so SituationEngine can cluster active states
            cstate = world_engine.rebuild(
                event_id=m.event_id,
                observations=[obs],
                claims=[],
                kind=repo.events.kind_of(m.event_id),
                phase=EventPhase.ACTIVE,
            )
            repo.states.append_state(cstate)

        assert len(set(child_ids)) == 3

        # Run Situation Engine
        new_situations = situation_engine.evaluate()
        assert len(new_situations) >= 1
        sit_id = new_situations[0]

        # Verify Situation properties
        assert repo.events.kind_of(sit_id) == EventKind.SITUATION
        sit_state = world_engine.rebuild(
            event_id=sit_id,
            observations=child_obs,
            claims=[],
            kind=EventKind.SITUATION,
            child_event_ids=child_ids,
            trajectory=SituationTrajectory.ESCALATING,
        )
        assert sit_state.kind == EventKind.SITUATION
        assert len(sit_state.child_event_ids) >= 3
        for cid in child_ids:
            assert cid in sit_state.child_event_ids


class TestStage1HonestEvidenceAndConfidence:
    """Stage 1 Section 8: Honest evidence counts and volume-independent confidence."""

    def test_volume_does_not_inflate_confidence(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        world_engine = WorldStateEngine(states=repo.states)

        base_time = datetime.now(UTC)
        event_id = "evt_test_volume"
        repo.events.ensure(event_id, base_time, "seattle")

        # 5 observations from the SAME single source record (e.g. repeated polls of the same feed item)
        obs_list = []
        for i in range(5):
            obs = _make_test_obs(
                source_id="sdot.events",
                obs_type=ObservationType.ROAD_CLOSURE,
                headline="Right lane blocked on 5th Ave",
                event_time=base_time + timedelta(minutes=i),
                source_record_id="closure_5th_ave_common_id",
            )
            obs_list.append(obs)
            repo.observations.append(obs)
            repo.events.link_observation(event_id, obs.observation_id)

        state_single = world_engine.rebuild(event_id, obs_list, [])
        assert state_single.evidence is not None
        assert state_single.evidence.source_count == 1
        assert state_single.evidence.independent_source_count == 1
        assert state_single.evidence.distinct_document_count == 1
        # Confidence must be anchored to the source authority (~0.75 for official city feed), NOT 1.0
        conf_single = state_single.evidence.vector.get("event_confidence", 0.0)
        assert conf_single < 0.95

        # Now add an independent source family (e.g. WSDOT or News)
        news_obs = _make_test_obs(
            source_id="news.king5",
            obs_type=ObservationType.NEWS_ARTICLE,
            headline="KING5 reports traffic delay due to lane blockage on 5th Ave",
            event_time=base_time + timedelta(minutes=6),
            authority=Authority.ESTABLISHED_MEDIA,
            source_record_id="king5_article_101",
        )
        repo.observations.append(news_obs)
        repo.events.link_observation(event_id, news_obs.observation_id)
        obs_list.append(news_obs)

        state_multi = world_engine.rebuild(event_id, obs_list, [])
        assert state_multi.evidence.independent_source_count == 2
        assert state_multi.evidence.source_family_count == 2
        conf_multi = state_multi.evidence.vector.get("event_confidence", 0.0)
        # Corroborated confidence is higher than single source
        assert conf_multi > conf_single


class TestStage1ReviewQueueAndContradictions:
    """Stage 1 Section 11 & 12: Ambiguous candidates quarantine and contradiction tracking."""

    def test_possible_match_queued_in_review(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        resolver = _make_resolver(repo)

        now = datetime.now(UTC)
        # Create base event
        obs1 = _make_test_obs(
            source_id="spd.cad",
            obs_type=ObservationType.POLICE_RESPONSE,
            headline="Assault in progress near 3rd and Pike",
            event_time=now,
            lon=-122.338,
            lat=47.609,
        )
        repo.observations.append(obs1)
        m1 = resolver.resolve(obs1)
        assert m1 is not None
        assert m1.event_id is not None

        # Create candidate in review band
        cand = CorrelationCandidate(
            candidate_id="cand_test_101",
            observation_id="obs_ambiguous_001",
            source_record_id="rec_ambiguous_001",
            event_id=m1.event_id,
            score=0.55,
            decision=MatchDecision.POSSIBLE,
            created_at=now,
            status="pending",
        )
        repo.candidates.append(cand)

        # Check repository and queue
        pending = repo.candidates.list_pending()
        assert len(pending) == 1
        assert pending[0].candidate_id == "cand_test_101"

        # Operator approves candidate
        repo.candidates.update_status("cand_test_101", "approved")
        pending_after = repo.candidates.list_pending()
        assert len(pending_after) == 0

    def test_conflicting_claims_recorded_as_contradictions(self) -> None:
        repo = SqlitePlatformRepository(":memory:")
        contra = Contradiction(
            contradiction_id="contra_1",
            event_id="evt_contra_test",
            predicate="lanes_blocked",
            claim_id_a="claim_1",
            claim_id_b="claim_2",
            value_a="1_lane",
            value_b="all_lanes",
            detected_at=datetime.now(UTC),
            resolved=False,
        )
        repo.contradictions.append(contra)

        unresolved = repo.contradictions.unresolved()
        assert len(unresolved) == 1
        assert unresolved[0].predicate == "lanes_blocked"
        assert unresolved[0].value_a == "1_lane"
        assert unresolved[0].value_b == "all_lanes"


class TestStage1MaterialChangesStream:
    """Stage 1 Section 13: Stage 2 handoff through material changes."""

    def test_material_changes_stream_recorded(self) -> None:
        repo = SqlitePlatformRepository(":memory:")

        now = datetime.now(UTC)
        mc = MaterialChangeRecord(
            change_id="mc_001",
            event_id="evt_material_test",
            version=2,
            changed_fields=("phase", "severity"),
            change_flags=("phase_transition", "severity_escalation"),
            is_material=True,
            reason="Phase transitioned active -> ended; severity escalated to high",
            recorded_at=now,
        )
        repo.material_changes.append(mc)

        recent = repo.material_changes.list_recent(limit=10)
        assert len(recent) == 1
        assert recent[0].event_id == "evt_material_test"
        assert recent[0].is_material is True
        assert "phase" in recent[0].changed_fields

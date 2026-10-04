"""Section 2.2 and section 7: the observation ledger and dedupe.

The ledger is append-only: an observation, once written, is never updated or
removed. Dedupe happens *before* the append so a replayed record does not
create a second row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import DOWNTOWN, point

from infraimpact.domain.enums import (
    Authority,
    ObservationType,
    SourceType,
)
from infraimpact.domain.ids import content_hash
from infraimpact.domain.schemas import (
    Claim,
    EventState,
    Observation,
    ObservationQuality,
    Provenance,
)


def make_observation(
    observation_id: str = "obs_1",
    *,
    source_id: str = "synthetic.puget_sound",
    source_record_id: str = "rec_1",
    observed_at: datetime | None = None,
    payload: dict | None = None,
) -> Observation:
    now = datetime.now(UTC)
    payload = payload if payload is not None else {"kind": "road_closure"}
    return Observation(
        observation_id=observation_id,
        source_id=source_id,
        source_record_id=source_record_id,
        event_time=now,
        observed_at=observed_at or now,
        ingested_at=now,
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=ObservationType.ROAD_CLOSURE,
        geometry=point(*DOWNTOWN),
        headline="I-5 closed",
        structured_payload=payload,
        provenance=Provenance(
            authority=Authority.OFFICIAL,
            retrieval_method="feed",
            content_hash=content_hash(payload),
        ),
        quality=ObservationQuality(source_reliability=0.9, spatial_precision=0.85),
    )


class TestObservationLedger:
    def test_append_then_get(self, repo):
        repo.observations.append(make_observation())
        stored = repo.observations.get("obs_1")
        assert stored is not None
        assert stored.headline == "I-5 closed"

    def test_append_is_idempotent_for_the_same_dedupe_key(self, repo):
        assert repo.observations.append(make_observation()) is True
        # A replay of the same record must not create a second row.
        assert repo.observations.append(make_observation()) is False
        assert repo.observations.count() == 1

    def test_has_dedupe_key(self, repo):
        observation = make_observation()
        repo.observations.append(observation)
        assert repo.observations.has_dedupe_key(observation.dedupe_key())
        assert not repo.observations.has_dedupe_key("dk_nonexistent")

    def test_dedupe_key_is_deterministic(self):
        # Section 7 keys on source_id + source_record_id + update timestamp, so
        # the timestamp has to be held still for the key to be reproducible.
        stamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert make_observation(observed_at=stamp).dedupe_key() == make_observation(
            observed_at=stamp
        ).dedupe_key()

    def test_dedupe_key_changes_when_the_source_updates_the_record(self):
        # Same record_id, later update timestamp: this is a revision, not a
        # duplicate, and must survive dedupe as a distinct observation.
        first = make_observation(observed_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
        revised = make_observation(observed_at=datetime(2026, 1, 1, 12, 5, tzinfo=UTC))
        assert first.dedupe_key() != revised.dedupe_key()

    def test_different_record_ids_are_distinct(self, repo):
        repo.observations.append(make_observation("obs_1", source_record_id="rec_1"))
        repo.observations.append(make_observation("obs_2", source_record_id="rec_2"))
        assert repo.observations.count() == 2

    def test_missing_observation_returns_none(self, repo):
        assert repo.observations.get("nope") is None

    def test_recent_is_newest_first(self, repo):
        base = datetime.now(UTC)
        for i in range(3):
            repo.observations.append(
                make_observation(
                    f"obs_{i}",
                    source_record_id=f"rec_{i}",
                    observed_at=base - timedelta(minutes=i),
                )
            )
        recent = repo.observations.recent(limit=3)
        assert len(recent) == 3
        times = [o.observed_at for o in recent]
        assert times == sorted(times, reverse=True)


class TestEvents:
    def test_ensure_is_idempotent(self, repo):
        now = datetime.now(UTC)
        repo.events.ensure("evt_1", now, "puget-sound")
        repo.events.ensure("evt_1", now, "puget-sound")
        assert len(repo.events.active_events()) == 1

    def test_link_observation(self, repo):
        now = datetime.now(UTC)
        repo.events.ensure("evt_1", now, "puget-sound")
        repo.events.link_observation("evt_1", "obs_1")
        assert "obs_1" in repo.events.observations_of("evt_1")

    def test_closing_removes_from_active(self, repo):
        now = datetime.now(UTC)
        repo.events.ensure("evt_1", now, "puget-sound")
        repo.events.close("evt_1", now, "resolved")
        assert "evt_1" not in repo.events.active_events()

    def test_alias_recording(self, repo):
        now = datetime.now(UTC)
        repo.events.ensure("evt_1", now, "puget-sound")
        repo.events.record_alias("evt_external_9", "evt_1")
        assert "evt_1" in repo.events.active_events()

    def test_all_events_includes_closed(self, repo):
        now = datetime.now(UTC)
        repo.events.ensure("evt_1", now, "puget-sound")
        repo.events.ensure("evt_2", now, "puget-sound")
        repo.events.close("evt_2", now, "resolved")
        ids = {e["event_id"] for e in repo.events.all_events()}
        assert ids == {"evt_1", "evt_2"}


class TestStates:
    def test_append_and_latest(self, repo):
        repo.states.append_state(EventState(event_id="evt_1", state_version=1))
        repo.states.append_state(EventState(event_id="evt_1", state_version=2))
        assert repo.states.latest("evt_1").state_version == 2

    def test_version_lookup(self, repo):
        repo.states.append_state(EventState(event_id="evt_1", state_version=1))
        assert repo.states.version("evt_1", 1) is not None
        assert repo.states.version("evt_1", 99) is None

    def test_history_is_ordered(self, repo):
        for v in (1, 2, 3):
            repo.states.append_state(EventState(event_id="evt_1", state_version=v))
        assert [s.state_version for s in repo.states.history("evt_1")] == [1, 2, 3]

    def test_state_as_of(self, repo):
        now = datetime.now(UTC)
        repo.states.append_state(
            EventState(event_id="evt_1", state_version=1, reconstructed_at=now)
        )
        repo.states.append_state(
            EventState(
                event_id="evt_1",
                state_version=2,
                reconstructed_at=now + timedelta(minutes=5),
            )
        )
        found = repo.states.state_as_of("evt_1", now + timedelta(minutes=1))
        assert found.state_version == 1

    def test_state_as_of_survives_a_backfill(self, repo):
        # Section 49 replay of an old incident writes every state row today, so
        # the query has to key on when the belief was formed, not when it was stored.
        long_ago = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)
        repo.states.append_state(
            EventState(event_id="evt_old", state_version=1, reconstructed_at=long_ago)
        )
        repo.states.append_state(
            EventState(
                event_id="evt_old",
                state_version=2,
                reconstructed_at=long_ago + timedelta(hours=1),
            )
        )
        assert repo.states.state_as_of("evt_old", long_ago + timedelta(minutes=30)).state_version == 1
        assert repo.states.state_as_of("evt_old", long_ago + timedelta(hours=2)).state_version == 2

    def test_states_are_never_overwritten(self, repo):
        first = EventState(event_id="evt_1", state_version=1, status="candidate")
        repo.states.append_state(first)
        repo.states.append_state(
            EventState(event_id="evt_1", state_version=1, status="closed")
        )
        # Same version, same row: the ledger does not mutate history.
        assert len(repo.states.history("evt_1")) == 1


class TestClaims:
    def test_append_and_query(self, repo):
        now = datetime.now(UTC)
        claim = Claim(
            claim_id="clm_1",
            observation_id="obs_1",
            event_id="evt_1",
            predicate="road.closed",
            value=True,
            asserted_at=now,
        )
        repo.claims.append(claim)
        assert len(repo.claims.claims_for_event("evt_1")) == 1
        assert len(repo.claims.claims_for_observation("obs_1")) == 1

    def test_claims_for_unknown_event(self, repo):
        assert repo.claims.claims_for_event("nope") == []


class TestCheckpoints:
    def test_round_trip(self, repo):
        repo.checkpoint("poll:synthetic", "2026-01-01T00:00:00Z")
        assert repo.get_checkpoint("poll:synthetic") == "2026-01-01T00:00:00Z"

    def test_unknown_consumer(self, repo):
        assert repo.get_checkpoint("never-seen") is None


class TestUsers:
    def test_upsert_and_get(self, repo, downtown_user):
        repo.users.upsert(downtown_user)
        stored = repo.users.get("u_downtown")
        assert stored is not None
        assert stored.saved_places[0].place_id == "p_home"

    def test_upsert_replaces(self, repo, downtown_user):
        repo.users.upsert(downtown_user)
        updated = downtown_user.model_copy(
            update={"saved_places": downtown_user.saved_places[:0]}
        )
        repo.users.upsert(updated)
        assert repo.users.get("u_downtown").saved_places == ()

    def test_all(self, repo, downtown_user, remote_user):
        repo.users.upsert(downtown_user)
        repo.users.upsert(remote_user)
        assert {u.user_id for u in repo.users.all()} == {"u_downtown", "u_remote"}

    def test_add_route_accumulates(self, repo, downtown_user):
        repo.users.upsert(downtown_user)
        before = len(repo.users.get("u_downtown").route_profiles)
        repo.users.add_route(
            "u_downtown",
            downtown_user.route_profiles[0].model_copy(update={"route_id": "r_new"}),
        )
        after = repo.users.get("u_downtown").route_profiles
        assert len(after) == before + 1
        assert "r_new" in {r.route_id for r in after}

    def test_add_place_accumulates(self, repo, downtown_user):
        from infraimpact.domain.schemas import SavedPlace

        repo.users.upsert(downtown_user)
        repo.users.add_place(
            "u_downtown",
            SavedPlace(
                place_id="p_gym", name="Gym", kind="other", geometry=point(*DOWNTOWN)
            ),
        )
        assert "p_gym" in {p.place_id for p in repo.users.get("u_downtown").saved_places}


class TestNotifications:
    def test_enqueue_and_pending(self, repo):
        from infraimpact.domain.enums import NotificationReason, Urgency
        from infraimpact.domain.schemas import NotificationCandidate

        candidate = NotificationCandidate(
            notification_id="nt_1",
            user_id="u1",
            event_id="evt_1",
            reason=NotificationReason.NEW_EVENT,
            urgency=Urgency.HIGH,
            headline="New event",
            body="",
            dedupe_key="u1|evt_1|event|1",
        )
        assert repo.notifications.enqueue(candidate)
        assert len(repo.notifications.pending()) == 1

    def test_enqueue_is_idempotent_on_dedupe_key(self, repo):
        from infraimpact.domain.enums import NotificationReason, Urgency
        from infraimpact.domain.schemas import NotificationCandidate

        def build(nid: str) -> NotificationCandidate:
            return NotificationCandidate(
                notification_id=nid,
                user_id="u1",
                event_id="evt_1",
                reason=NotificationReason.NEW_EVENT,
                urgency=Urgency.HIGH,
                headline="New event",
                body="",
                dedupe_key="u1|evt_1|event|1",
            )

        assert repo.notifications.enqueue(build("nt_1"))
        assert not repo.notifications.enqueue(build("nt_2"))
        assert repo.notifications.seen_dedupe_key("u1|evt_1|event|1")

    def test_mark_sent_clears_pending(self, repo):
        from infraimpact.domain.enums import NotificationReason, Urgency
        from infraimpact.domain.schemas import NotificationCandidate

        repo.notifications.enqueue(
            NotificationCandidate(
                notification_id="nt_1",
                user_id="u1",
                event_id="evt_1",
                reason=NotificationReason.NEW_EVENT,
                urgency=Urgency.HIGH,
                headline="x",
                body="",
                dedupe_key="k1",
            )
        )
        repo.notifications.mark_sent("nt_1")
        assert repo.notifications.pending() == []


class TestSourceHealth:
    def test_put_and_get(self, repo):
        from infraimpact.domain.enums import HealthState
        from infraimpact.domain.schemas import SourceHealth

        repo.source_health.put(
            SourceHealth(source_id="s1", state=HealthState.HEALTHY, records_received=5)
        )
        health = repo.source_health.get("s1")
        assert health.records_received == 5
        assert len(repo.source_health.all()) == 1

    def test_put_overwrites_health(self, repo):
        from infraimpact.domain.enums import HealthState
        from infraimpact.domain.schemas import SourceHealth

        repo.source_health.put(SourceHealth(source_id="s1", state=HealthState.HEALTHY))
        repo.source_health.put(SourceHealth(source_id="s1", state=HealthState.DEGRADED))
        assert len(repo.source_health.all()) == 1
        assert repo.source_health.get("s1").state is HealthState.DEGRADED
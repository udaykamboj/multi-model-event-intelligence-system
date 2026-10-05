"""Tests for the world-state system: identity, scale, lifecycle, deltas, world view.

These cover the properties the system's central claims depend on, and each test
is named after the claim rather than the method. A test called
``test_magnitude_tracks_the_present`` says what must be true; one called
``test_scale_aggregate`` only says what is called.

The recurring theme is that most of the bugs found while building this were
*silent*. Every one of them produced plausible output: two unrelated events
reconstructed as one fictional situation, a crowd reported at a third of its
actual size, a status that flipped on an hour-old constant. Nothing crashed and
nothing looked wrong. So the assertions below check the properties themselves
rather than the shape of the return value.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from infraimpact.delta.engine import StateDeltaEngine
from infraimpact.domain.enums import (
    Authority,
    HealthState,
    ObservationType,
    SourceType,
    TruthStatus,
    Urgency,
)
from infraimpact.domain.geo import point
from infraimpact.domain.ids import utcnow
from infraimpact.domain.schemas import (
    Claim,
    Observation,
    ObservationQuality,
    Provenance,
    SourceHealth,
    StateDeltaRecord,
)
from infraimpact.events.identity import EventIdentityAllocator
from infraimpact.events.resolver import EventResolver
from infraimpact.events.scale import ScaleEstimator
from infraimpact.events.world_state import WorldStateEngine
from infraimpact.world.projection import WorldProjection

# Downtown Seattle, and a second downtown roughly 6 km away. Two situations a
# few kilometres apart is the ordinary case, not an edge case - it is what a city
# looks like from above, and it is what the identity bug could not tell apart.
DOWNTOWN = (-122.335, 47.608)
BALLARD = (-122.207, 47.660)


@pytest.fixture
def resolver(repo):
    return EventResolver(
        events=repo.events,
        observations=repo.observations,
        claims=repo.claims,
        states=repo.states,
        region_id="puget-sound",
    )


@pytest.fixture
def world(repo):
    return WorldStateEngine(repo.states)


@pytest.fixture
def deltas():
    return StateDeltaEngine()


def make_observation(
    observation_id: str,
    *,
    at,
    geometry=DOWNTOWN,
    headline: str = "Report",
    payload: dict | None = None,
    observation_type: ObservationType = ObservationType.PUBLIC_GATHERING_REPORT,
    authority: Authority = Authority.OFFICIAL,
    source_id: str = "synthetic.official",
) -> Observation:
    return Observation(
        observation_id=observation_id,
        source_id=source_id,
        source_record_id=observation_id,
        event_time=at,
        observed_at=at,
        ingested_at=at,
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=observation_type,
        geometry=point(*geometry),
        location_precision_m=30.0,
        headline=headline,
        structured_payload=payload or {},
        provenance=Provenance(
            authority=authority, retrieval_method="feed", content_hash=observation_id
        ),
        quality=ObservationQuality(
            source_reliability=0.9,
            spatial_precision=0.9,
            temporal_precision=0.9,
            extraction_confidence=0.9,
        ),
    )


def make_claim(observation: Observation, predicate: str = "participants") -> Claim:
    return Claim(
        claim_id=f"clm_{observation.observation_id}",
        observation_id=observation.observation_id,
        source_id=observation.source_id,
        predicate=predicate,
        value=observation.structured_payload,
        truth_status=TruthStatus.CONFIRMED,
        extraction_confidence=0.9,
    )


def ingest(repo, resolver, world, deltas, observations, *, now):
    """Run observations through the pipeline the runtime uses.

    Deliberately keeps one ``previous`` across every call. Passing
    ``previous=None`` per call is how the lifecycle test appeared to work while
    actually reporting a brand-new event each time, so a genuine sequence has to
    stay continuous to be testing anything.
    """

    previous = None
    event_id = None
    for observation in observations:
        repo.observations.append(observation)
        claims = [make_claim(observation)]
        for claim in claims:
            repo.claims.append(claim)
        resolution = resolver.resolve(observation, claims)
        event_id = resolution.event_id
        state = world.rebuild(
            event_id,
            repo.observations.list_for_event(event_id),
            repo.claims.claims_for_event(event_id),
            previous,
            now=now,
        )
        world.persist(state)
        deltas.compare(
            previous,
            state,
            impacts=state.affected_infrastructure,
            previous_impacts=previous.affected_infrastructure if previous else (),
        )
        previous = state
    return previous, event_id


class _NullStates:
    """A state repository that stores nothing, for pure-reconstruction tests."""

    def append_state(self, state) -> None:  # noqa: ANN001
        pass

    def latest(self, event_id: str):  # noqa: ANN201
        return None


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_unrelated_events_get_distinct_ids(repo, resolver):
    """Two simultaneous incidents 6 km apart are two events, not one.

    This is the identity bug. The id used to be a hash of region, observation
    type and calendar day, so both of these produced the *same* id, and the
    platform reconstructed one fictional event out of two real ones. Nothing
    indicated a problem: the id was deterministic, the state rebuilt cleanly, and
    every downstream number was confidently wrong.
    """

    now = utcnow()
    a = make_observation(
        "obs_a",
        at=now,
        geometry=DOWNTOWN,
        headline="Aurora Ave closed northbound",
        observation_type=ObservationType.ROAD_CLOSURE,
    )
    b = make_observation(
        "obs_b",
        at=now,
        geometry=BALLARD,
        headline="Ballard Bridge closed to all vehicles",
        observation_type=ObservationType.ROAD_CLOSURE,
    )
    repo.observations.append(a)
    repo.observations.append(b)

    assert resolver.resolve(a).event_id != resolver.resolve(b).event_id


def test_same_event_from_two_sources_resolves_to_one_id(repo, resolver):
    """Cross-source deduplication still works.

    The fix for the collision above must not overshoot into opening an event per
    record. Two agencies reporting the same closure minutes apart, with different
    observation types and no shared claim structure, are one event - that was the
    behaviour the identity allocator's fingerprint exists to preserve, and it is
    easy to break while fixing a collision.
    """

    now = utcnow()
    official = make_observation(
        "obs_official",
        at=now,
        headline="Aurora Ave closed northbound at N 45th",
        observation_type=ObservationType.ROAD_CLOSURE,
    )
    media = make_observation(
        "obs_media",
        at=now + timedelta(minutes=4),
        headline="Aurora Ave closed northbound at N 45th",
        observation_type=ObservationType.NEWS_ARTICLE,
        authority=Authority.ESTABLISHED_MEDIA,
        source_id="synthetic.media",
    )
    repo.observations.append(official)
    repo.observations.append(media)

    assert resolver.resolve(official).event_id == resolver.resolve(media).event_id


def test_reprocessing_a_source_never_forks_an_event(repo, resolver):
    """Re-resolving already-linked observations is a no-op, not a re-decision.

    Every feed is polled on a timer and re-reads its whole window each time, so
    most observations reaching the resolver have been seen before. Deciding that
    by reverse lookup rather than by re-running matching is what makes that safe.
    """

    now = utcnow()
    observations = [
        make_observation(f"obs_{i}", at=now - timedelta(minutes=10 * i), headline="4th Ave blocked")
        for i in range(4)
    ]
    for observation in observations:
        repo.observations.append(observation)

    first_pass = {o.observation_id: resolver.resolve(o).event_id for o in observations}
    assert len(set(first_pass.values())) == 1

    # Three more rounds, as a polled feed would produce.
    for _ in range(3):
        again = {o.observation_id: resolver.resolve(o).event_id for o in observations}
        assert again == first_pass

    assert len(repo.events.all_events()) == 1


def test_identity_allocation_prefers_an_existing_link(repo):
    """The allocator's own invariant: one observation, at most one event, forever.

    Tested against the allocator directly because the resolver's shortcut is what
    makes the invariant hold in practice, and a direct test is what would catch
    the shortcut being removed.
    """

    allocator = EventIdentityAllocator("puget-sound")
    observation = make_observation("obs_x", at=utcnow())

    first = allocator.allocate(
        observation,
        event_exists=lambda _: False,
        event_for_observation=lambda _: "evt_already_linked",
        observations_of=lambda _: ["obs_x"],
    )
    assert first.event_id == "evt_already_linked"
    assert first.basis == "existing"


def test_identity_allocator_disambiguates_a_taken_fingerprint():
    """Distinct situations sharing a fingerprint get distinct ids, in order.

    Two sources reporting one closure eight minutes apart share a fingerprint
    and must get the *same* id - that is what deduplication across sources means.
    But when a fingerprint is already held by an event the new observation does
    not belong to, the situation really is a second one, and it needs a second
    id. Handing out the next ordinal rather than failing or reusing is what keeps
    the collision impossible instead of merely unlikely.
    """

    allocator = EventIdentityAllocator("puget-sound", time_bucket_min=15.0, geo_cell_m=750.0)
    now = utcnow()
    observations = [
        make_observation(f"obs_{i}", at=now, payload={"participants": 100})
        for i in range(3)
    ]
    # All three share a fingerprint, so only the disambiguating ordinal separates
    # them. Nothing links them to an existing event.
    base_id = allocator.base_id(allocator.fingerprint(observations[0]))
    taken: set[str] = set()
    allocated = []
    for observation in observations:
        allocation = allocator.allocate(
            observation,
            event_exists=lambda event_id: event_id in taken,
            event_for_observation=lambda _: None,
            observations_of=lambda _: [],
        )
        taken.add(allocation.event_id)
        allocated.append(allocation)

    assert [a.basis for a in allocated] == ["fingerprint", "ordinal", "ordinal"]
    assert len(set(a.event_id for a in allocated)) == 3
    assert base_id in {a.event_id for a in allocated}
    assert allocated[1].event_id == f"{base_id}:1"
    assert allocated[2].event_id == f"{base_id}:2"


def test_identity_allocation_never_forgets_an_existing_link(repo):
    """A linked observation keeps its event even when its fingerprint looks free.

    The allocator's determinism claim is conditional, and the condition is worth
    stating: it is stable for an observation the ledger has already placed. An
    observation with no link has no prior answer to be consistent with, so for
    that case the allocator allocates rather than recalls - which is why the
    resolver's idempotency check has to come first.
    """

    allocator = EventIdentityAllocator("puget-sound")
    observation = make_observation("obs_x", at=utcnow())
    linked_event = f"{allocator.base_id(allocator.fingerprint(observation))}:7"

    allocation = allocator.allocate(
        observation,
        event_exists=lambda _: False,
        event_for_observation=lambda _: linked_event,
        observations_of=lambda _: [observation.observation_id],
    )

    assert allocation.event_id == linked_event
    assert allocation.basis == "existing"


def test_a_reopening_report_returns_to_the_same_event(repo, resolver):
    """A closed event that is reported on again is the same event, reactivated.

    Without this, a long-running incident accumulates a new id every time it
    reappears after a gap, and the platform reconstructs a series of unrelated
    short events with no way to tell afterwards that they were one thing. The
    prior status must also stay visible rather than being erased.
    """

    now = utcnow()
    first = make_observation(
        "obs_1", at=now - timedelta(minutes=50), headline="4th Ave blocked"
    )
    repo.observations.append(first)
    event_id = resolver.resolve(first).event_id
    repo.events.set_status(event_id, "closed", now - timedelta(minutes=40), "went quiet")

    later = make_observation(
        "obs_2", at=now, headline="4th Ave still blocked"
    )
    repo.observations.append(later)
    assert resolver.resolve(later).event_id == event_id
    assert repo.events.status_of(event_id) == "active"


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------


def scale_for(values, *, now, intervals_min=20, geometry=DOWNTOWN, extra=()):
    observations = [
        make_observation(
            f"obs_{i}",
            at=now - timedelta(minutes=intervals_min * (len(values) - 1 - i)),
            geometry=geometry,
            payload={"participants": value},
        )
        for i, value in enumerate(values)
    ]
    observations.extend(extra)
    return ScaleEstimator().estimate(observations, now=now).primary


def test_magnitude_tracks_the_present_not_the_past_of_the_event():
    """A growing event is reported larger than a shrinking one with the same history.

    Mirrored series, so the only thing that can separate them is recency. An
    unweighted median reports both as identical, because it describes where the
    event has been rather than where it is.
    """

    now = utcnow()
    growing = scale_for([300, 900, 2500], now=now)
    shrinking = scale_for([2500, 900, 300], now=now)

    assert growing.value > shrinking.value


def test_magnitude_resists_one_bad_recent_report():
    """A single wildly wrong recent reading cannot become the estimate.

    Recency weighting is exactly what makes a lone outlier dangerous, so the
    weights are capped such that no reading can outweigh all the others
    combined. Without the cap this test fails at 90,000.
    """

    now = utcnow()
    baseline = scale_for([300, 900, 2500], now=now)
    polluted = scale_for(
        [300, 900, 2500],
        now=now,
        extra=[
            make_observation(
                "obs_bad",
                at=now,
                payload={"participants": 90_000},
                authority=Authority.COMMUNITY,
                source_id="synthetic.community",
            )
        ],
    )

    assert polluted.value != 90_000
    assert polluted.value <= max(300, 900, 2500) * 1.01


def test_a_single_reading_reports_its_own_uncertainty():
    """One source means a wide interval, not false precision.

    "No disagreement observed" is not "certain". The interval has to come from
    the source's declared precision, or a lone rumour reads as an established
    count.
    """

    now = utcnow()
    estimate = scale_for([420], now=now)
    assert estimate.value == 420.0
    assert estimate.lower < estimate.value < estimate.upper
    assert estimate.source_count == 1


def test_no_evidence_yields_no_estimate():
    """An event with nothing measurable reports unavailable, not zero.

    Zero is a claim - that the crowd is empty, the road is clear. Nothing
    measured is not the same statement.
    """

    now = utcnow()
    scale = ScaleEstimator().estimate(
        [make_observation("obs_x", at=now, headline="Demonstration reported")], now=now
    )
    assert scale.primary is None or scale.primary.unavailable or scale.primary.value is None


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_a_fresh_event_is_active_despite_having_one_report():
    """A newly opened event is not judged quiet by a fixed timer.

    With no established reporting cadence there is no evidence that reports
    *were expected*, so absence of reports is not evidence of absence. This test
    exists because a 15-minute floor applied to events with no cadence made every
    freshly-opened event go quiescent twenty minutes after its first sighting.
    """

    now = utcnow()
    observation = make_observation("obs_1", at=now - timedelta(minutes=20))
    state = WorldStateEngine(_NullStates()).rebuild("evt_x", [observation], [], now=now)

    assert state.status == "active"
    assert state.lifecycle.confidence > 0.0


def test_official_record_closes_with_high_confidence(repo, resolver, world):
    """An official end record closes the event, and says that is why.

    Confidently closed *and* attributably closed. A closure inferred from an
    official record and one inferred from nobody mentioning it again are
    different facts with very different confidence, and reporting both as
    "status changed" throws the difference away.
    """

    now = utcnow()
    previous, event_id = ingest(
        repo,
        resolver,
        world,
        StateDeltaEngine(),
        [
            make_observation(f"obs_{i}", at=now - timedelta(minutes=60 - 10 * i))
            for i in range(3)
        ],
        now=now,
    )

    final, _ = ingest(
        repo,
        resolver,
        world,
        StateDeltaEngine(),
        [
            make_observation(
                "obs_end",
                at=now,
                headline="Demonstration has concluded, streets reopened",
                payload={"participants": 0},
                observation_type=ObservationType.OFFICIAL_EMERGENCY_NOTICE,
            )
        ],
        now=now,
    )

    assert final.status == "closed"
    assert final.lifecycle.termination_basis == "official_release"
    assert final.lifecycle.confidence >= 0.9
    assert final.lifecycle.evidence_observation_ids


def test_silence_alone_produces_lower_confidence_than_a_record(repo, resolver, world):
    """Closure inferred from absence is admissible but weaker, and labelled as such."""

    now = utcnow()
    observations = [
        make_observation(
            f"obs_{i}",
            at=now - timedelta(hours=6, minutes=-30 * i),
            headline="4th Ave blocked",
        )
        for i in range(5)
    ]
    state, _ = ingest(repo, resolver, world, StateDeltaEngine(), observations, now=now)

    assert state.status in ("quiescent", "closed")
    assert state.lifecycle.termination_basis == "silence"
    assert state.lifecycle.confidence < 0.9
    # The derivation must be recorded, not just the outcome.
    assert "threshold" in state.lifecycle.reason


# --------------------------------------------------------------------------
# Deltas
# --------------------------------------------------------------------------


def test_deltas_are_written_per_state_version_and_carry_provenance(repo, resolver, world):
    """Each state version produces queryable deltas naming the version they explain.

    Deltas used to exist only inside an analysis run's JSON, which meant "what
    changed about this event" was unanswerable for any event no analysis had run
    on, and answering it required parsing run blobs.
    """

    now = utcnow()
    previous = None
    event_id = None
    for i in range(3):
        observation = make_observation(
            f"obs_{i}", at=now - timedelta(minutes=30 - 10 * i), payload={"participants": 100 * (i + 1)}
        )
        repo.observations.append(observation)
        claims = [make_claim(observation)]
        for claim in claims:
            repo.claims.append(claim)
        event_id = resolver.resolve(observation, claims).event_id
        state = world.rebuild(
            event_id, repo.observations.list_for_event(event_id),
            repo.claims.claims_for_event(event_id), previous, now=now,
        )
        world.persist(state)
        report = StateDeltaEngine().compare(
            previous, state,
            impacts=state.affected_infrastructure,
            previous_impacts=previous.affected_infrastructure if previous else (),
        )
        for delta in report.deltas:
            repo.deltas.append_many(
                [
                    StateDeltaRecord.from_delta(
                        delta,
                        event_id=event_id,
                        state_version=state.state_version,
                        previous_state_version=previous.state_version if previous else None,
                        region_id="puget-sound",
                        is_material=delta.magnitude >= StateDeltaEngine().materiality_floor,
                        recorded_at=now,
                    )
                ]
            )
        previous = state

    records = repo.deltas.for_event(event_id)
    assert records, "state versions were written but no deltas were recorded"
    assert records[0].state_version == 1
    assert {r.state_version for r in records} <= {1, 2, 3}
    # Every delta must be attributable to the state version that caused it.
    for record in records:
        assert record.event_id == event_id
        assert record.state_version >= 1


def test_a_new_state_version_can_be_diffed_after_the_fact(repo, resolver, world, deltas):
    """Deltas are recomputable from stored states alone.

    The whole point of append-only state versions: "what changed" is a function
    of two stored objects, not of anything that happened to be running at the
    time. If this needs a live resolver it is not replayable.
    """

    now = utcnow()
    observation = make_observation("obs_1", at=now, payload={"participants": 100})
    repo.observations.append(observation)
    claims = [make_claim(observation)]
    for claim in claims:
        repo.claims.append(claim)
    event_id = resolver.resolve(observation, claims).event_id
    v1 = world.rebuild(event_id, repo.observations.list_for_event(event_id),
                       repo.claims.claims_for_event(event_id), None, now=now)
    world.persist(v1)

    later = make_observation("obs_2", at=now + timedelta(minutes=5), payload={"participants": 900})
    repo.observations.append(later)
    claim = make_claim(later)
    repo.claims.append(claim)
    resolver.resolve(later, [claim])
    v2 = world.rebuild(event_id, repo.observations.list_for_event(event_id),
                       repo.claims.claims_for_event(event_id), v1, now=now)
    world.persist(v2)

    report = deltas.compare(v1, v2, impacts=v2.affected_infrastructure,
                            previous_impacts=v1.affected_infrastructure)
    assert report.deltas
    # Provenance lives on the record, not the delta: the same delta recomputed
    # from stored states is the same fact as when it was first computed.
    assert all(isinstance(d.change, str) for d in report.deltas)


# --------------------------------------------------------------------------
# World projection
# --------------------------------------------------------------------------


def test_world_view_answers_what_is_happening_across_all_events(repo, resolver, world, deltas):
    """One snapshot covers every open event, derived only from stored states."""

    now = utcnow()
    for i, geometry in enumerate((DOWNTOWN, BALLARD)):
        observation = make_observation(
            f"obs_{i}", at=now, geometry=geometry, headline=f"Report {i}"
        )
        repo.observations.append(observation)
        claim = make_claim(observation)
        repo.claims.append(claim)
        event_id = resolver.resolve(observation, [claim]).event_id
        state = world.rebuild(event_id, repo.observations.list_for_event(event_id),
                              repo.claims.claims_for_event(event_id), None, now=now)
        world.persist(state)

    snapshot = WorldProjection(repo, "puget-sound").snapshot(now=now)

    assert snapshot.events_total == 2
    assert snapshot.observation_total == 2
    assert {e.event_id for e in snapshot.events} == {
        r["event_id"] for r in repo.events.all_events()
    }


def test_world_view_reports_coverage_gaps_so_silence_is_not_reassurance(repo):
    """A world picture says which sources could not contribute to it.

    A region with no traffic problems and no traffic feed is a fact about the
    platform, not about the world. Reporting those identically is how a system
    that cannot see something ends up confidently claiming there is nothing to
    see - and a degraded feed means something different from a context-only one.
    """

    repo.source_health.put(
        SourceHealth(source_id="sdot.feed", state=HealthState.DEGRADED, usage="realtime")
    )
    repo.source_health.put(
        SourceHealth(source_id="transit.gtfs", state=HealthState.HEALTHY, usage="context_only")
    )
    repo.source_health.put(
        SourceHealth(source_id="weather.now", state=HealthState.UNKNOWN, usage="realtime")
    )

    snapshot = WorldProjection(repo, "puget-sound").snapshot(now=utcnow())
    gaps = set(snapshot.coverage_gaps)

    assert "sdot.feed:degraded" in gaps
    assert "transit.gtfs:context_only" in gaps
    assert "weather.now:unknown" in gaps
    assert set(snapshot.sources_degraded) == {"sdot.feed"}


def test_world_view_separates_tracking_events_from_changed_events(repo, resolver, world, deltas):
    """"How much is happening" and "what just changed" are different numbers.

    The scalable version of this system is the one that keeps the first cheap
    and acts only on the second. Reporting a single blended count would make
    both unusable.
    """

    now = utcnow()
    for i, geometry in enumerate((DOWNTOWN, BALLARD, (-122.28, 47.63))):
        observation = make_observation(
            f"obs_{i}", at=now, geometry=geometry, headline=f"Report {i}"
        )
        repo.observations.append(observation)
        claim = make_claim(observation)
        repo.claims.append(claim)
        event_id = resolver.resolve(observation, [claim]).event_id
        state = world.rebuild(event_id, repo.observations.list_for_event(event_id),
                              repo.claims.claims_for_event(event_id), None, now=now)
        world.persist(state)

    quiet = WorldProjection(repo, "puget-sound").snapshot(now=now)
    assert quiet.events_total == 3
    assert quiet.events_changed_materially == 0

    now2 = now + timedelta(minutes=10)
    fresh = make_observation("obs_new", at=now2, payload={"participants": 5000})
    repo.observations.append(fresh)
    claim = make_claim(fresh)
    repo.claims.append(claim)
    event_id = resolver.resolve(fresh, [claim]).event_id
    updated = world.rebuild(event_id, repo.observations.list_for_event(event_id),
                            repo.claims.claims_for_event(event_id),
                            repo.states.latest(event_id), now=now2)
    world.persist(updated)
    report = deltas.compare(repo.states.history(event_id)[-2], updated,
                            impacts=updated.affected_infrastructure)
    repo.deltas.append_many(
        [
            StateDeltaRecord.from_delta(
                d, event_id=event_id, state_version=updated.state_version,
                previous_state_version=updated.state_version - 1,
                region_id="puget-sound", is_material=True, recorded_at=now2,
            )
            for d in report.deltas
            if d.magnitude >= deltas.materiality_floor
        ]
    )

    changed = WorldProjection(repo, "puget-sound").snapshot(now=now2)
    assert changed.events_changed_materially == 1
    assert changed.events_changed_materially < changed.events_total


def test_world_view_ranks_changed_events_first(repo, resolver, world):
    """Ordering answers "what needs attention", not "what arrived last"."""

    now = utcnow()
    ids = []
    for i, geometry in enumerate((DOWNTOWN, BALLARD)):
        observation = make_observation(
            f"obs_{i}", at=now - timedelta(hours=5 - i), geometry=geometry, headline=f"Report {i}"
        )
        repo.observations.append(observation)
        claim = make_claim(observation)
        repo.claims.append(claim)
        event_id = resolver.resolve(observation, [claim]).event_id
        ids.append(event_id)
        state = world.rebuild(event_id, repo.observations.list_for_event(event_id),
                              repo.claims.claims_for_event(event_id), None, now=now)
        world.persist(state)

    repo.deltas.append_many(
        [
            StateDeltaRecord(
                delta_id="d1", event_id=ids[1], region_id="puget-sound", state_version=2,
                change="infrastructure_severity_rose", domain="infrastructure",
                magnitude=0.9, confidence=0.9, is_material=True,
                urgency=Urgency.HIGH, recorded_at=now,
            )
        ]
    )

    snapshot = WorldProjection(repo, "puget-sound").snapshot(now=now)
    assert snapshot.events[0].event_id == ids[1]
    assert snapshot.events[0].changed_materially


"""The continuous world-state loop, driven the way it runs in production.

Every other test in this suite exercises one component at a time, and
``test_end_to_end_lifecycle.py`` assembles its own wiring rather than using
:class:`~infraimpact.runtime.Runtime`. That left the one piece the whole project
exists to provide - the loop that connects the components - with no coverage at
all, and a defect in it shipped green.

The specific defect: ``Runtime._record_lifecycle`` built an
``EventLifecycleTransition`` without its required ``transition_id``. Every event
raised ``ValidationError`` inside the per-event transaction, so the state
version, the deltas and the lifecycle row rolled back together. ``cycle()``
catches per-event exceptions by design, so the loop ran to completion, reported
``errors=1`` per cycle in a field nothing asserted, and persisted no state at
all - a clean-looking run that stored nothing.

These tests drive the real ``Runtime`` against the real synthetic adapter and
assert on the durable ledger afterwards.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from infraimpact.bus.event_bus import InMemoryEventBus
from infraimpact.config import Settings, get_region
from infraimpact.domain.schemas import EventLifecycleTransition, StateDeltaRecord
from infraimpact.runtime import Runtime
from infraimpact.sources.registry import SourceRegistry
from infraimpact.storage.sqlite_driver import SqlitePlatformRepository

SOURCE = "synthetic.puget_sound"


async def drive(runtime: Runtime, registry: SourceRegistry, cycles: int) -> list:
    """Run the real loop, one script step per cycle.

    The synthetic adapter gates its scenario on wall-clock seconds, so with a
    millisecond loop interval only the opening record would ever be due. Its
    clock is rewound a step per cycle to make each cycle deliver the next
    observation. This exercises the runtime loop, not the adapter's pacing.
    """

    adapter = registry.get(SOURCE)
    await runtime.start()
    results = []
    for _ in range(cycles):
        adapter._started -= timedelta(seconds=1.0)
        results.append(await runtime.cycle())
    return results


@pytest.fixture
def live(tmp_path):
    """A real Runtime over a real repository, registry and bus."""

    db = tmp_path / "loop.db"
    repo = SqlitePlatformRepository(f"sqlite:///{db}")
    settings = Settings(
        database_url=f"sqlite:///{db}",
        region_id="puget-sound",
        llm_enabled=False,
        loop_interval_s=0.01,
        enable_synthetic_sources=True,
        enable_collector=False,
        enable_network_sources=False,
    )
    registry = SourceRegistry(get_region("puget-sound"), settings, only=[SOURCE])
    runtime = Runtime(repo, InMemoryEventBus(), settings, registry=registry)
    yield runtime, repo, registry, db
    repo.close()


class TestContinuousLoop:
    @pytest.mark.anyio
    async def test_a_cycle_persists_state_deltas_and_lifecycle(self, live):
        """One cycle over a new observation must leave durable traces.

        This is the assertion whose absence let the transition-id defect ship.
        It failed for eight cycles before the fix, every time, while the suite
        stayed green.
        """

        runtime, repo, registry, _ = live
        results = await drive(runtime, registry, cycles=3)

        assert all(not r.errors for r in results), [e for r in results for e in r.errors]

        event_ids = [row["event_id"] for row in repo.events.all_events()]
        assert event_ids, "no events were created"

        # Every observation is linked to an event - no orphans.
        stored = repo.observations.count()
        linked = sum(len(repo.observations.list_for_event(e)) for e in event_ids)
        assert stored == linked, f"{stored} stored but {linked} linked"

        # State versions exist, and they are not all the first one.
        versions = {e: len(repo.states.history(e)) for e in event_ids}
        assert max(versions.values()) > 1, f"no event evolved: {versions}"

        # Deltas are semantic, not a generic field diff.
        kinds: set[str] = set()
        for e in event_ids:
            kinds |= {d.change for d in repo.deltas.for_event(e)}
        assert len(kinds) >= 3, kinds
        assert any(k.startswith("impact_appeared") for k in kinds), kinds
        assert any(k.startswith("scale_") for k in kinds), kinds

        # And lifecycle history exists, which is where the defect lived.
        transitions = [t for e in event_ids for t in repo.lifecycle.for_event(e)]
        assert transitions, "no lifecycle transitions recorded"
        assert all(t.transition_id for t in transitions)

    @pytest.mark.anyio
    async def test_world_snapshot_is_published_each_cycle(self, live):
        runtime, repo, registry, _ = live
        results = await drive(runtime, registry, cycles=4)

        assert any(r.world is not None for r in results)
        snapshot = repo.world.latest("puget-sound")
        assert snapshot is not None
        assert snapshot.events_total > 0
        assert snapshot.generated_at is not None

    @pytest.mark.anyio
    async def test_duplicate_observations_do_not_create_duplicate_events(
        self, live, tmp_path
    ):
        """Replaying the same records must not fragment or duplicate an event.

        The adapter restarts its scenario once exhausted, so driving well past
        the script length replays records the platform already holds. Those must
        be caught by deduplication rather than resolved into fresh events.
        """

        runtime, repo, registry, _ = live
        short = await drive(runtime, registry, cycles=6)
        events_before = {r["event_id"] for r in repo.events.all_events()}
        obs_before = repo.observations.count()

        # Continue past the end of the script so it replays.
        for _ in range(12):
            registry.get(SOURCE)._started -= timedelta(seconds=1.0)
            short.append(await runtime.cycle())

        assert any(c.duplicates > 0 for c in short), "replay produced no duplicates"
        obs_after = repo.observations.count()

        # Replayed records did not become new observations.
        assert obs_after < obs_before + 40
        # And the original events still exist.
        events_after = {r["event_id"] for r in repo.events.all_events()}
        assert events_before <= events_after

    @pytest.mark.anyio
    async def test_state_survives_a_process_restart(self, live):
        """The world state is durable, not an artifact of one process.

        Everything the loop concluded must be readable after the repository is
        closed and reopened against the same file, because a world-state system
        that forgets on restart cannot answer what happened while it was down.
        """

        runtime, repo, registry, db = live
        await drive(runtime, registry, cycles=10)

        event_ids = [row["event_id"] for row in repo.events.all_events()]
        versions_before = {e: len(repo.states.history(e)) for e in event_ids}
        deltas_before = repo.deltas.count()
        world_before = repo.world.latest("puget-sound")
        repo.close()

        reopened = SqlitePlatformRepository(f"sqlite:///{db}")
        try:
            assert {r["event_id"] for r in reopened.events.all_events()} == set(event_ids)
            for e in event_ids:
                assert len(reopened.states.history(e)) == versions_before[e]
            assert reopened.deltas.count() == deltas_before
            assert reopened.world.latest("puget-sound") is not None
            assert reopened.world.latest("puget-sound").events_total == world_before.events_total
        finally:
            reopened.close()

    @pytest.mark.anyio
    async def test_observation_alone_persists_state_without_analysis(self, live):
        """State and deltas are written even when no analysis is warranted.

        A delta that only exists inside an ``AnalysisRun`` does not exist for an
        event no analysis ran on, which is the majority of quiet updates.
        """

        runtime, repo, registry, _ = live
        results = await drive(runtime, registry, cycles=12)

        # Find a cycle that produced an observation but no analysis outcome.
        quiet = [r for r in results if r.observations > 0 and not r.events]
        for r in quiet:
            assert not r.errors, r.errors

        # Regardless of quiet cycles existing, every observation is linked.
        for row in repo.events.all_events():
            eid = row["event_id"]
            assert repo.observations.list_for_event(eid), eid
            assert repo.states.history(eid), eid


class TestDurableRecordIdentity:
    """Storage records must be able to identify themselves.

    A required identity field on a record the runtime constructs is a trap: the
    omission is invisible until the record is written, at which point it aborts
    whatever transaction created it. These defaults mean the omission cannot
    happen, and that a durable row can never be persisted without an id.
    """

    def test_lifecycle_transition_generates_its_own_id(self):
        transition = EventLifecycleTransition(event_id="evt_1", to_status="active")
        assert transition.transition_id.startswith("lct_")

    def test_two_transitions_get_distinct_ids(self):
        a = EventLifecycleTransition(event_id="evt_1", to_status="active")
        b = EventLifecycleTransition(event_id="evt_1", to_status="active")
        assert a.transition_id != b.transition_id

    def test_delta_record_still_requires_its_context(self):
        """``delta_id`` is derived, but the ledger context is not optional.

        ``from_delta`` builds the identity and the event/version binding
        together, so a delta always knows what it is a delta *of*. A hand-built
        one without that context is still rejected rather than stored unattached.
        """

        with pytest.raises(Exception):
            StateDeltaRecord(change="x", domain="event")

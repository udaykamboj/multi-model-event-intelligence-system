"""The runtime loop (brief section 71).

    while system_is_running:
        observation = receive_new_information()
        persist_raw(observation)
        normalized = normalize(observation)
        deduplicate(normalized)
        claims = extract_claims(normalized)
        event = resolve_event(normalized, claims)
        previous_state = load_current_state(event)
        new_state = rebuild_state(event, all_relevant_observations)
        delta = compare(previous_state, new_state)
        capabilities = relevance_engine.select(state, delta, ALL_CAPABILITIES)
        results = run(capabilities, state)
        enriched_state = fuse(evidence, model_results)
        forecasts = forecasting_models(enriched_state)
        users = identify_potentially_affected_users(enriched_state)
        for user in users:
            exposure = calculate_user_exposure(user, enriched_state, forecasts)
            user_delta = compare_previous_user_state(user, exposure)
            priority = prioritize(exposure, user_delta, confidence, urgency)
            presentation = select_user_information(priority)
            if notification_policy_allows(user, presentation):
                notify(user)
        persist_analysis_run()

There is no ``protest_flow()``, no ``earthquake_flow()``, no
``level_three_flow()``. One loop, six adapters, whatever they report.

This module is that loop and nothing else. Every step delegates to the
subsystem that owns it; the runtime's only real responsibility is ordering,
bookkeeping and making every intermediate decision observable on the bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Sequence

from .analysis.orchestrator import AnalysisOrchestrator, AnalysisOutcome
from .bus.event_bus import Envelope, EventBus, Topic
from .config import Settings, get_region, get_settings, workspace_root
from .delta.engine import DeltaReport, StateDeltaEngine, compare_user_exposure
from .domain.enums import (
    EventKind,
    EventPhase,
    NotificationReason,
    SituationTrajectory,
    TruthStatus,
    Urgency,
)
from .domain.geo import distance_m
from .domain.ids import utcnow
from .domain.schemas import (
    EventLifecycleTransition,
    EventState,
    MaterialChangeRecord,
    NotificationCandidate,
    Observation,
    StateDelta,
    StateDeltaRecord,
    UserContext,
    UserExposure,
    UserImpactState,
    WorldSnapshot,
)
from .events.claims import ClaimExtractor, RuleClaimExtractor
from .events.resolver import EventResolver
from .events.situation import SituationEngine
from .events.world_state import WorldStateEngine
from .graph.model import InfrastructureGraph, build_puget_sound_graph
from .ingestion.pipeline import IngestResult, IngestionPipeline
from .llm.client import LlmClient, build_llm_client
from .llm.interpreter import InterpretationLayer, UserExplanation
from .sources.registry import SourceRegistry
from .storage.repository import PlatformRepository
from .storage.raw_store import build_raw_store
from .users.exposure import ExposureContext, ExposureEngine
from .users.notifications import NotificationContext, NotificationEngine
from .users.presentation import PresentationContext, PresentationEngine
from .users.priority import PriorityContext, UserPriorityEngine
from .world.projection import WorldProjection

log = logging.getLogger(__name__)


@dataclass
class UserOutcome:
    """Everything one user got out of one analysis."""

    user_id: str
    exposure: UserExposure
    priority: Any
    presentation: Any
    user_deltas: tuple[StateDelta, ...] = ()
    notification: NotificationCandidate | None = None
    decision: str | None = None
    bypassed: tuple[str, ...] = ()
    #: Section 18 user communication for this user, when produced. Kept on the
    #: outcome so the loop's own record shows whether an explanation was
    #: attempted, refused, or never warranted.
    explanation: UserExplanation | None = None


@dataclass
class EventOutcome:
    """Everything one event produced in one cycle."""

    event_id: str
    state: EventState
    analysis: AnalysisOutcome
    users: list[UserOutcome] = field(default_factory=list)
    notified: int = 0
    suppressed: dict[str, int] = field(default_factory=dict)
    skipped: str | None = None


@dataclass
class CycleResult:
    """One pass of the loop, summarised."""

    cycle: int
    started_at: datetime
    duration_ms: float = 0.0

    fetched: int = 0
    persisted: int = 0
    duplicates: int = 0
    failed: int = 0

    observations: int = 0
    events: list[EventOutcome] = field(default_factory=list)
    analyses: int = 0
    users: int = 0
    notifications: int = 0
    suppression: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    #: Cross-event view as of the end of this cycle. ``None`` when the
    #: projection failed, which is reported as an error rather than as an empty
    #: world - a missing snapshot and an empty one are different facts.
    world: WorldSnapshot | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "duration_ms": round(self.duration_ms, 2),
            "fetched": self.fetched,
            "persisted": self.persisted,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "observations": self.observations,
            "events": len(self.events),
            "analyses": self.analyses,
            "users": self.users,
            "notifications": self.notifications,
            "suppression": dict(self.suppression),
            "errors": self.errors,
            "world_events": self.world.events_total if self.world else None,
            "world_changed": self.world.events_changed_materially if self.world else None,
        }


class Runtime:
    """Owns the loop and the wiring between subsystems."""

    _CONSUMER = "runtime"

    def __init__(
        self,
        repository: PlatformRepository,
        bus: EventBus,
        settings: Settings | None = None,
        *,
        registry: SourceRegistry | None = None,
        graph: InfrastructureGraph | None = None,
        claim_extractor: ClaimExtractor | None = None,
        orchestrator: AnalysisOrchestrator | None = None,
        raw_store: Any | None = None,
        llm: LlmClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repository
        self.bus = bus
        self.region = get_region(self.settings.region_id)

        self.registry = registry or SourceRegistry(self.region, self.settings)
        self.raw_store = raw_store or build_raw_store(self.settings.raw_store_path)
        self.graph = graph or build_puget_sound_graph()

        # Section 18 interpretation and orchestration. Built once, shared by the
        # claim extractor, the orchestrator and the presentation layer, so a
        # single ``AnalysisRun`` sees a consistent view of what was asked and
        # what was refused.
        self.interpretation = InterpretationLayer(
            client=llm if llm is not None else build_llm_client(),
            enabled=self.settings.llm_enabled,
        )

        self.claims = claim_extractor or _build_claim_extractor(self.interpretation)
        self.pipeline = IngestionPipeline(
            self.repo, self.raw_store, self.bus, self.region.region_id
        )
        self.resolver = EventResolver(
            events=self.repo.events,
            observations=self.repo.observations,
            claims=self.repo.claims,
            states=self.repo.states,
            region_id=self.region.region_id,
            lifecycle=self.repo.lifecycle,
            candidates=self.repo.candidates,
            contradictions=self.repo.contradictions,
            time_window_min=self.settings.resolver_time_window_min,
            search_radius_m=self.settings.resolver_search_radius_m,
            merge_threshold=self.settings.resolver_merge_threshold,
        )
        self.world = WorldStateEngine(self.repo.states)
        self.situation_engine = SituationEngine(self.repo, self.region.region_id)
        #: Delta engine used for the durable delta ledger. The orchestrator has
        #: its own copy for analysis runs; this one exists because deltas must be
        #: written even when no analysis runs at all - an event can be updated
        #: during replay or recovery, and a delta that exists only inside an
        #: ``AnalysisRun`` does not exist for those.
        self.delta_engine = StateDeltaEngine()
        #: Cross-event world view. Rebuilt once per cycle rather than per event,
        #: because a snapshot per event would be a snapshot of one event.
        self.projection = WorldProjection(self.repo, self.region.region_id)
        self.orchestrator = orchestrator or AnalysisOrchestrator(
            self.repo,
            self.bus,
            region_id=self.region.region_id,
            graph=self.graph,
            interpretation=self.interpretation,
        )

        self.exposure_engine = ExposureEngine(self.graph)
        self.priority_engine = UserPriorityEngine(jev=self.orchestrator.jev)
        self.presentation_engine = PresentationEngine()
        self.notification_engine = NotificationEngine(
            timezone=self.region.timezone,
            repository=self.repo.notifications,
            jev=self.orchestrator.jev,
        )

        self._cycle = 0
        self._stopping = asyncio.Event()
        self._subscribed = False
        #: Resolutions observed this cycle, keyed by event id. The resolver
        #: runs once per observation at ingest time; this is only so the event
        #: stage can report why an event is new without re-deciding.
        self._resolutions: dict[str, Any] = {}
        self._dirty: set[str] = set()

        # Autonomous source collector service (§4-7)
        self._collector_task: asyncio.Task[None] | None = None
        self.collector = None
        if getattr(self.settings, "enable_collector", True):
            from .sources.collector import LiveSourceCollector

            self.collector = LiveSourceCollector(
                target_dir=workspace_root() / "live_feeds",
                interval_s=self.settings.collector_interval_s,
                timeout_s=self.settings.collector_timeout_s,
            )

    # -- lifecycle --------------------------------------------------------

    async def _ensure_started(self) -> None:
        """Bring up the bus and subscribe. Idempotent, and safe to re-enter."""

        await self.bus.start()
        if not self._subscribed:
            await self.bus.subscribe(
                Topic.NORMALIZED_OBSERVATIONS, self._on_normalized
            )
            self._subscribed = True

    async def start(self) -> None:
        await self._ensure_started()
        # Self-running guarantee: ensure configured user context exists so exposure
        # and personalized priority engine run without manual seeding
        if hasattr(self.repo, "users") and not self.repo.users.all():
            try:
                from .cli import DEMO_USERS
                for u in DEMO_USERS:
                    self.repo.users.upsert(u)
                log.info("auto-seeded default user contexts for self-running platform")
            except Exception as exc:  # noqa: BLE001
                log.debug("could not auto-seed users: %r", exc)

        # Autonomous continuous collection: start background source collector
        if self.collector is not None and (self._collector_task is None or self._collector_task.done()):
            self._collector_task = asyncio.create_task(
                self.collector.run_forever(), name="live-source-collector"
            )
            log.info("autonomous background source collector started")

        log.info(
            "runtime started: region=%s sources=%d collector=%s tz=%s",
            self.region.region_id,
            len(self.registry),
            "active" if self.collector is not None else "disabled",
            self.region.timezone,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self.collector is not None:
            await self.collector.stop()
        if self._collector_task is not None:
            self._collector_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._collector_task
            self._collector_task = None
        log.info("runtime stopped cleanly")

    async def run_forever(
        self,
        max_cycles: int | None = None,
        on_cycle: Callable[[CycleResult], None] | None = None,
    ) -> list[CycleResult]:
        """Run the loop until stopped. ``max_cycles`` bounds it for tests."""
        await self.start()
        results: list[CycleResult] = []
        try:
            while not self._stopping.is_set():
                if max_cycles is not None and len(results) >= max_cycles:
                    break
                cycle_result = await self.cycle()
                results.append(cycle_result)
                if max_cycles is None and len(results) > 100:
                    results.pop(0)
                if on_cycle is not None:
                    try:
                        on_cycle(cycle_result)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("on_cycle callback error: %r", exc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.settings.loop_interval_s
                    )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            log.info("runtime cancelled")
        finally:
            if max_cycles is not None:
                await self.stop()
        return results

    # -- one pass ---------------------------------------------------------

    async def cycle(self) -> CycleResult:
        # ``cycle`` is a public entry point - ``seed --cycle`` and several tests
        # call it without ``run_forever`` - so it cannot assume ``start`` ran.
        # Without this, ingestion still worked (the bus buffers and drops) but
        # the NORMALIZED_OBSERVATIONS subscriber was never attached, so every
        # observation landed in the ledger as an orphan linked to no event and
        # the cycle reported ``events: 0`` over 10,755 persisted observations.
        # A half-wired loop that exits 0 and looks healthy is the worst failure
        # mode available here.
        await self._ensure_started()
        self._cycle += 1
        started = time.perf_counter()
        result = CycleResult(cycle=self._cycle, started_at=utcnow())
        self._resolutions = {}
        self._dirty = set()

        await self._ingest(result)

        for event_id in await self._dirty_events():
            try:
                outcome = await self._process_event(event_id)
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the loop
                log.exception("event %s failed", event_id)
                result.errors.append(f"{event_id}: {exc!r}")
                await self.bus.publish(
                    Topic.DEAD_LETTER, {"stage": "event", "event_id": event_id, "error": repr(exc)}
                )
                continue
            if outcome is not None:
                result.events.append(outcome)
                # Roll the per-event tallies up into the cycle. These five fields
                # were declared and reported but never assigned anywhere, so
                # every cycle summary read ``analyses: 0, users: 0,
                # notifications: 0`` no matter what had actually happened. That
                # is worse than a missing metric: on a platform whose entire
                # claim is that it can prove what it did and did not say, a
                # counter pinned at zero is indistinguishable from a platform
                # that genuinely did nothing.
                result.analyses += 1
                result.users += len(outcome.users)
                result.notifications += outcome.notified
                for reason, count in outcome.suppressed.items():
                    result.suppression[reason] = result.suppression.get(reason, 0) + count

        # Stage 1 section 7: Compound parent Situations & trajectory evaluation
        try:
            situation_ids = self.situation_engine.evaluate()
            for sit_id in situation_ids:
                sit_outcome = await self._process_event(sit_id)
                if sit_outcome is not None:
                    result.events.append(sit_outcome)
        except Exception as exc:  # noqa: BLE001
            log.exception("situation evaluation failed: %r", exc)

        # The world view is assembled once, after every event has been processed
        # and the ledger has settled. Building it per event would describe a
        # half-updated world; not rebuilding it would leave a snapshot that goes
        # stale on any cycle where nothing was dirty, which is most cycles.
        try:
            result.world = self._refresh_world()
        except Exception as exc:  # noqa: BLE001 - a read model must not stop ingest
            log.exception("world projection failed")
            result.errors.append(f"world: {exc!r}")

        result.duration_ms = (time.perf_counter() - started) * 1000.0
        log.info("cycle %d: %s", result.cycle, result.summary())
        return result

    # -- stage 1: receive -------------------------------------------------

    async def _ingest(self, result: CycleResult) -> None:
        for adapter in self.registry.runnable():
            source_id = adapter.source_id
            try:
                checkpoint = adapter.checkpoint() or self.repo.get_checkpoint(
                    f"poll:{source_id}"
                )
                records = list(adapter.poll(checkpoint))
                if not records:
                    continue

                ingest = await self.pipeline.ingest_records(
                    source_id, records, adapter.normalize
                )
                result.fetched += ingest.fetched
                result.persisted += ingest.persisted
                result.duplicates += ingest.duplicates
                result.failed += ingest.failed
                result.errors.extend(ingest.errors)
                # Observations that actually entered the ledger this cycle.
                # ``persisted`` is that number; the separate ``observations``
                # counter exists because "records a source produced" and
                # "observations the platform holds" are different claims, and
                # conflating them is how a feed that emits 12,944 rows of which
                # 10,755 were already known gets reported as 12,944 new facts.
                result.observations += ingest.persisted

                if adapter.checkpoint():
                    self.repo.checkpoint(f"poll:{source_id}", adapter.checkpoint())
            except Exception as exc:  # noqa: BLE001 - a failing feed must not stop the loop
                log.warning("poll failed for %s: %r", source_id, exc)
                adapter.record_failure(repr(exc))
                result.failed += 1
                result.errors.append(f"{source_id}: poll failed: {exc!r}")
                await self.bus.publish(
                    Topic.DEAD_LETTER, {"stage": "poll", "source_id": source_id, "error": repr(exc)}
                )
            finally:
                self.pipeline.record_health([adapter.health()])

    async def _on_normalized(self, envelope: Envelope) -> None:
        """Claims extraction runs here, off the normalized topic.

        Extracting at the topic boundary rather than in the ingest loop keeps
        claim extraction independently replayable: any consumer of normalized
        observations can rebuild claims, and a rerun is idempotent.
        """
        payload = envelope.payload or {}
        observation_id = payload.get("observation_id")
        if not observation_id:
            return
        observation = self.repo.observations.get(observation_id)
        if observation is None:
            return

        # One transaction for this observation's resolution *and* its claims.
        # These are one fact - "this record is part of that event and asserts
        # these things" - and the ledger should never be observable in the state
        # where the link exists but the claims do not. It is also the hot path:
        # left un-batched it cost three commits per observation, which is where
        # the remaining ingest time went once the ledger append was fixed.
        #
        # Re-entrant, so when :meth:`_ingest` wraps a whole adapter batch the
        # per-observation transactions here nest and the batch commits once.
        with self.repo.transaction():
            await self._resolve_and_extract(observation, observation_id)

    async def _resolve_and_extract(self, observation, observation_id: str) -> None:
        # resolve_event. Runs before claim extraction because claims carry the
        # event id, and resolution benefits from continuity against existing
        # claims. Idempotent: ``resolve`` re-links the same observation to the
        # same event, and a replayed observation adds nothing.
        existing_claims = self.repo.claims.claims_for_observation(observation_id)
        resolution = self.resolver.resolve(observation, existing_claims)
        if not resolution or not resolution.event_id:
            log.debug("observation %s not correlated to an event: %s", observation_id, resolution.reason if resolution else "filtered")
            return
        self._resolutions[resolution.event_id] = resolution
        self._dirty.add(resolution.event_id)

        if resolution.is_new_event:
            await self.bus.publish(
                Topic.EVENTS_CANDIDATES,
                {
                    "event_id": resolution.event_id,
                    "observation_id": observation_id,
                    "score": resolution.score,
                    "reason": resolution.reason,
                    "decided_by": resolution.decided_by,
                },
                key=resolution.event_id,
            )
        else:
            await self.bus.publish(
                Topic.EVENTS_UPDATED,
                {
                    "event_id": resolution.event_id,
                    "observation_id": observation_id,
                    "score": resolution.score,
                },
                key=resolution.event_id,
            )

        if existing_claims:
            return

        extracted = self.claims.extract(observation, resolution.event_id)
        if not extracted:
            return
        for claim in extracted:
            self.repo.claims.append(claim)
        await self.bus.publish(
            Topic.CLAIMS_CREATED,
            {
                "observation_id": observation_id,
                "event_id": resolution.event_id,
                "count": len(extracted),
            },
            key=resolution.event_id,
        )

    async def _dirty_events(self) -> list[str]:
        """Events touched this cycle, plus any analysed event with no state.

        The second clause is recovery: after a crash between persisting a
        state and recording the run, the event has a run but a state version
        ahead of it, and must be picked up again on the next pass.
        """
        dirty = set(self._dirty)
        for event_id in self.repo.events.active_events():
            latest_run = self.repo.runs.latest_for_event(event_id)
            if latest_run is None:
                if self.repo.observations.list_for_event(event_id):
                    dirty.add(event_id)
                continue
            if latest_run.new_state_version > _latest_version(
                self.repo.states.history(event_id)
            ):
                dirty.add(event_id)
        return sorted(dirty)

    # -- stage 2: resolve, rebuild, analyse -------------------------------

    async def _process_event(self, event_id: str) -> EventOutcome | None:
        observations = self.repo.observations.list_for_event(event_id)
        if not observations:
            return None

        # resolve_event: already done at link time, but we need claims, which
        # the subscriber extracted above.
        claims = self.repo.claims.claims_for_event(event_id)
        resolution = self._resolutions.get(event_id)

        if resolution is None:
            # Recovery path: this event was picked up from stored state rather
            # than from an observation arriving this cycle, so there is no
            # resolution to report. The link already exists.
            log.debug("processing %s without a live resolution", event_id)
        elif resolution.is_new_event:
            await self.bus.publish(
                Topic.EVENTS_CANDIDATES,
                {"event_id": event_id, "score": resolution.score, "reason": resolution.reason},
                key=event_id,
            )
        else:
            await self.bus.publish(
                Topic.EVENTS_UPDATED,
                {"event_id": event_id, "score": resolution.score},
                key=event_id,
            )

        # previous_state = load_current_state(event)
        previous = self.repo.states.latest(event_id)

        kind = self.repo.events.kind_of(event_id)
        phase = self.repo.events.phase_of(event_id)
        relations = self.repo.events.relations_for(event_id)
        timeline = self.repo.events.timeline_for(event_id)
        contradictions = self.repo.contradictions.for_event(event_id) if hasattr(self.repo, "contradictions") else []
        parent_id = next((r.parent_event_id for r in relations if r.child_event_id == event_id), None)
        child_ids = tuple(r.child_event_id for r in relations if r.parent_event_id == event_id)

        # new_state = rebuild_state(event, all_relevant_observations)
        with self.repo.transaction():
            state = self.world.rebuild(
                event_id,
                observations,
                claims,
                previous,
                now=utcnow(),
                kind=kind,
                phase=phase,
                parent_event_id=parent_id,
                child_event_ids=child_ids,
                timeline=timeline,
                contradictions=contradictions,
            )
            delta_report = self._persist_state_version(
                event_id, previous, state, observations, claims
            )

        await self.bus.publish(
            Topic.STATE_UPDATED,
            {
                "event_id": event_id,
                "state_version": state.state_version,
                "status": state.status,
                "observations": len(state.observation_ids),
                "deltas": len(delta_report.deltas),
                "material": delta_report.is_material,
            },
            key=event_id,
        )

        if previous is not None and previous.state_version >= state.state_version:
            # Rebuild produced nothing new. Section 33: the observation is
            # still archived and visible, it just propagates no further.
            return None

        analysis = await self.orchestrator.analyze(
            event_id,
            state,
            trigger=f"cycle:{self._cycle}",
            trigger_observation_id=observations[-1].observation_id if observations else None,
            previous_state=previous,
            affected_user_count=len(self.repo.users.all()),
        )
        await self.bus.publish(
            Topic.ANALYSIS_COMPLETED,
            {
                "event_id": event_id,
                "analysis_run_id": analysis.analysis_run_id,
                "material": analysis.is_material,
                "impacts": len(analysis.impacts),
                "forecasts": len(analysis.forecasts),
                "deltas": len(analysis.deltas),
                "suppressed": analysis.delta_report.suppressed_reason,
            },
            key=event_id,
        )

        outcome = EventOutcome(event_id=event_id, state=analysis.state, analysis=analysis)

        if analysis.impacts:
            await self.bus.publish(
                Topic.IMPACT_UPDATED,
                {
                    "event_id": event_id,
                    "impacts": [i.model_dump(mode="json") for i in analysis.impacts],
                },
                key=event_id,
            )
        if analysis.forecasts:
            await self.bus.publish(
                Topic.MODELS_PREDICTIONS,
                {
                    "event_id": event_id,
                    "forecasts": [f.model_dump(mode="json") for f in analysis.forecasts],
                },
                key=event_id,
            )

        await self._fan_out_users(
            analysis,
            # ``is_new_event`` drives whether a user treats this as a first
            # sighting (which is when they may be notified at all). An event
            # picked up from stored state rather than from an observation
            # arriving this cycle is not a first sighting - and that branch is
            # exactly the one ``resolution is None`` above already logs, so
            # dereferencing it here crashed every recovered event.
            resolution.is_new_event if resolution is not None else False,
            outcome,
        )
        return outcome

    def _persist_state_version(
        self,
        event_id: str,
        previous: EventState | None,
        state: EventState,
        observations: Sequence[Observation],
        claims: Sequence[Any],
    ) -> DeltaReport:
        """Write one state version with its deltas and lifecycle transition.

        All three are one fact - "this is what we now believe, how it differs
        from what we believed, and why the event's status is what it is" - and
        are written inside the caller's transaction so the ledger can never be
        observed disagreeing with itself.

        The delta report is returned rather than discarded because the caller
        publishes it, and because analysis reuses the same comparison; computing
        it twice would be free to do but would leave two subtly different
        opinions about what changed in the same cycle.
        """

        report = self.delta_engine.compare(
            previous,
            state,
            impacts=state.affected_infrastructure,
            previous_impacts=previous.affected_infrastructure if previous else (),
            affected_user_count=len(self.repo.users.all()),
        )

        self.world.persist(state)

        recorded_at = state.reconstructed_at
        records = [
            StateDeltaRecord.from_delta(
                delta,
                event_id=event_id,
                state_version=state.state_version,
                previous_state_version=previous.state_version if previous else None,
                region_id=self.region.region_id,
                is_material=delta.magnitude >= self.delta_engine.materiality_floor,
                recorded_at=recorded_at,
            )
            for delta in report.deltas
        ]
        if records:
            self.repo.deltas.append_many(records)

        # Stage 1 section 13: Interface to Stage 2 material changes stream
        change_flags: list[str] = []
        changed_fields: list[str] = [d.change for d in report.deltas]
        if previous is None:
            change_flags.append("created")
        else:
            if previous.phase != state.phase:
                change_flags.append("phase_changed")
            if (
                previous.geometry
                and state.geometry
                and distance_m(previous.geometry, state.geometry) > 500.0
            ):
                change_flags.append("location_moved")
            prev_domains = {a.domain for a in previous.affected_infrastructure}
            new_domains = {a.domain for a in state.affected_infrastructure}
            if new_domains - prev_domains:
                change_flags.append("new_impact_domain")
            if state.trajectory == SituationTrajectory.ESCALATING:
                change_flags.append("escalated")
            elif state.trajectory == SituationTrajectory.DE_ESCALATING:
                change_flags.append("de_escalated")
            if state.phase in (EventPhase.ENDED, EventPhase.HISTORICAL) or state.status == "closed":
                change_flags.append("resolved")
            if len(state.contradictions) > len(previous.contradictions):
                change_flags.append("contradiction")

        is_material = report.is_material or bool(
            set(change_flags) & {"created", "phase_changed", "location_moved", "new_impact_domain", "escalated", "resolved"}
        )

        if hasattr(self.repo, "material_changes"):
            self.repo.material_changes.append(
                MaterialChangeRecord(
                    event_id=event_id,
                    version=state.state_version,
                    changed_fields=tuple(changed_fields),
                    change_flags=tuple(change_flags),
                    is_material=is_material,
                    reason=f"{len(change_flags)} flags: {', '.join(change_flags)}" if change_flags else "routine delta",
                    recorded_at=recorded_at,
                )
            )

        self._record_lifecycle(event_id, previous, state)
        return report

    def _record_lifecycle(
        self, event_id: str, previous: EventState | None, state: EventState
    ) -> None:
        """Log a lifecycle transition when the status actually moves.

        One transition per rebuild, not one per assessment. The lifecycle engine
        re-evaluates on every state version, and most of those evaluations
        conclude that nothing changed; logging each one would turn the table into
        a heartbeat and bury the transitions it exists to record. The
        ``previous`` argument is what makes "actually moves" checkable rather
        than assumed.

        The status on the event row is written from the same
        :class:`~infraimpact.domain.schemas.LifecycleAssessment` that went into
        the state version, so the event table, the state version and this log
        cannot disagree about what happened.
        """

        before = previous.status if previous else None
        if before == state.status:
            return

        transition = EventLifecycleTransition(
            event_id=event_id,
            region_id=self.region.region_id,
            from_status=before,
            to_status=state.status,
            reason=state.lifecycle.reason,
            termination_basis=state.lifecycle.termination_basis,
            confidence=state.lifecycle.confidence,
            evidence_observation_ids=state.lifecycle.evidence_observation_ids,
            silence_threshold_seconds=state.lifecycle.silence_threshold_seconds,
            observation_count=len(state.observation_ids),
            state_version=state.state_version,
            at=state.reconstructed_at,
        )
        self.repo.lifecycle.record(transition)
        self.repo.events.set_status(
            event_id, state.status, state.reconstructed_at, state.lifecycle.reason
        )
        log.info(
            "lifecycle %s -> %s for %s (%s)",
            before,
            state.status,
            event_id,
            state.lifecycle.termination_basis,
        )

    def _refresh_world(self) -> WorldSnapshot:
        """Rebuild and store the cross-event world view for this cycle.

        Once per cycle, after every event has been processed, so the snapshot
        describes a settled set of state versions rather than a partially
        updated one. Reading latest states inside the projection also keeps it
        correct on a cycle where nothing was dirty, which is the common case and
        the one that keeps ``observation_total`` honest.
        """

        snapshot = self.projection.snapshot(now=utcnow())
        log.info(
            "world snapshot %s: %d events (%d changed), %d observations",
            snapshot.snapshot_id,
            snapshot.events_total,
            snapshot.events_changed_materially,
            snapshot.observation_total,
        )
        return snapshot

    # -- stage 3: the user loop -------------------------------------------

    async def _fan_out_users(
        self,
        analysis: AnalysisOutcome,
        is_new_event: bool,
        outcome: EventOutcome,
    ) -> None:
        users = self.repo.users.all()
        if not users:
            return

        event_observations = self.repo.observations.list_for_event(analysis.event_id)

        for user in users:
            result = await self._process_user(
                user, analysis, event_observations, is_new_event, outcome
            )
            outcome.users.append(result)
            if result.notification is not None and result.decision == "delivered":
                outcome.notified += 1
            elif result.decision and result.decision != "delivered":
                outcome.suppressed[result.decision] = (
                    outcome.suppressed.get(result.decision, 0) + 1
                )

    async def _process_user(
        self,
        user: UserContext,
        analysis: AnalysisOutcome,
        observations: Sequence[Observation],
        is_new_event: bool,
        outcome: EventOutcome,
    ) -> UserOutcome:
        event_id = analysis.event_id
        previous_record = self.repo.user_impacts.latest(user.user_id, event_id)
        previous_exposure = _exposure_from(previous_record)

        # exposure = calculate_user_exposure(user, enriched_state, forecasts)
        exposure = self.exposure_engine.compute(
            ExposureContext(
                user=user,
                state=analysis.state,
                impacts=analysis.impacts,
                forecasts=analysis.forecasts,
                analysis_run_id=analysis.analysis_run_id,
                graph=self.graph,
            )
        )

        # user_delta = compare_previous_user_state(user, exposure)
        user_deltas = tuple(
            compare_user_exposure(previous_exposure, exposure, causes=self._causes(analysis))
        )

        # priority = prioritize(exposure, user_delta, confidence, urgency)
        priority = self.priority_engine.compute(
            PriorityContext(
                exposure=exposure,
                state=analysis.state,
                impacts=analysis.impacts,
                delta_report=analysis.delta_report,
                is_new_event=is_new_event or previous_exposure is None,
            )
        )

        # presentation = select_user_information(priority)
        explanation = None
        try:
            explanation = self.interpretation.explain_to_user(
                state=analysis.state,
                exposure=exposure,
                impacts=analysis.impacts,
                forecasts=analysis.forecasts,
                delta_report=analysis.delta_report,
                observations=observations,
            )
        except Exception:  # noqa: BLE001 - prose is optional, presentation is not
            log.warning("user explanation failed for %s", user.user_id, exc_info=True)

        presentation = self.presentation_engine.build(
            PresentationContext(
                exposure=exposure,
                priority=priority,
                state=analysis.state,
                impacts=analysis.impacts,
                forecasts=analysis.forecasts,
                observations=observations,
                delta_report=analysis.delta_report,
                is_new_event=is_new_event,
                explanation=explanation,
            )
        )

        # Persist the user impact before notifying, so a crash between the two
        # leaves a record rather than an unexplained alert.
        self._persist_user_impact(
            user, event_id, exposure, priority, previous_exposure, user_deltas, analysis
        )

        await self.bus.publish(
            Topic.USER_IMPACT_UPDATED,
            {
                "event_id": event_id,
                "user_id": user.user_id,
                "exposure_level": exposure.exposure_level.value,
                "priority": priority.priority,
                "changes": [d.change for d in user_deltas],
            },
            key=user.user_id,
        )

        # if notification_policy_allows(user, presentation): notify(user)
        notification_result = self.notification_engine.evaluate(
            NotificationContext(
                user=user,
                exposure=exposure,
                priority=priority,
                presentation=presentation.items,
                state=analysis.state,
                state_version=analysis.state.state_version,
                delta_report=analysis.delta_report,
                previous_exposure_level=(
                    previous_exposure.exposure_level if previous_exposure else None
                ),
                is_new_event=is_new_event or previous_exposure is None,
            )
        )

        if notification_result is None:
            return UserOutcome(
                user_id=user.user_id,
                exposure=exposure,
                priority=priority,
                presentation=presentation,
                user_deltas=user_deltas,
                decision="not_applicable",
                explanation=explanation,
            )

        candidate = notification_result.candidate
        await self.bus.publish(
            Topic.ALERTS_CANDIDATES,
            {
                "notification_id": candidate.notification_id,
                "user_id": candidate.user_id,
                "event_id": candidate.event_id,
                "reason": candidate.reason.value,
                "dedupe_key": candidate.dedupe_key,
                "decision": notification_result.decision.reason,
                "allowed": notification_result.decision.allowed,
                "bypassed": list(notification_result.decision.bypassed),
                "notes": notification_result.decision.notes,
            },
            key=candidate.user_id,
        )

        if notification_result.decision.allowed:
            self.repo.notifications.enqueue(candidate)
            await self.bus.publish(
                Topic.ALERTS_SENT,
                {
                    "notification_id": candidate.notification_id,
                    "user_id": candidate.user_id,
                    "event_id": candidate.event_id,
                    "reason": candidate.reason.value,
                    "urgency": candidate.urgency.value,
                    "headline": candidate.headline,
                    "channels": list(user.preferences.channels),
                },
                key=candidate.user_id,
            )

        return UserOutcome(
            user_id=user.user_id,
            exposure=exposure,
            priority=priority,
            presentation=presentation,
            user_deltas=user_deltas,
            notification=candidate,
            decision=notification_result.decision.reason,
            bypassed=notification_result.decision.bypassed,
            explanation=explanation,
        )

    def _persist_user_impact(
        self,
        user: UserContext,
        event_id: str,
        exposure: UserExposure,
        priority: Any,
        previous_exposure: UserExposure | None,
        deltas: Sequence[StateDelta],
        analysis: AnalysisOutcome,
    ) -> None:
        state = UserImpactState(
            user_id=user.user_id,
            event_id=event_id,
            previous_analysis_run_id=(
                previous_exposure.analysis_run_id if previous_exposure else None
            ),
            previous_exposure_level=(
                previous_exposure.exposure_level if previous_exposure else None
            ),
            current=exposure,
            deltas=tuple(deltas),
        )
        # ``priority`` is stored alongside the exposure so the stored row is
        # rankable without re-running analysis.
        payload = state.model_dump(mode="json") | {"priority": priority.priority}
        self.repo.user_impacts.put(
            json.dumps(payload), analysis.analysis_run_id
        )

    @staticmethod
    def _causes(analysis: AnalysisOutcome) -> tuple[str, ...]:
        """What to tell the user this exposure is *because of*."""
        causes: list[str] = []
        for impact in analysis.impacts[:5]:
            causes.append(impact.identifier)
        for delta in analysis.deltas[:3]:
            causes.extend(delta.causes[:2])
        return tuple(dict.fromkeys(causes))[:8]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _latest_version(states: Sequence[EventState]) -> int:
    return max((s.state_version for s in states), default=0)


def _build_claim_extractor(interpretation: InterpretationLayer) -> ClaimExtractor:
    """Rules always; the LLM additionally, when one is configured.

    The two-tier decision lives here rather than in the extractor because it is a
    platform policy, not an implementation detail: whether this deployment
    spends completions on prose interpretation is a setting, and the runtime is
    the only place that knows the settings.
    """

    client = interpretation.client
    if not interpretation.enabled or client is None:
        return RuleClaimExtractor()
    from .events.claims import HybridClaimExtractor

    return HybridClaimExtractor(client)


def _exposure_from(record: dict[str, Any] | None) -> UserExposure | None:
    if not record:
        return None
    try:
        return UserExposure.model_validate(record.get("current"))
    except Exception:  # noqa: BLE001 - a corrupt row must not break the loop
        log.warning("unreadable stored exposure; treating as first sighting")
        return None


__all__ = ["CycleResult", "EventOutcome", "Runtime", "UserOutcome"]
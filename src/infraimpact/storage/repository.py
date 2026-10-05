"""Repository interfaces.

The persistence port. Concrete drivers (``sqlite``, ``postgres``) implement
these protocols so the analysis layer never knows the database. This is what
lets the platform run on SQLite locally while production targets PostGIS
(section 28/42).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..domain.enums import EventKind, EventPhase
from ..domain.schemas import (
    AnalysisRun,
    Claim,
    Contradiction,
    CorrelationCandidate,
    EventLifecycleTransition,
    EventRelation,
    EventState,
    MaterialChangeRecord,
    NotificationCandidate,
    Observation,
    RouteProfile,
    SavedPlace,
    SourceHealth,
    SourceRecord,
    StateDeltaRecord,
    TimelineEntry,
    UserContext,
    WorldSnapshot,
)


class ObservationRepository(ABC):
    @abstractmethod
    def append(self, observation: Observation) -> bool:
        """Persist one immutable observation. Returns False if deduplicated."""

    @abstractmethod
    def get(self, observation_id: str) -> Observation | None: ...

    @abstractmethod
    def has_dedupe_key(self, dedupe_key: str) -> bool: ...

    @abstractmethod
    def list_for_event(self, event_id: str, limit: int | None = None) -> list[Observation]: ...

    @abstractmethod
    def event_ids_between(self, start: datetime, end: datetime) -> list[str]:
        """Event ids having at least one observation with ``event_time`` in range.

        This is the resolver's candidate prefilter, and it exists because the
        alternative is unusable. Resolution runs once per *observation*, so
        asking "which active events could this belong to?" by scanning every
        active event is O(observations x events) database round-trips - about
        twelve million for a full cold ingest of the supplied feeds, which took
        twenty minutes and looked like an LLM latency problem. The time window
        was already computed and then discarded (``_ = window_start``) with the
        comment "cheap deterministic constraints first", so the constraint was
        documented, named, and not applied.

        The predicate is deliberately *weaker* than the full Python check in
        ``EventResolver._temporally_compatible``, which also requires the whole
        sibling set to fall inside the window. One in-window sibling is
        necessary for compatibility, so this returns a superset of the true
        candidates and can never discard one the full check would keep.
        """

    @abstractmethod
    def list_between(
        self, start: datetime, end: datetime, region_id: str | None = None
    ) -> list[Observation]: ...

    @abstractmethod
    def recent(self, limit: int = 100) -> list[Observation]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def find_event_for_source_record(self, source_id: str, source_record_id: str) -> str | None:
        """Find the event ID linked to a prior observation of this source record."""


class EventRepository(ABC):
    @abstractmethod
    def create(self, event_id: str, first_observed: datetime, region_id: str) -> None: ...

    @abstractmethod
    def ensure(self, event_id: str, first_observed: datetime, region_id: str) -> None: ...

    @abstractmethod
    def link_observation(self, event_id: str, observation_id: str) -> None: ...

    @abstractmethod
    def event_for_observation(self, observation_id: str) -> str | None:
        """The event this observation belongs to, if any.

        This is the reverse of ``link_observation`` and it enforces the invariant
        the whole world-state system rests on: *an observation belongs to at
        most one event, permanently*. Resolution consults it before doing any
        scoring, so reprocessing a source - which is routine, because every feed
        is polled on a timer and re-reads its whole window each time - can never
        fork an event's history into two ids that then reconstruct two different
        fictions from the same evidence.
        """

    @abstractmethod
    def observations_of(self, event_id: str) -> list[str]: ...

    @abstractmethod
    def active_events(self) -> list[str]: ...

    @abstractmethod
    def all_events(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def get(self, event_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def status_of(self, event_id: str) -> str | None:
        """Persisted lifecycle status, or ``None`` if the event is unknown."""

    @abstractmethod
    def set_status(self, event_id: str, status: str, at: datetime, reason: str) -> bool:
        """Record a lifecycle status. Returns False if the row is unknown.

        Separate from :meth:`close` because status moves in both directions - an
        event that has gone quiet becomes active again the moment something is
        reported about it, and a system that can only ever close events cannot
        express that.
        """

    @abstractmethod
    def close(self, event_id: str, at: datetime, reason: str) -> None: ...

    @abstractmethod
    def record_alias(self, alias: str, event_id: str) -> None: ...

    @abstractmethod
    def merge(self, source_event_id: str, target_event_id: str, reason: str) -> None: ...

    # Stage 1: kinds, phases, relations, and timeline
    @abstractmethod
    def set_kind(self, event_id: str, kind: EventKind) -> None: ...

    @abstractmethod
    def kind_of(self, event_id: str) -> EventKind: ...

    @abstractmethod
    def set_phase(self, event_id: str, phase: EventPhase) -> None: ...

    @abstractmethod
    def phase_of(self, event_id: str) -> EventPhase: ...

    @abstractmethod
    def add_relation(self, relation: EventRelation) -> None: ...

    @abstractmethod
    def relations_for(self, event_id: str) -> list[EventRelation]: ...

    @abstractmethod
    def add_timeline_entry(self, entry: TimelineEntry) -> None: ...

    @abstractmethod
    def timeline_for(self, event_id: str) -> list[TimelineEntry]: ...


class EventLifecycleRepository(ABC):
    @abstractmethod
    def record(self, transition: EventLifecycleTransition) -> None:
        """Append one lifecycle transition. History is never overwritten."""

    @abstractmethod
    def for_event(self, event_id: str) -> list[EventLifecycleTransition]: ...

    @abstractmethod
    def latest(self, event_id: str) -> EventLifecycleTransition | None: ...


class StateDeltaRepository(ABC):
    @abstractmethod
    def append_many(self, records: Sequence[StateDeltaRecord]) -> int:
        """Write a state version's deltas. Idempotent on ``delta_id``."""

    @abstractmethod
    def for_event(self, event_id: str, limit: int = 200) -> list[StateDeltaRecord]: ...

    @abstractmethod
    def for_state_version(
        self, event_id: str, state_version: int
    ) -> list[StateDeltaRecord]: ...

    @abstractmethod
    def material_since(
        self, event_id: str, since: datetime
    ) -> list[StateDeltaRecord]:
        """Material changes for one event since a moment.

        Backs "what has materially changed about this event", which is the query
        a notification, a stream consumer or a user view all need and which was
        previously only answerable by parsing every analysis run.
        """

    @abstractmethod
    def recent(
        self, region_id: str | None = None, limit: int = 200
    ) -> list[StateDeltaRecord]:
        """Material changes across all events - the region's change feed."""

    @abstractmethod
    def count(self) -> int: ...


class WorldSnapshotRepository(ABC):
    @abstractmethod
    def put(self, snapshot: WorldSnapshot) -> None: ...

    @abstractmethod
    def latest(self, region_id: str) -> WorldSnapshot | None: ...

    @abstractmethod
    def history(self, region_id: str, limit: int = 50) -> list[WorldSnapshot]: ...


class StateRepository(ABC):
    @abstractmethod
    def append_state(self, state: EventState) -> None:
        """Write a new immutable state version. Never overwrite."""

    @abstractmethod
    def latest(self, event_id: str) -> EventState | None: ...

    @abstractmethod
    def latest_all(self, limit: int = 1000) -> list[EventState]:
        """Latest state for every event, in one read.

        The world projection needs this for every event on every cycle. Fetching
        them individually is one query per event per cycle, which is the
        difference between a world view that is cheap to maintain and one that
        is not.
        """

    @abstractmethod
    def version(self, event_id: str, state_version: int) -> EventState | None: ...

    @abstractmethod
    def history(self, event_id: str) -> list[EventState]: ...

    @abstractmethod
    def state_as_of(self, event_id: str, as_of: datetime) -> EventState | None:
        """Section 49: replay - what did we believe at this moment?"""


class ClaimRepository(ABC):
    @abstractmethod
    def append(self, claim: Claim) -> None: ...

    @abstractmethod
    def claims_for_event(self, event_id: str) -> list[Claim]: ...

    @abstractmethod
    def claims_for_observation(self, observation_id: str) -> list[Claim]: ...


class AnalysisRunRepository(ABC):
    @abstractmethod
    def append(self, run: AnalysisRun) -> None: ...

    @abstractmethod
    def latest_for_event(self, event_id: str) -> AnalysisRun | None: ...

    @abstractmethod
    def for_event(self, event_id: str, limit: int = 50) -> list[AnalysisRun]: ...

    @abstractmethod
    def by_id(self, analysis_run_id: str) -> AnalysisRun | None: ...


class UserRepository(ABC):
    @abstractmethod
    def upsert(self, user: UserContext) -> None: ...

    @abstractmethod
    def get(self, user_id: str) -> UserContext | None: ...

    @abstractmethod
    def all(self) -> list[UserContext]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def set_ephemeral_location(
        self, user_id: str, geometry: dict[str, Any] | None, expires_at: datetime | None
    ) -> None: ...

    @abstractmethod
    def add_place(self, user_id: str, place: SavedPlace) -> None: ...

    @abstractmethod
    def add_route(self, user_id: str, route: RouteProfile) -> None: ...


class UserImpactRepository(ABC):
    @abstractmethod
    def put(self, exposure_json: str, analysis_run_id: str) -> None: ...

    @abstractmethod
    def latest(self, user_id: str, event_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]: ...


class NotificationRepository(ABC):
    @abstractmethod
    def enqueue(self, notification: NotificationCandidate) -> bool:
        """Returns False when suppressed by the dedupe key."""

    @abstractmethod
    def pending(self) -> list[NotificationCandidate]: ...

    @abstractmethod
    def mark_sent(self, notification_id: str) -> None: ...

    @abstractmethod
    def seen_dedupe_key(self, dedupe_key: str) -> bool: ...

    @abstractmethod
    def recent(self, limit: int = 50) -> list[NotificationCandidate]: ...


class SourceHealthRepository(ABC):
    @abstractmethod
    def put(self, health: SourceHealth) -> None: ...

    @abstractmethod
    def all(self) -> list[SourceHealth]: ...

    @abstractmethod
    def get(self, source_id: str) -> SourceHealth | None: ...


# --------------------------------------------------------------------------
# Stage 1 Repositories
# --------------------------------------------------------------------------


class SourceRecordRepository(ABC):
    """Stage 1 section 2: Source record ledger, fingerprinting & disappearance."""

    @abstractmethod
    def upsert(self, record: SourceRecord) -> None: ...

    @abstractmethod
    def get(self, source_id: str, source_record_id: str) -> SourceRecord | None: ...

    @abstractmethod
    def list_active(self, source_id: str) -> list[SourceRecord]: ...

    @abstractmethod
    def confirm_present(self, source_id: str, source_record_id: str, fingerprint: str) -> tuple[bool, int]:
        """Returns (fingerprint_changed: bool, version: int). Updates last_confirmed."""
        ...

    @abstractmethod
    def mark_missed(
        self, source_id: str, missing_ids: Sequence[str], threshold: int
    ) -> list[SourceRecord]:
        """Increments missed_polls for missing_ids. Returns records that hit threshold and were marked ended."""
        ...


class CorrelationCandidateRepository(ABC):
    """Stage 1 section 6/12: Review queue for POSSIBLE matches."""

    @abstractmethod
    def append(self, candidate: CorrelationCandidate) -> None: ...

    @abstractmethod
    def list_pending(self, limit: int = 100) -> list[CorrelationCandidate]: ...

    @abstractmethod
    def for_event(self, event_id: str) -> list[CorrelationCandidate]: ...

    @abstractmethod
    def update_status(self, candidate_id: str, status: str) -> bool: ...


class ContradictionRepository(ABC):
    """Stage 1 section 6/10: Claims contradiction ledger."""

    @abstractmethod
    def append(self, contradiction: Contradiction) -> None: ...

    @abstractmethod
    def for_event(self, event_id: str) -> list[Contradiction]: ...

    @abstractmethod
    def unresolved(self, limit: int = 100) -> list[Contradiction]: ...


class MaterialChangeRepository(ABC):
    """Stage 1 section 13: Interface to Stage 2 material changes stream."""

    @abstractmethod
    def append(self, record: MaterialChangeRecord) -> None: ...

    @abstractmethod
    def list_for_event(self, event_id: str) -> list[MaterialChangeRecord]: ...

    @abstractmethod
    def list_recent(self, limit: int = 50) -> list[MaterialChangeRecord]: ...


class PlatformRepository(ABC):
    """Aggregate root handed to the runtime."""

    observations: ObservationRepository
    events: EventRepository
    states: StateRepository
    deltas: StateDeltaRepository
    lifecycle: EventLifecycleRepository
    world: WorldSnapshotRepository
    claims: ClaimRepository
    runs: AnalysisRunRepository
    users: UserRepository
    user_impacts: UserImpactRepository
    notifications: NotificationRepository
    source_health: SourceHealthRepository
    source_records: SourceRecordRepository
    candidates: CorrelationCandidateRepository
    contradictions: ContradictionRepository
    material_changes: MaterialChangeRepository


    @abstractmethod
    def checkpoint(self, consumer: str, offset: str) -> None: ...

    @abstractmethod
    def get_checkpoint(self, consumer: str) -> str | None: ...

    @abstractmethod
    def transaction(self) -> AbstractContextManager[PlatformRepository]:
        """Group writes into one atomic unit. Re-entrant.

        Every repository below commits per statement by default, so a caller that
        does not open one gets a durable store and nothing else. Opening a
        transaction is how a caller says "these writes are one fact": an
        observation that exists but is not yet linked to an event, or an analysis
        run whose state version has not been written, are both states the ledger
        should never be observable in.

        Implementations must be re-entrant and must roll back to the outermost
        boundary on exception. See
        :meth:`infraimpact.storage.sqlite_driver.SqlitePlatformRepository.transaction`.
        """

    @abstractmethod
    def execute_raw(self, sql: str, params: Sequence[Any] | None = None) -> Iterable[dict[str, Any]]:
        """Escape hatch for geospatial queries in the target dialect."""

    @abstractmethod
    def close(self) -> None: ...
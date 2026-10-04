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

from ..domain.schemas import (
    AnalysisRun,
    Claim,
    EventState,
    NotificationCandidate,
    Observation,
    RouteProfile,
    SavedPlace,
    SourceHealth,
    UserContext,
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
    def list_for_event(self, event_id: str) -> list[Observation]: ...

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


class EventRepository(ABC):
    @abstractmethod
    def create(self, event_id: str, first_observed: datetime, region_id: str) -> None: ...

    @abstractmethod
    def ensure(self, event_id: str, first_observed: datetime, region_id: str) -> None: ...

    @abstractmethod
    def link_observation(self, event_id: str, observation_id: str) -> None: ...

    @abstractmethod
    def observations_of(self, event_id: str) -> list[str]: ...

    @abstractmethod
    def active_events(self) -> list[str]: ...

    @abstractmethod
    def all_events(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def close(self, event_id: str, at: datetime, reason: str) -> None: ...

    @abstractmethod
    def record_alias(self, alias: str, event_id: str) -> None: ...

    @abstractmethod
    def merge(self, source_event_id: str, target_event_id: str, reason: str) -> None: ...


class StateRepository(ABC):
    @abstractmethod
    def append_state(self, state: EventState) -> None:
        """Write a new immutable state version. Never overwrite."""

    @abstractmethod
    def latest(self, event_id: str) -> EventState | None: ...

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


class PlatformRepository(ABC):
    """Aggregate root handed to the runtime."""

    observations: ObservationRepository
    events: EventRepository
    states: StateRepository
    claims: ClaimRepository
    runs: AnalysisRunRepository
    users: UserRepository
    user_impacts: UserImpactRepository
    notifications: NotificationRepository
    source_health: SourceHealthRepository

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
"""World projection: the answer to "what is happening right now?"

The world-state engine in :mod:`infraimpact.events.world_state` reconstructs one
event. This module projects every event's current reconstruction into a single
view of the region, and that view is what makes the system's central claim
answerable rather than merely asserted:

    what is happening in the world right now?

Three properties are load-bearing here.

It is a *projection*, not a source of truth. Every field is derived from state
versions that already exist, and the snapshot records the observation count and
generation time it was built from. A world view that cannot be rebuilt from the
ledger is a second, competing account of reality - and two accounts of reality
is the failure mode this whole architecture exists to avoid. Because it is
derived, ``observation_total`` makes staleness detectable: a snapshot claiming
to describe the present while reporting fewer observations than the ledger holds
is recognisably stale instead of quietly authoritative.

It distinguishes *open* from *changed*. Five hundred events being tracked and ten
having materially changed are completely different situations, and the scalable
version of this system is exactly the one that keeps the first number cheap and
acts only on the second. Both are carried, per event
(``changed_materially``) and in aggregate (``events_changed_materially``).

It carries its own coverage. A world picture assembled from four working feeds
looks identical to a world picture assembled from four working feeds while
fifteen others are down, unless the failures are in the picture. So
``coverage_gaps`` and ``sources_degraded`` travel with the snapshot. A region
with no traffic problems and no traffic feed is a statement about the platform,
not about the world, and this is where the difference is recorded.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from ..domain.enums import HealthState
from ..domain.geo import centroid_of
from ..domain.ids import deterministic_id, ensure_utc, utcnow
from ..domain.schemas import (
    EventState,
    SourceHealth,
    StateDeltaRecord,
    WorldEventEntry,
    WorldSnapshot,
)
from ..storage.repository import PlatformRepository

log = logging.getLogger(__name__)

#: Events whose latest state version is older than this are reported but sorted
#: last, so a long-running quiet event never outranks something that just
#: happened. Not a filter - an ordering, because "stale" is not "irrelevant".
_FRESHNESS_SORT_SECONDS = 3600.0


class WorldProjection:
    """Builds the cross-event world snapshot from durable state."""

    def __init__(self, repository: PlatformRepository, region_id: str) -> None:
        self.repo = repository
        self.region_id = region_id

    # -- construction -----------------------------------------------------

    def build(
        self,
        *,
        now: datetime | None = None,
        states: Sequence[EventState] | None = None,
        change_window_seconds: float = 3600.0,
        include_closed: bool = False,
    ) -> WorldSnapshot:
        """Assemble the snapshot.

        ``states`` may be supplied to avoid re-reading every event's latest
        state; the runtime already has them in hand from the cycle it just ran.
        Everything else is read, because source health and observation counts are
        facts about the platform rather than about any one event.
        """

        now = now or utcnow()
        source_states = list(states) if states is not None else self.repo.states.latest_all()
        if not include_closed:
            source_states = [s for s in source_states if s.status != "closed"]

        changed = self._material_change_index(now, change_window_seconds)
        entries = [self._entry(state, changed, now) for state in source_states]
        entries.sort(key=lambda e: self._sort_key(e, now), reverse=True)

        health = self.repo.source_health.all()
        domain_activity: Counter[str] = Counter()
        for entry in entries:
            for domain in entry.affected_domains:
                domain_activity[domain] += 1

        observation_total = self.repo.observations.count()
        statuses = Counter(e.status for e in entries)

        return WorldSnapshot(
            snapshot_id=deterministic_id(
                "wrl", self.region_id, now.isoformat(), observation_total
            ),
            region_id=self.region_id,
            generated_at=now,
            observation_total=observation_total,
            events_total=len(entries),
            events_active=statuses.get("active", 0),
            events_quiescent=statuses.get("quiescent", 0),
            events_closed=statuses.get("closed", 0),
            events_changed_materially=sum(1 for e in entries if e.changed_materially),
            events=tuple(entries),
            domain_activity=dict(sorted(domain_activity.items())),
            source_health={h.source_id: str(h.state) for h in health},
            sources_degraded=tuple(
                sorted(
                    h.source_id
                    for h in health
                    if h.state in (HealthState.DEGRADED, HealthState.OFFLINE)
                )
            ),
            coverage_gaps=self._coverage_gaps(health),
        )

    def persist(self, snapshot: WorldSnapshot) -> WorldSnapshot:
        self.repo.world.put(snapshot)
        return snapshot

    def snapshot(self, *, now: datetime | None = None, persist: bool = True) -> WorldSnapshot:
        built = self.build(now=now)
        return self.persist(built) if persist else built

    def latest(self) -> WorldSnapshot | None:
        return self.repo.world.latest(self.region_id)

    # -- per-event entry --------------------------------------------------

    def _entry(
        self,
        state: EventState,
        changed: dict[str, StateDeltaRecord],
        now: datetime,
    ) -> WorldEventEntry:
        centre = centroid_of(state.geometry) if state.geometry else None
        primary = state.scale.primary
        window = changed.get(state.event_id)
        return WorldEventEntry(
            event_id=state.event_id,
            status=state.status,
            state_version=state.state_version,
            dominant_type=_dominant(state),
            type_distribution=dict(state.event_type_distribution),
            first_observed=state.first_observed,
            last_observed=state.last_observed,
            centroid=(round(centre[0], 6), round(centre[1], 6)) if centre else None,
            footprint_area_m2=float(state.derived.get("footprint_area_m2", 0.0)),
            moving=state.movement.moving,
            movement_speed=state.movement.speed_estimate_m_per_min,
            movement_direction_deg=state.movement.direction_deg,
            affected_domains=tuple(sorted({str(i.domain) for i in state.affected_infrastructure})),
            impact_count=len(state.affected_infrastructure),
            severity_peak=_peak_severity(state),
            evidence_confidence=float(
                state.evidence.vector.get("source_authority", 0.0)
            ),
            independent_source_count=state.evidence.independent_source_count,
            primary_magnitude=primary,
            duration_seconds=state.scale.duration.elapsed_seconds,
            observation_rate_per_hour=state.scale.rate.observation_rate_per_hour,
            rate_trend=state.scale.rate.trend,
            changed_materially=window is not None,
            last_change=window.change if window else None,
            lifecycle_reason=state.lifecycle.reason or None,
        )

    # -- change index -----------------------------------------------------

    def _material_change_index(
        self, now: datetime, window_seconds: float
    ) -> dict[str, StateDeltaRecord]:
        """Strongest material change per event inside the window.

        Reads the durable delta table rather than analysis runs, which is the
        point of that table: this query is answerable for every event on every
        cycle without parsing a single run blob.
        """

        since = ensure_utc(now) - timedelta(seconds=max(0.0, window_seconds))
        records = self.repo.deltas.recent(self.region_id, limit=2000)
        index: dict[str, StateDeltaRecord] = {}
        for record in records:
            if ensure_utc(record.recorded_at) < since:
                continue
            current = index.get(record.event_id)
            if current is None or record.magnitude > current.magnitude:
                index[record.event_id] = record
        return index

    # -- coverage ---------------------------------------------------------

    @staticmethod
    def _coverage_gaps(health: Sequence[SourceHealth]) -> tuple[str, ...]:
        """Signals that cannot currently contribute to the world picture.

        Two distinct causes, both reported, because they call for different
        responses. A source whose usage is not ``realtime`` is working as
        designed, yet nothing it produces can answer "right now": a
        historical-only archive never raises an alert on its own, and a
        context-only source exists precisely so its records inform the world
        without being able to interrupt anyone. A source that is degraded,
        offline, or of unknown state is a gap the platform could close and has
        not.

        Keeping them apart matters because they imply opposite confidence. With a
        context-only feed, silence about traffic means "the context feed has
        nothing to add", which is normal. With a degraded feed, silence about
        traffic means "we may simply not be able to see the traffic", which is
        not. A world view that cannot express that difference will report
        reassuring answers it has not earned.
        """

        gaps: set[str] = set()
        for item in health:
            if item.usage != "realtime":
                gaps.add(f"{item.source_id}:{item.usage}")
            if item.state in (HealthState.DEGRADED, HealthState.OFFLINE):
                gaps.add(f"{item.source_id}:{item.state}")
            if item.state is HealthState.UNKNOWN:
                gaps.add(f"{item.source_id}:unknown")
        return tuple(sorted(gaps))

    # -- ordering ---------------------------------------------------------

    @staticmethod
    def _sort_key(entry: WorldEventEntry, now: datetime) -> tuple[Any, ...]:
        """Most-actionable first.

        Ordered by whether the world actually moved, then by how bad, then by how
        much infrastructure is affected, then by how confident and how
        corroborated we are - so an event with two shaky reports never outranks
        one with eight solid ones.

        Recency is the final tie-break rather than a filter, which is why an
        event that has been quiet for hours still appears: it is simply ranked
        below everything that is happening now. Age is measured against the
        snapshot's own ``generated_at`` so that two builds of the same world view
        order it identically.
        """

        last = entry.last_observed
        age = 0.0 if last is None else (ensure_utc(now) - ensure_utc(last)).total_seconds()
        return (
            1 if entry.changed_materially else 0,
            _severity_rank(entry.severity_peak),
            entry.impact_count,
            entry.evidence_confidence,
            entry.independent_source_count,
            -age,
        )


_SEVERITY_ORDER = {
    "none": 0,
    "low": 1,
    "moderate": 2,
    "high": 3,
    "immediate": 4,
}


def _severity_rank(severity: Any) -> float:
    return float(_SEVERITY_ORDER.get(str(severity), 0))


def _dominant(state: EventState) -> str | None:
    ranked = [
        (name, weight)
        for name, weight in state.event_type_distribution.items()
        if name != "unknown" and weight > 0.0
    ]
    if not ranked:
        return "unknown"
    return max(ranked, key=lambda kv: kv[1])[0]


def _peak_severity(state: EventState) -> Any:
    from ..domain.enums import Urgency

    if not state.affected_infrastructure:
        return Urgency.NONE
    return max(
        state.affected_infrastructure,
        key=lambda i: _SEVERITY_ORDER.get(str(i.severity), 0),
    ).severity


__all__ = ["WorldProjection"]

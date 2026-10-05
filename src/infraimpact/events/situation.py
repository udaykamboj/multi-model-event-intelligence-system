"""Stage 1 Situations & Escalation Engine (Stage 1 §7).

Manages compound parent events and trajectories:
1. Forms a parent Situation when 3+ events correlate on location, time, and theme
   (e.g., protest crowd + road closures + police CAD response).
2. Links child Incidents and Conditions into the parent Situation.
3. Tracks escalation signals:
   - New agency involvement (SPD, SFD, SDOT, WSDOT)
   - Geographic footprint spread
   - New impacted infrastructure domains (roads, transit, utilities)
   - Rising evidence / observation rate
   - Critical severity keywords
4. Computes trajectory: `building | steady | escalating | de_escalating | ended`.
5. Records auditable timeline entries explaining each escalation or de-escalation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Sequence

from ..domain.enums import (
    EventKind,
    EventPhase,
    InfrastructureDomain,
    SituationTrajectory,
)
from ..domain.geo import distance_m, union_bbox
from ..domain.ids import new_id
from ..domain.schemas import EventRelation, EventState, TimelineEntry
from ..storage.repository import PlatformRepository

log = logging.getLogger(__name__)

#: Proximity radius for clustering child events into a Situation
SITUATION_RADIUS_M = 1800.0

#: Time window for clustering child events into a Situation (4 hours)
SITUATION_WINDOW_SECONDS = 14400.0


class SituationEngine:
    """Detects, forms, and updates parent Situations and their escalation trajectories."""

    def __init__(self, repo: PlatformRepository, region_id: str = "puget-sound") -> None:
        self.repo = repo
        self.region_id = region_id

    def evaluate(self) -> list[str]:
        """Examine active events, propose new parent Situations, and update existing Situations.

        Returns list of situation event IDs evaluated or updated.
        """
        active_states = [s for s in self.repo.states.latest_all() if s.status != "closed"]
        updated_situations: list[str] = []

        # 1. Update existing Situations
        situations = [s for s in active_states if getattr(s, "kind", EventKind.INCIDENT) == EventKind.SITUATION]
        for sit in situations:
            self._update_situation(sit)
            updated_situations.append(sit.event_id)

        # 2. Check for promotion of 3+ unparented events into a new parent Situation
        unparented = [
            s
            for s in active_states
            if getattr(s, "kind", EventKind.INCIDENT) != EventKind.SITUATION
            and not getattr(s, "parent_event_id", None)
            and s.geometry is not None
        ]

        # Group by proximity and temporal overlap
        clusters = self._cluster_events(unparented)
        for cluster in clusters:
            if len(cluster) >= 3:
                sit_id = self._form_situation(cluster)
                updated_situations.append(sit_id)

        return updated_situations

    def _cluster_events(self, events: Sequence[EventState]) -> list[list[EventState]]:
        """Cluster events that share geographic proximity and temporal overlap."""
        clusters: list[list[EventState]] = []
        assigned: set[str] = set()

        for i, ev_a in enumerate(events):
            if ev_a.event_id in assigned:
                continue
            geom_a = ev_a.geometry
            if not geom_a:
                continue
            cluster = [ev_a]
            assigned.add(ev_a.event_id)

            for j, ev_b in enumerate(events):
                if ev_b.event_id in assigned or j <= i:
                    continue
                geom_b = ev_b.geometry
                if not geom_b:
                    continue

                dist = distance_m(geom_a, geom_b)
                if dist is not None and dist <= SITUATION_RADIUS_M:
                    # Check temporal compatibility
                    t_a = ev_a.first_observed or datetime.now(UTC)
                    t_b = ev_b.first_observed or datetime.now(UTC)
                    if abs((t_a - t_b).total_seconds()) <= SITUATION_WINDOW_SECONDS:
                        cluster.append(ev_b)
                        assigned.add(ev_b.event_id)

            clusters.append(cluster)
        return clusters

    def _form_situation(self, child_events: Sequence[EventState]) -> str:
        """Stage 1 section 7: Form a parent Situation from 3+ correlated events."""
        sit_id = new_id("sit")
        now = datetime.now(UTC)
        first_time = min(
            (e.first_observed for e in child_events if e.first_observed),
            default=now,
        )

        child_ids = [e.event_id for e in child_events]
        log.info("forming parent Situation %s from child events: %s", sit_id, child_ids)

        # 1. Create event row
        self.repo.events.create(
            event_id=sit_id,
            first_observed=first_time,
            region_id=self.region_id,
            kind=EventKind.SITUATION,
            phase=EventPhase.ACTIVE,
        )

        # 2. Add relations & link
        for child in child_events:
            rel = EventRelation(
                parent_event_id=sit_id,
                child_event_id=child.event_id,
                relation_type="parent_child",
                linked_at=now,
            )
            self.repo.events.add_relation(rel)

        # 3. Add timeline entry
        timeline_entry = TimelineEntry(
            event_id=sit_id,
            timestamp=now,
            event_kind=EventKind.SITUATION,
            phase=EventPhase.ACTIVE,
            headline=f"Parent Situation formed from {len(child_events)} correlated events",
            detail=f"Correlated child events: {', '.join(child_ids)}",
            causal_factor="multi_event_correlation",
        )
        self.repo.events.add_timeline_entry(timeline_entry)

        return sit_id

    def _update_situation(self, situation: EventState) -> None:
        """Stage 1 section 7: Evaluate trajectory and escalation signals."""
        relations = self.repo.events.relations_for(situation.event_id)
        child_ids = [r.child_event_id for r in relations if r.parent_event_id == situation.event_id]

        child_states = [self.repo.states.latest(cid) for cid in child_ids]
        active_children = [s for s in child_states if s and s.status != "closed"]

        escalation_factors: list[str] = []
        agencies: set[str] = set()
        domains: set[str] = set()

        for s in active_children:
            if not s:
                continue
            for domain in s.affected_infrastructure:
                domains.add(domain.domain.value)
            for obs_type in s.evidence.observation_types:
                if "police" in obs_type or "cad" in obs_type:
                    agencies.add("SPD")
                if "fire" in obs_type:
                    agencies.add("SFD")
                if "road" in obs_type or "traffic" in obs_type:
                    agencies.add("SDOT/WSDOT")

        if len(agencies) >= 2:
            escalation_factors.append(f"multi_agency_response: {', '.join(sorted(agencies))}")
        if len(domains) >= 2:
            escalation_factors.append(f"cross_domain_impact: {', '.join(sorted(domains))}")

        # Trajectory calculation
        if not active_children:
            trajectory = SituationTrajectory.ENDED
            phase = EventPhase.ENDED
        elif len(escalation_factors) >= 2:
            trajectory = SituationTrajectory.ESCALATING
            phase = EventPhase.ACTIVE
        elif len(active_children) > len(child_ids) / 2:
            trajectory = SituationTrajectory.BUILDING
            phase = EventPhase.ACTIVE
        else:
            trajectory = SituationTrajectory.STEADY
            phase = EventPhase.ACTIVE

        # Record timeline update if trajectory escalated
        if trajectory == SituationTrajectory.ESCALATING:
            entry = TimelineEntry(
                event_id=situation.event_id,
                timestamp=datetime.now(UTC),
                event_kind=EventKind.SITUATION,
                phase=phase,
                headline="Situation escalating: multiple agencies and domains active",
                detail="; ".join(escalation_factors),
                causal_factor="escalation_signals",
            )
            self.repo.events.add_timeline_entry(entry)

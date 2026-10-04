"""Point-in-time training dataset builder (brief section 46).

Section 46 requirement:
  "Historical training dataset must be produced from the same event ledger:
   OBSERVATION LEDGER -> historical state reconstruction -> point-in-time features
   -> known future outcomes -> training examples.
   This prevents future information leaking into historical feature vectors.
   Point-in-time correctness is essential."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..domain.enums import InfrastructureDomain, ObservationType
from ..domain.schemas import EventState, Observation
from ..storage.repository import PlatformRepository

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrainingExample:
    """One leak-free training example reconstructed at a historical moment T."""

    event_id: str
    as_of: datetime
    state_version: int
    features: dict[str, Any]
    target_horizons_min: tuple[int, ...]
    outcomes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "as_of": self.as_of.isoformat(),
            "state_version": self.state_version,
            "features": self.features,
            "outcomes": self.outcomes,
        }


class PointInTimeDatasetBuilder:
    """Builds clean, leak-free training sets from the observation ledger."""

    def __init__(self, repository: PlatformRepository) -> None:
        self.repo = repository

    def build_examples_for_event(
        self,
        event_id: str,
        horizons_minutes: tuple[int, ...] = (5, 15, 30, 60),
        sample_step_minutes: int = 15,
    ) -> list[TrainingExample]:
        """Reconstruct state history and generate training examples without future leakage."""
        all_obs = self.repo.observations.list_for_event(event_id)
        if not all_obs:
            return []

        # Sort strictly by observed_at
        all_obs = sorted(all_obs, key=lambda o: o.observed_at)
        start_time = all_obs[0].observed_at
        end_time = all_obs[-1].observed_at

        # If the event lasted less than 5 minutes, sample at least the start
        duration = (end_time - start_time).total_seconds() / 60.0
        sample_times: list[datetime] = []
        cur = start_time
        while cur <= end_time:
            sample_times.append(cur)
            cur += timedelta(minutes=sample_step_minutes)

        examples: list[TrainingExample] = []

        for t in sample_times:
            # 1. Strictly point-in-time observations known by t:
            known_obs = [o for o in all_obs if o.observed_at <= t]
            if not known_obs:
                continue

            # 2. State at t:
            state = self.repo.states.state_as_of(event_id, t)
            state_version = state.state_version if state else 1

            # 3. Features known at t:
            features = self._extract_features_at(known_obs, state, t)

            # 4. Target outcomes known strictly in the future (t -> t + horizon):
            outcomes = self._extract_outcomes(all_obs, t, horizons_minutes)

            examples.append(
                TrainingExample(
                    event_id=event_id,
                    as_of=t,
                    state_version=state_version,
                    features=features,
                    target_horizons_min=horizons_minutes,
                    outcomes=outcomes,
                )
            )

        return examples

    def build_dataset(
        self,
        event_ids: Sequence[str] | None = None,
        horizons_minutes: tuple[int, ...] = (5, 15, 30, 60),
    ) -> list[TrainingExample]:
        """Build dataset across all or specified events."""
        if event_ids is None:
            all_events = self.repo.events.all_events()
            event_ids = [e["event_id"] for e in all_events]

        dataset: list[TrainingExample] = []
        for eid in event_ids:
            try:
                examples = self.build_examples_for_event(eid, horizons_minutes)
                dataset.extend(examples)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed building training examples for %s: %r", eid, exc)

        return dataset

    def _extract_features_at(
        self, known_obs: Sequence[Observation], state: EventState | None, t: datetime
    ) -> dict[str, Any]:
        """Compute feature vector strictly from information available at or before t."""
        obs_types = {o.observation_type.value for o in known_obs}
        dur_hours = (t - known_obs[0].observed_at).total_seconds() / 3600.0 if known_obs else 0.0

        moving = 0.0
        crowd = 0.0
        if state and state.movement:
            moving = 1.0 if state.movement.moving else 0.0
        if state and state.derived:
            crowd = float(state.derived.get("estimated_crowd") or 0.0)

        arterial_count = sum(
            1 for o in known_obs
            if o.observation_type == ObservationType.ROAD_CLOSURE
            and o.structured_payload.get("arterial")
        )
        road_closure_count = sum(1 for o in known_obs if o.observation_type == ObservationType.ROAD_CLOSURE)
        transit_alert_count = sum(1 for o in known_obs if o.observation_type == ObservationType.TRANSIT_SERVICE_ALERT)

        local_hour = t.hour
        is_rush = (7 <= local_hour < 10) or (15 <= local_hour < 19)

        return {
            "observation_count": len(known_obs),
            "observation_types": sorted(obs_types),
            "duration_hours": round(dur_hours, 2),
            "moving": moving,
            "crowd_estimate": crowd,
            "arterial_count": arterial_count,
            "road_closure_count": road_closure_count,
            "transit_alert_count": transit_alert_count,
            "is_rush_hour": is_rush,
            "source_count": len({o.source_id for o in known_obs}),
        }

    def _extract_outcomes(
        self,
        all_obs: Sequence[Observation],
        t: datetime,
        horizons_minutes: tuple[int, ...],
    ) -> dict[str, Any]:
        """Compute ground truth labels occurring in future windows (t -> t + H)."""
        outcomes: dict[str, Any] = {}

        for h in horizons_minutes:
            window_end = t + timedelta(minutes=h)
            future_obs = [o for o in all_obs if t < o.observed_at <= window_end]

            road_closure_occurred = any(o.observation_type == ObservationType.ROAD_CLOSURE for o in future_obs)
            transit_disruption_occurred = any(
                o.observation_type in {ObservationType.TRANSIT_SERVICE_ALERT, ObservationType.TRANSIT_DELAY}
                for o in future_obs
            )
            police_escalation = any(
                o.observation_type == ObservationType.POLICE_RESPONSE
                and "arrest" in str(o.structured_payload).lower()
                for o in future_obs
            )

            outcomes[f"road_disruption_by_{h}m"] = int(road_closure_occurred)
            outcomes[f"transit_disruption_by_{h}m"] = int(transit_disruption_occurred)
            outcomes[f"police_escalation_by_{h}m"] = int(police_escalation)

        return outcomes


__all__ = ["PointInTimeDatasetBuilder", "TrainingExample"]

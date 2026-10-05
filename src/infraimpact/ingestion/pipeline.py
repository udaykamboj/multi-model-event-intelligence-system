"""Stage 1 Ingestion pipeline (Stage 1 §2, §4, §5).

Enforces:
1. Source policies, fingerprinting, and last_confirmed tracking.
   Repeated polling adds ZERO observations. A poll is just a heartbeat.
2. Disappearance detection: a condition missing for N consecutive successful polls
   emits a source_record_ended observation, closing the condition with a measured duration.
3. Significance gate: cheap, deterministic classification into:
   EVENT_CANDIDATE, STATE_ONLY, CONTEXT, NOISE.
4. Time model: four timestamps (event_time, published_at, observed_at, ingested_at).
   Never use observed_at as event_time.
5. Ingest & future-dated guards: future events route to scheduled phase; stale articles
   create historical events, never active events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from ..bus.event_bus import EventBus, Topic
from ..domain.enums import EventPhase, SignificanceClass, SourceSemantics
from ..domain.ids import utcnow
from ..domain.schemas import Observation, SourceHealth
from ..sources.adapter import RawRecord
from ..storage.raw_store import RawStore
from ..storage.repository import PlatformRepository
from .policy import (
    compute_record_fingerprint,
    create_ended_observation,
    get_source_policy,
)
from .significance import classify_significance

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    fetched: int = 0
    persisted: int = 0
    duplicates: int = 0
    failed: int = 0
    ended: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: IngestResult) -> IngestResult:
        return IngestResult(
            fetched=self.fetched + other.fetched,
            persisted=self.persisted + other.persisted,
            duplicates=self.duplicates + other.duplicates,
            failed=self.failed + other.failed,
            ended=self.ended + other.ended,
            errors=[*self.errors, *other.errors],
        )

    def summary(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "persisted": self.persisted,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "ended": self.ended,
        }


class IngestionPipeline:
    def __init__(
        self,
        repository: PlatformRepository,
        raw_store: RawStore,
        bus: EventBus,
        region_id: str,
    ) -> None:
        self.repo = repository
        self.raw_store = raw_store
        self.bus = bus
        self.region_id = region_id

    async def ingest_records(
        self, source_id: str, records: Sequence[RawRecord], normalizer
    ) -> IngestResult:
        result = IngestResult()
        policy = get_source_policy(source_id)
        now_dt = datetime.now(UTC)

        normalized: list[Observation] = []
        present_record_ids: set[str] = set()

        with self.repo.transaction():
            for record in records:
                result.fetched += 1
                rec_id = str(record.source_record_id or "")
                if rec_id:
                    present_record_ids.add(rec_id)

                # 1. Fingerprint & Source Record Confirmation (Stage 1 §2)
                # Compute fingerprint of normalized meaningful fields, excluding fetch time/ads
                fingerprint = compute_record_fingerprint(source_id, record.payload)
                changed = True
                version = 1
                if hasattr(self.repo, "source_records") and rec_id:
                    changed, version = self.repo.source_records.confirm_present(
                        source_id, rec_id, fingerprint
                    )

                if not changed:
                    # Same fingerprint: record confirmed alive on this poll.
                    # Zero observation emitted, zero event work.
                    result.duplicates += 1
                    continue

                # 2. Normalize newly seen or revised record
                try:
                    observation = normalizer(record)
                except Exception as exc:  # noqa: BLE001
                    result.failed += 1
                    result.errors.append(f"{source_id}: normalize failed: {exc!r}")
                    log.warning("normalize failed for %s/%s: %r", source_id, rec_id, exc)
                    continue

                if observation is None:
                    result.failed += 1
                    result.errors.append(f"{source_id}: normalizer returned None")
                    continue

                # 3. Significance Gate (Stage 1 §4)
                sig_class = classify_significance(observation)

                # 4. Time Model & Guards (Stage 1 §5)
                # Ensure published_at is set if extractable, else None
                published_at = observation.published_at
                if published_at is None and "published_at" in record.payload:
                    with contextlib_suppress():
                        published_at = datetime.fromisoformat(record.payload["published_at"])

                event_time = observation.event_time
                event_time_conf = observation.event_time_confidence

                # Future-dated guard: event_time > now + 1 hour routes to context/scheduled
                if event_time and event_time > (now_dt + timedelta(hours=1)):
                    if sig_class == SignificanceClass.EVENT_CANDIDATE:
                        sig_class = SignificanceClass.CONTEXT

                # Stale news guard: article older than 6h at first sight cannot create active event
                if (
                    published_at
                    and (now_dt - published_at) > timedelta(hours=6)
                    and observation.observation_type.value == "news_article"
                ):
                    event_time_conf = "historical"

                stamped_obs = observation.model_copy(
                    update={
                        "version": version,
                        "significance_class": sig_class,
                        "published_at": published_at,
                        "event_time_confidence": event_time_conf,
                    }
                )

                written = self._persist(stamped_obs, record)
                if written:
                    result.persisted += 1
                    normalized.append(stamped_obs)
                else:
                    result.duplicates += 1

            # 5. Disappearance Detection (Stage 1 §2 & §16)
            # Only trigger disappearance if poll was non-empty and successful
            if records and hasattr(self.repo, "source_records"):
                missing_records = [
                    r.source_record_id
                    for r in self.repo.source_records.list_active(source_id)
                    if r.source_record_id not in present_record_ids
                ]
                if missing_records:
                    ended_records = self.repo.source_records.mark_missed(
                        source_id, missing_records, threshold=policy.disappearance_threshold_polls
                    )
                    # For state and event feeds (e.g. road closures), emit ended observations
                    if policy.semantics in {SourceSemantics.STATE_FEED, SourceSemantics.EVENT_FEED}:
                        for ended_rec in ended_records:
                            last_obs_list = self.repo.observations.recent(limit=50)
                            matching_last = next(
                                (o for o in last_obs_list if o.source_record_id == ended_rec.source_record_id),
                                None,
                            )
                            ended_obs = create_ended_observation(
                                source_id=source_id,
                                source_record_id=ended_rec.source_record_id,
                                last_observation=matching_last,
                                ended_at=ended_rec.ended_at or now_dt,
                            )
                            if self.repo.observations.append(ended_obs):
                                result.ended += 1
                                result.persisted += 1
                                normalized.append(ended_obs)

        if normalized:
            await self._publish(normalized)
        return result

    def _persist(self, observation: Observation, record: RawRecord) -> bool:
        # 1. raw payload first (section 9)
        try:
            uri = self.raw_store.put(
                observation.source_id,
                observation.observation_id,
                record.payload,
                observation.ingested_at,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("raw store write failed for %s: %r", observation.observation_id, exc)
            uri = None

        stamped = observation.model_copy(update={"raw_payload_uri": uri})

        # 2. dedupe via unique key (section 7)
        if self.repo.observations.has_dedupe_key(stamped.dedupe_key()):
            return False

        # 3. immutable ledger append (section 2.2)
        return self.repo.observations.append(stamped)

    async def _publish(self, observations: Sequence[Observation]) -> None:
        """Announce exactly the observations that reached the ledger."""
        for observation in observations:
            await self.bus.publish(
                Topic.NORMALIZED_OBSERVATIONS,
                observation.model_dump(mode="json"),
                key=observation.source_id,
            )

    def record_health(self, healths: Sequence[SourceHealth]) -> None:
        for health in healths:
            self.repo.source_health.put(health)

    def prune_observations(self, older_than: datetime) -> int:
        _ = older_than
        return 0


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return True


__all__ = ["IngestResult", "IngestionPipeline", "UTC", "utcnow"]
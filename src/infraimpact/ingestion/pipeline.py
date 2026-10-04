"""Ingestion pipeline (brief sections 6, 7, 9).

Order matters and is fixed:

    raw record -> persist raw payload -> normalize -> dedupe -> ledger -> bus

The raw payload is written *before* normalisation so a parser can later be
improved and every historical record reprocessed. Dedupe is at-least-once
tolerant: consumers are idempotent rather than delivery being exactly-once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Sequence

from ..bus.event_bus import EventBus, Topic
from ..domain.ids import utcnow
from ..domain.schemas import Observation, SourceHealth
from ..sources.adapter import RawRecord
from ..storage.raw_store import RawStore
from ..storage.repository import PlatformRepository

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    fetched: int = 0
    persisted: int = 0
    duplicates: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: IngestResult) -> IngestResult:
        return IngestResult(
            fetched=self.fetched + other.fetched,
            persisted=self.persisted + other.persisted,
            duplicates=self.duplicates + other.duplicates,
            failed=self.failed + other.failed,
            errors=[*self.errors, *other.errors],
        )

    def summary(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "persisted": self.persisted,
            "duplicates": self.duplicates,
            "failed": self.failed,
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
        # Normalize once, keep the result. Previously this loop called
        # ``normalizer(record)``, threw the result away, and then
        # :meth:`_publish` called it again for every record - doubling the
        # cost of the most expensive stage, and doing it with no guarantee the
        # two calls agreed. Any normalizer with a side effect, any nondeterminism,
        # and any future per-call counter would have made the published
        # observation differ from the stored one.
        normalized: list[Observation] = []

        # One transaction for the batch. Inside it the dedupe probe and the
        # ledger append commit once at the end rather than twice per record;
        # see :meth:`PlatformRepository.transaction`.
        with self.repo.transaction():
            for record in records:
                result.fetched += 1
                try:
                    observation = normalizer(record)
                except Exception as exc:  # noqa: BLE001 - one bad record must not stop the feed
                    result.failed += 1
                    result.errors.append(f"{source_id}: normalize failed: {exc!r}")
                    log.warning(
                        "normalize failed for %s/%s: %r",
                        source_id,
                        record.source_record_id,
                        exc,
                    )
                    continue

                if observation is None:
                    result.failed += 1
                    result.errors.append(f"{source_id}: normalizer returned None")
                    continue

                written = self._persist(observation, record)
                if written:
                    result.persisted += 1
                    normalized.append(observation)
                else:
                    result.duplicates += 1

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

        # 2. dedupe via the unique key (section 7)
        if self.repo.observations.has_dedupe_key(stamped.dedupe_key()):
            return False

        # 3. immutable ledger append (section 2.2)
        return self.repo.observations.append(stamped)

    async def _publish(self, observations: Sequence[Observation]) -> None:
        """Announce exactly the observations that reached the ledger.

        Takes the stored objects rather than records plus a normalizer, so what
        is published is what was written. Publishing a re-normalized copy meant
        the subscriber - which resolves events and extracts claims - was working
        from a second, unverified rendering of each record.
        """

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
        """Retention helper. State versions and analyses are never pruned."""
        _ = older_than
        return 0


__all__ = ["IngestResult", "IngestionPipeline", "UTC", "utcnow"]
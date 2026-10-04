"""Source adapter contract (brief section 4).

Every connector implements the same interface, so sources are added without
touching event logic. The analysis engine has no idea which adapters run.

    poll()       fetch whatever is new since the checkpoint
    subscribe()  optional streaming counterpart
    normalize()  raw record -> Observation envelope
    health()     freshness / reliability telemetry
    checkpoint() resume position
    backfill()   historical load (separate from live detection)
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Iterable

from ..domain.enums import Authority, HealthState, ObservationType, SourceType
from ..domain.ids import content_hash, deterministic_id, new_id, utcnow
from ..domain.schemas import Observation, ObservationQuality, Provenance, SourceHealth

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RawRecord:
    """One record as received from a source, before normalisation."""

    source_record_id: str
    payload: Any
    event_time: datetime | None = None
    observed_at: datetime | None = None
    source_url: str | None = None


@dataclass
class SourceAdapter(abc.ABC):
    """Base class providing health accounting around the abstract hooks.

    Subclasses declare their provenance and cadence as plain class attributes,
    and those are ``ClassVar`` for a reason: as ordinary dataclass fields the
    generated ``__init__`` reassigns every instance to the *base* defaults, so a
    connector declaring ``authority = Authority.COMMUNITY`` or
    ``source_type = SourceType.ESTABLISHED_NEWS`` would silently emit
    ``official`` / ``official_machine_readable`` observations instead, and the
    per-outlet authority a news feed computes would be discarded the moment the
    object was constructed. Only the per-instance health counters are fields.
    """

    source_id: ClassVar[str] = ""
    source_type: ClassVar[SourceType] = SourceType.OFFICIAL_MACHINE_READABLE
    authority: ClassVar[Authority] = Authority.OFFICIAL
    default_observation_type: ClassVar[ObservationType] = ObservationType.NEWS_ARTICLE
    reliability: ClassVar[float] = 0.9
    expected_interval_s: ClassVar[float] = 60.0
    stale_after_s: ClassVar[float] = 600.0
    usage: ClassVar[str] = "realtime"
    requires_key: ClassVar[str | None] = None
    connector_version: ClassVar[str] = "0.1.0"

    _records_received: int = field(default=0, init=False)
    _last_success: datetime | None = field(default=None, init=False)
    _last_attempt: datetime | None = field(default=None, init=False)
    _errors: int = field(default=0, init=False)
    _attempts: int = field(default=0, init=False)
    _last_latency_ms: float | None = field(default=None, init=False)
    _message: str | None = field(default=None, init=False)

    # -- required hooks ---------------------------------------------------

    @abc.abstractmethod
    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        """Return records added since ``checkpoint``."""

    def subscribe(self) -> Iterable[RawRecord] | None:
        """Optional push-based feed. Default: no streaming support."""
        return None

    @abc.abstractmethod
    def normalize(self, record: RawRecord) -> Observation | None:
        """Raw record -> universal observation envelope."""

    def backfill(self, since: datetime) -> Iterable[Observation]:
        """Historical load. Distinct from live detection on purpose."""
        return []

    # -- optional hooks ---------------------------------------------------

    def available(self) -> bool:
        """Is this adapter runnable right now (credentials present, etc.)?"""
        if self.requires_key is None:
            return True
        from ..config import get_settings

        return bool(getattr(get_settings(), self.requires_key, ""))

    def checkpoint(self) -> str | None:
        return None

    def health(self) -> SourceHealth:
        from ..config import get_settings

        now = utcnow()
        freshness = (
            (now - self._last_success).total_seconds() if self._last_success else None
        )
        if self._last_success is None:
            state = HealthState.UNKNOWN
        elif self._attempts and self._errors / self._attempts > 0.5:
            state = HealthState.DEGRADED
        elif freshness is not None and freshness > self.stale_after_s:
            state = HealthState.OFFLINE
        elif freshness is not None and freshness > self.expected_interval_s * 2:
            state = HealthState.DELAYED
        else:
            state = HealthState.HEALTHY

        settings = get_settings()
        _ = settings
        return SourceHealth(
            source_id=self.source_id,
            state=state,
            last_success=self._last_success,
            last_attempt=self._last_attempt,
            latency_ms=self._last_latency_ms,
            error_rate=(self._errors / self._attempts) if self._attempts else 0.0,
            records_received=self._records_received,
            freshness_seconds=freshness,
            expected_interval_s=self.expected_interval_s,
            stale_after_s=self.stale_after_s,
            usage=self.usage,  # type: ignore[arg-type]
            message=self._message,
        )

    # -- helpers for subclasses ------------------------------------------

    def build_observation(
        self,
        record: RawRecord,
        *,
        observation_type: ObservationType | None = None,
        geometry: dict[str, Any] | None = None,
        headline: str = "",
        structured_payload: dict[str, Any] | None = None,
        location_precision_m: float | None = None,
        authority: Authority | None = None,
        source_type: SourceType | None = None,
        source_record_id: str | None = None,
        event_time: datetime | None = None,
        observed_at: datetime | None = None,
        reliability: float | None = None,
    ) -> Observation:
        """Construct an envelope with consistent provenance and timestamps.

        ``authority`` and ``source_type`` default to the adapter's, but an
        adapter can override them per record - a news feed carries different
        trust depending on which outlet the record came from.
        """
        now = utcnow()
        payload = structured_payload if structured_payload is not None else _safe(record.payload)
        raw_hash = content_hash(record.payload)
        rid = source_record_id or record.source_record_id
        effective_source_type = source_type or self.source_type
        return Observation(
            observation_id=deterministic_id(
                "obs", self.source_id, rid, (observed_at or record.observed_at or now).isoformat(), raw_hash
            ),
            source_id=self.source_id,
            source_record_id=str(rid),
            event_time=event_time or record.event_time or now,
            observed_at=observed_at or record.observed_at or now,
            ingested_at=now,
            source_type=effective_source_type,
            observation_type=observation_type or self.default_observation_type,
            geometry=geometry,
            location_precision_m=location_precision_m,
            headline=headline,
            structured_payload=payload,
            source_url=record.source_url,
            provenance=Provenance(
                authority=authority or self.authority,
                retrieval_method="api"
                if effective_source_type != SourceType.USER_SUPPLIED
                else "user",
                content_hash=raw_hash,
                connector_version=self.connector_version,
                upstream_id=str(rid) if rid else None,
            ),
            quality=ObservationQuality(
                source_reliability=reliability if reliability is not None else self.reliability,
                temporal_precision=0.95,
                spatial_precision=0.9 if geometry else 0.5,
                extraction_confidence=1.0,
            ),
        )

    def record_success(self, count: int, latency_ms: float) -> None:
        self._records_received += count
        self._last_success = utcnow()
        self._last_latency_ms = latency_ms
        self._message = None

    def record_failure(self, message: str) -> None:
        self._errors += 1
        self._message = message[:500]
        log.warning("source %s failed: %s", self.source_id, message)

    @property
    def enabled_by_default(self) -> bool:
        return True


class _TimedPollMixin:
    def timed_poll(self, checkpoint: str | None) -> list[RawRecord]:
        self._last_attempt = utcnow()
        self._attempts += 1
        started = time.perf_counter()
        try:
            records = list(self.poll(checkpoint))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.record_failure(repr(exc))  # type: ignore[attr-defined]
            return []
        self.record_success(len(records), (time.perf_counter() - started) * 1000.0)  # type: ignore[attr-defined]
        return records


def _safe(payload: Any) -> Any:
    if isinstance(payload, (str, int, float, bool, list, dict)) or payload is None:
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    if isinstance(payload, (bytes, bytearray)):
        return {"_bytes": len(payload)}
    return {"_repr": repr(payload)[:500]}


def unavailable_reason(adapter: SourceAdapter) -> str | None:
    if adapter.requires_key and not adapter.available():
        return f"missing credential env {adapter.requires_key.upper()}"
    return None


def make_id() -> str:
    return new_id("tmp")


__all__ = [
    "RawRecord",
    "SourceAdapter",
    "unavailable_reason",
    "Authority",
    "UTC",
]
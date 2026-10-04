"""Event bus (brief section 8).

At-least-once delivery plus idempotent consumers (section 7) - deliberately not
end-to-end exactly-once.

``InMemoryEventBus`` is the local default. ``KafkaEventBus`` requires
``aiokafka`` and is intentionally lazy so the package imports without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)


class Topic(StrEnum):
    """Section 8 core topics."""

    RAW_OBSERVATIONS = "raw.observations"
    NORMALIZED_OBSERVATIONS = "normalized.observations"
    CLAIMS_CREATED = "claims.created"
    EVENTS_CANDIDATES = "events.candidates"
    EVENTS_UPDATED = "events.updated"
    EVENTS_MERGED = "events.merged"
    EVENTS_CLOSED = "events.closed"
    STATE_UPDATED = "state.updated"
    ANALYSIS_REQUESTED = "analysis.requested"
    ANALYSIS_COMPLETED = "analysis.completed"
    IMPACT_UPDATED = "impact.updated"
    USER_IMPACT_UPDATED = "user_impact.updated"
    ALERTS_CANDIDATES = "alerts.candidates"
    ALERTS_SENT = "alerts.sent"
    MODELS_PREDICTIONS = "models.predictions"
    DEAD_LETTER = "dead_letter"


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    topic: str
    key: str | None = None
    payload: dict[str, Any]
    produced_at: datetime
    attempt: int = 0


class EventBus(ABC):
    @abstractmethod
    async def publish(self, topic: Topic | str, payload: dict[str, Any], key: str | None = None) -> None: ...

    @abstractmethod
    async def subscribe(
        self, topic: Topic | str, handler: Callable[[Envelope], Awaitable[None]]
    ) -> None: ...

    @abstractmethod
    async def replay_history(self, topic: Topic | str) -> list[Envelope]: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class InMemoryEventBus(EventBus):
    """Async in-process bus with bounded per-topic history for replay/debugging."""

    def __init__(self, history: int = 2000, stream_buffer: int = 500, heartbeat_interval_s: float = 15.0) -> None:
        self._subscribers: dict[str, list[Callable[[Envelope], Awaitable[None]]]] = defaultdict(list)
        self._history: dict[str, deque[Envelope]] = defaultdict(lambda: deque(maxlen=history))
        self._streams: list[asyncio.Queue[Envelope]] = []
        self._stream_buffer = stream_buffer
        self._heartbeat_interval_s = heartbeat_interval_s
        self._closing = False

    async def start(self) -> None:
        return None

    async def publish(self, topic: Topic | str, payload: dict[str, Any], key: str | None = None) -> None:
        env = Envelope(
            topic=str(topic),
            key=key,
            payload=payload,
            produced_at=datetime.now(UTC),
        )
        self._history[str(topic)].append(env)
        for queue in list(self._streams):
            try:
                queue.put_nowait(env)
            except asyncio.QueueFull:  # pragma: no cover - slow reader
                pass
        for handler in list(self._subscribers.get(str(topic), [])):
            try:
                await handler(env)
            except Exception:  # noqa: BLE001 - consumer isolation is deliberate
                log.exception("handler failed for topic=%s", topic)
                self._history[str(Topic.DEAD_LETTER)].append(
                    Envelope(
                        topic=str(Topic.DEAD_LETTER),
                        key=key,
                        payload={"failed_topic": str(topic), "error": repr(sys.exc_info()[1])},
                        produced_at=datetime.now(UTC),
                    )
                )

    async def subscribe(
        self, topic: Topic | str, handler: Callable[[Envelope], Awaitable[None]]
    ) -> None:
        self._subscribers[str(topic)].append(handler)

    async def replay_history(self, topic: Topic | str) -> list[Envelope]:
        return list(self._history.get(str(topic), ()))

    async def stream(self) -> AsyncIterator[Envelope]:
        """Fan-out helper backing the SSE endpoint (section 50)."""
        queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize=self._stream_buffer)
        self._streams.append(queue)
        try:
            while not self._closing:
                try:
                    yield await asyncio.wait_for(
                        queue.get(), timeout=self._heartbeat_interval_s
                    )
                except asyncio.TimeoutError:
                    # Keepalive: an idle stream that goes silent gets dropped by
                    # proxies, so emit a heartbeat even when nothing happened.
                    yield Envelope(topic="heartbeat", payload={}, produced_at=datetime.now(UTC))
        finally:
            if queue in self._streams:
                self._streams.remove(queue)

    async def close(self) -> None:
        self._closing = True
        self._streams.clear()


class KafkaEventBus(EventBus):
    """Kafka/Redpanda-backed bus. Requires the optional ``kafka`` extra."""

    def __init__(self, bootstrap: str, client_id: str = "infraimpact") -> None:
        self.bootstrap = bootstrap
        self.client_id = client_id
        self._producer: Any = None
        self._consumer_tasks: list[asyncio.Task[None]] = []

    async def _get_producer(self) -> Any:
        if self._producer is None:
            try:
                from aiokafka import AIOKafkaProducer
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("install infraimpact[kafka] to use the Kafka bus") from exc
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self.bootstrap, client_id=self.client_id, acks="all"
            )
            await self._producer.start()
        return self._producer

    async def publish(self, topic: Topic | str, payload: dict[str, Any], key: str | None = None) -> None:
        producer = await self._get_producer()
        await producer.send_and_wait(
            str(topic),
            value=json.dumps(payload, default=str).encode(),
            key=(key or "").encode() or None,
        )

    async def subscribe(
        self, topic: Topic | str, handler: Callable[[Envelope], Awaitable[None]]
    ) -> None:
        try:
            from aiokafka import AIOKafkaConsumer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("install infraimpact[kafka] to use the Kafka bus") from exc

        async def _run() -> None:
            consumer = AIOKafkaConsumer(
                str(topic),
                bootstrap_servers=self.bootstrap,
                group_id=f"{self.client_id}-{topic}",
                auto_offset_reset="earliest",
                enable_auto_commit=False,
            )
            await consumer.start()
            try:
                async for msg in consumer:
                    env = Envelope(
                        topic=str(topic),
                        key=msg.key.decode() if msg.key else None,
                        payload=json.loads(msg.value.decode()),
                        produced_at=datetime.fromtimestamp(msg.timestamp / 1000, tz=UTC),
                        attempt=0,
                    )
                    await handler(env)
                    await consumer.commit()
            finally:
                await consumer.stop()

        self._consumer_tasks.append(asyncio.create_task(_run(), name=f"kafka-consumer-{topic}"))

    async def replay_history(self, topic: Topic | str) -> list[Envelope]:
        raise NotImplementedError("replay_history is served by the ledger in production")

    async def close(self) -> None:
        for task in self._consumer_tasks:
            task.cancel()
        self._consumer_tasks.clear()
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


def build_bus(backend: str | None = None, bootstrap: str | None = None) -> EventBus:
    from ..config import get_settings

    settings = get_settings()
    choice = (backend or settings.bus_backend).lower()
    if choice in {"memory", "inmemory", "local"}:
        return InMemoryEventBus()
    if choice in {"kafka", "redpanda"}:
        return KafkaEventBus(bootstrap or settings.kafka_bootstrap)
    raise ValueError(f"unknown bus backend '{choice}'")
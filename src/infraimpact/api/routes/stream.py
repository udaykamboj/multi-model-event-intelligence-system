"""Real-time channel (brief section 50 ``/v1/stream``).

SSE rather than WebSocket: the traffic is one-directional server -> client, and
SSE survives proxies without a handshake. The stream is a *tap* - it never
drives the loop, it only reports what the bus already published, so a slow or
disconnected client cannot back up the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ...bus.event_bus import InMemoryEventBus
from ..deps import BusDep

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["stream"])

#: Client-facing topics. ``dead_letter`` and raw internals stay off the wire.
STREAM_TOPICS = (
    "events.candidates",
    "events.updated",
    "events.merged",
    "events.closed",
    "state.updated",
    "analysis.completed",
    "impact.updated",
    "user_impact.updated",
    "alerts.candidates",
    "alerts.sent",
    "models.predictions",
)


@router.get("/stream", summary="Server-sent event stream of platform activity")
async def stream(
    request: Request,
    bus: BusDep,
    topics: str | None = Query(
        default=None,
        description="Comma-separated subset of topics. Omit for all client topics.",
    ),
    user_id: str | None = Query(
        default=None,
        description="When set, only events touching this user are sent.",
    ),
) -> StreamingResponse:
    if not isinstance(bus, InMemoryEventBus):
        # KafkaEventBus has no in-process fan-out; the client should consume the
        # broker directly rather than have us proxy it.
        return StreamingResponse(
            _unavailable(),
            media_type="text/event-stream",
            status_code=503,
        )

    wanted = _wanted_topics(topics)

    async def publisher() -> AsyncIterator[bytes]:
        try:
            async for envelope in bus.stream():
                if await request.is_disconnected():
                    break
                # Heartbeats always pass: they are keepalives, and filtering
                # them out would leave an idle stream silent until the proxy or
                # the client gives up.
                if envelope.topic != "heartbeat" and wanted and envelope.topic not in wanted:
                    continue
                if user_id and not _touches_user(envelope.payload, user_id):
                    continue
                yield _format(envelope.topic, envelope.payload, envelope.produced_at)
        except asyncio.CancelledError:  # pragma: no cover - client disconnect
            raise
        except Exception:  # noqa: BLE001 - never leak a stack trace into the stream
            log.exception("stream failed")

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _unavailable() -> AsyncIterator[bytes]:
    yield _format(
        "error",
        {"detail": "streaming requires the in-memory bus; consume Kafka directly"},
        None,
    )


def _wanted_topics(raw: str | None) -> set[str]:
    if not raw:
        return set()
    requested = {t.strip() for t in raw.split(",") if t.strip()}
    # Only allow known client topics; an unknown name is ignored rather than
    # echoed back, so the endpoint cannot be used to probe internal topics.
    return requested & set(STREAM_TOPICS)


def _touches_user(payload: dict, user_id: str) -> bool:
    if payload.get("user_id") == user_id:
        return True
    for item in payload.get("presentation") or ():
        if isinstance(item, dict) and item.get("user_id") == user_id:
            return True
    return False


def _format(topic: str, payload: dict, produced_at) -> bytes:
    """One SSE frame. ``event:`` carries the topic so clients can subscribe."""
    body = json.dumps(payload, default=str)
    stamp = produced_at.isoformat() if produced_at is not None else ""
    return f"event: {topic}\nid: {stamp}\ndata: {body}\n\n".encode()


__all__ = ["STREAM_TOPICS", "router"]
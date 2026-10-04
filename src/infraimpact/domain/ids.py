"""Identifier and time helpers.

Every identifier is deterministic where it can be, so ingestion is idempotent
(brief section 7) and replays reproduce identical IDs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

NAMESPACE = uuid.UUID("6f1d2b7a-1c4e-5a3f-9b8d-0e7c5a4f3b21")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def deterministic_id(prefix: str, *parts: Any) -> str:
    """Stable ID from input parts. Used for dedupe keys and replay fidelity."""
    payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def parse_time(value: str | datetime | None) -> datetime | None:
    """Lenient timestamp parsing for third-party feeds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%Y%m%dT%H%M%S",
        "%Y%m%d%H%M%S",
    ):
        try:
            return ensure_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None


def epoch_seconds(value: datetime) -> float:
    return ensure_utc(value).timestamp()


def minutes_between(a: datetime, b: datetime) -> float:
    return abs((ensure_utc(b) - ensure_utc(a)).total_seconds()) / 60.0


def now_plus(**kwargs: float) -> datetime:
    return utcnow() + timedelta(**kwargs)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
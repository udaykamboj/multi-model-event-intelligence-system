"""Region-level read (brief section 50 ``GET /v1/regions/{region_id}/state``).

Section 4: a region profile is data - which sources, which bounds, which
timezone. This route surfaces it as-is, plus a live count of open events and
source health from section 56.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...config import REGIONS, get_region
from ...domain.ids import parse_time, utcnow
from ..deps import get_repository
from ..schemas import (
    ErrorResponse,
    EventSummary,
    RegionStateResponse,
    SourceHealthView,
)

router = APIRouter(prefix="/v1/regions", tags=["regions"])


@router.get(
    "/{region_id}/state",
    response_model=RegionStateResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_region_state(region_id: str, request: Request) -> RegionStateResponse:
    if region_id not in REGIONS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown region '{region_id}'; known: {sorted(REGIONS)}",
        )
    profile = get_region(region_id)
    repo = get_repository(request)

    open_rows = [r for r in repo.events.all_events() if r.get("status") != "closed"]
    sources = [_source_view(repo, profile, source_id) for source_id in profile.source_specs]

    return RegionStateResponse(
        region_id=profile.region_id,
        display_name=profile.display_name,
        timezone=profile.timezone,
        bounds=profile.bounds,
        center=profile.center,
        active_events=len(repo.events.active_events()),
        open_events=[
            EventSummary(
                event_id=r["event_id"],
                status=r.get("status", "active"),
                region_id=r.get("region_id", profile.region_id),
                observation_count=int(r.get("observation_count") or 0),
                updated_at=_maybe_dt(r.get("updated_at")),
            )
            for r in open_rows[:100]
        ],
        sources=sources,
        observations_total=repo.observations.count(),
        generated_at=utcnow(),
    )


def _source_view(repo: Any, profile: Any, source_id: str) -> SourceHealthView:
    """Merge the declared source spec (section 56) with observed health.

    A source that has never reported still appears, carrying its declared
    profile - an adapter that is configured but idle is different from one that
    is missing, and a region view should be able to tell them apart.
    """
    spec = profile.source_specs.get(source_id)
    health = repo.source_health.get(source_id)

    if health is None:
        return SourceHealthView(
            source_id=source_id,
            state="idle",
            usage=spec.usage if spec else "realtime",
            expected_interval_s=spec.expected_interval_s if spec else None,
            stale_after_s=spec.stale_after_s if spec else None,
            capability_tier=str(spec.capability_tier) if spec else "triggered",
            note=spec.note if spec else "",
        )

    return SourceHealthView(
        source_id=source_id,
        state=str(health.state),
        usage=health.usage,
        expected_interval_s=health.expected_interval_s,
        stale_after_s=health.stale_after_s,
        freshness_seconds=health.freshness_seconds,
        stale=health.freshness_seconds is not None
        and health.stale_after_s is not None
        and health.freshness_seconds > health.stale_after_s,
        last_success=health.last_success,
        last_attempt=health.last_attempt,
        latency_ms=health.latency_ms,
        error_rate=health.error_rate,
        records_received=health.records_received,
        message=health.message,
        capability_tier=str(health.capability_tier),
        note=spec.note if spec else "",
    )


def _maybe_dt(value: Any):
    if not value:
        return None
    if hasattr(value, "isoformat"):
        return value
    return parse_time(str(value))


__all__ = ["router"]
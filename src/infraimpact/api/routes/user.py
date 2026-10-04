"""User-facing impact routes (brief section 50, sections 34-37/54).

Everything here is scoped to an explicit ``user_id``. Section 59: location is
user-supplied and ephemeral, so the API never infers who is asking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ...domain.schemas import UserExposure
from ..deps import RepoDep
from ..schemas import (
    ErrorResponse,
    NotificationListResponse,
    NotificationView,
    RouteImpactResponse,
    RouteImpactView,
    UserImpactView,
    UserImpactsResponse,
)

router = APIRouter(prefix="/v1/user", tags=["user"])


def _view(record: dict[str, Any]) -> UserImpactView:
    """A stored exposure blob -> public view.

    ``record`` is the persisted ``UserImpactState`` envelope: the ``current``
    exposure plus the runtime's priority and presentation decisions.
    """
    current: dict[str, Any] = record.get("current") or {}
    exposure = UserExposure.model_validate(current)
    presentation = record.get("presentation") or []
    return UserImpactView(
        user_id=exposure.user_id,
        event_id=exposure.event_id,
        analysis_run_id=exposure.analysis_run_id,
        exposure_level=str(exposure.exposure_level),
        exposure_score=exposure.exposure_score,
        priority=float(record.get("priority") or 0.0),
        distance_m=exposure.distance_m,
        inside_impact_area=exposure.inside_impact_area,
        confidence=exposure.confidence,
        route_impacts=exposure.route_impacts,
        saved_place_impacts=exposure.saved_place_impacts,
        components=exposure.components,
        presentation=presentation,
        as_of=exposure.as_of,
    )


@router.get(
    "/impacts",
    response_model=UserImpactsResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_user_impacts(
    repo: RepoDep,
    user_id: str = Query(description="Section 34: the user is always explicit."),
    limit: int = Query(default=50, ge=1, le=200),
) -> UserImpactsResponse:
    """What is affecting this user right now, most severe first."""
    user = repo.users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"unknown user '{user_id}'")

    records = repo.user_impacts.for_user(user_id, limit=limit)
    impacts = sorted(
        (_view(r) for r in records),
        key=lambda i: (-i.priority, -i.exposure_score),
    )
    return UserImpactsResponse(user_id=user_id, count=len(impacts), impacts=impacts)


@router.get(
    "/routes/{route_id}/impact",
    response_model=RouteImpactResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_route_impact(
    route_id: str,
    repo: RepoDep,
    user_id: str | None = Query(default=None),
) -> RouteImpactResponse:
    """Impact on a route, across events.

    Routes belong to users, so a ``route_id`` is resolved by looking it up
    across stored users. Pass ``user_id`` to disambiguate.
    """
    owners = _route_owners(repo, route_id, user_id)
    if not owners:
        raise HTTPException(
            status_code=404,
            detail=f"unknown route '{route_id}'" + (f" for user '{user_id}'" if user_id else ""),
        )

    views: list[RouteImpactView] = []
    for uid in owners:
        for record in repo.user_impacts.for_user(uid, limit=200):
            exposure = UserExposure.model_validate(record.get("current") or {})
            as_of = exposure.as_of
            for route in exposure.route_impacts:
                if route.route_id != route_id:
                    continue
                views.append(
                    RouteImpactView(
                        route_id=route.route_id,
                        route_name=route.route_name,
                        user_id=uid,
                        event_id=exposure.event_id,
                        intersects=route.intersects,
                        delay_estimate_min=route.delay_estimate_min,
                        blocked_node_ids=route.blocked_node_ids,
                        alternative_available=route.alternative_available,
                        evidence_ids=route.evidence_ids,
                        as_of=as_of,
                    )
                )

    # Blocked routes first, then most recently computed.
    views.sort(key=lambda v: (not v.intersects, _desc(v.as_of)))
    return RouteImpactResponse(route_id=route_id, count=len(views), impacts=views)


def _desc(value: datetime | None) -> float:
    """Sort key that puts the newest timestamp first and None last."""
    return -value.timestamp() if value is not None else float("inf")


def _route_owners(repo: Any, route_id: str, user_id: str | None) -> list[str]:
    users = [repo.users.get(user_id)] if user_id else repo.users.all()
    return [
        u.user_id
        for u in users
        if u is not None and any(r.route_id == route_id for r in u.route_profiles)
    ]


@router.get(
    "/notifications",
    response_model=NotificationListResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_notifications(
    repo: RepoDep,
    user_id: str = Query(description="Section 52: notifications are per user."),
    limit: int = Query(default=50, ge=1, le=200),
) -> NotificationListResponse:
    """Alerts already emitted for a user.

    Read-only: policy, dedupe and rate limiting already ran in the runtime, so
    nothing here can manufacture an alert that the platform did not decide to
    send (section 52).
    """
    if repo.users.get(user_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown user '{user_id}'")
    rows = [n for n in repo.notifications.recent(limit=limit) if n.user_id == user_id]
    return NotificationListResponse(
        count=len(rows),
        notifications=[
            NotificationView(
                notification_id=n.notification_id,
                user_id=n.user_id,
                event_id=n.event_id,
                reason=str(n.reason),
                urgency=str(n.urgency),
                headline=n.headline,
                body=n.body,
                presentation=n.presentation,
            )
            for n in rows
        ],
    )


__all__ = ["router"]
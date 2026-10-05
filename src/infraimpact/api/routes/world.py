"""World-state read routes (brief sections 12, 32, 51).

``GET /v1/world`` answers the question the whole platform exists to answer:

    what is happening right now?

Every other route is about one event. This is the one that is about the region,
and it is the only endpoint that can be answered without already knowing which
event to ask about - which is the situation a user is actually in.

Three things are load-bearing in the response shape.

**Staleness is visible.** A snapshot reports the observation total it was built
from and when it was generated. A world view that cannot say how current it is
cannot be distinguished from one that is confidently describing a stale world.

**Coverage travels with it.** ``coverage_gaps`` says which sources could not
contribute. A region with no traffic problems and no traffic feed is a fact
about the platform, not about the world, and only one of those two is a reason
to relax.

**Tracked and changed are separate.** ``events_total`` is how much the platform
is watching; ``events_changed_materially`` is what needs attention. Collapsing
them would make a thousand quiet events look like a crisis and a hundred
unchanged ones look like calm.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...domain.ids import utcnow
from ...domain.schemas import WorldSnapshot
from ...world.projection import WorldProjection
from ..deps import RegionDep, RepoDep
from ..schemas import WorldResponse

router = APIRouter(prefix="/v1/world", tags=["world"])


@router.get("", response_model=WorldResponse)
async def get_world(
    repo: RepoDep,
    region: RegionDep,
    refresh: bool = Query(
        default=False,
        description="rebuild from stored state versions instead of returning the stored snapshot",
    ),
    include_closed: bool = Query(
        default=False,
        description="include events that have been closed",
    ),
    change_window_s: float = Query(
        default=3600.0,
        ge=0.0,
        description="how far back a delta counts as a recent change",
    ),
) -> WorldResponse:
    """The cross-event world view.

    Returns the stored snapshot by default. Rebuilding on every request would
    mean a read endpoint doing a full fan-out over events, which is fine at a
    dozen events and ruinous at twelve thousand; ``refresh=true`` exists for
    callers that genuinely need it current, such as immediately after a write.
    """

    projection = WorldProjection(repo, region)
    if refresh:
        snapshot = projection.build(
            now=utcnow(),
            change_window_seconds=change_window_s,
            include_closed=include_closed,
        )
        projection.persist(snapshot)
    else:
        snapshot = projection.latest()
        if snapshot is None or include_closed != (snapshot.events_closed > 0):
            # No snapshot yet, or the caller asked for a view this one cannot
            # satisfy. Rebuilding is better than returning something that
            # silently does not match the request.
            snapshot = projection.build(
                now=utcnow(),
                change_window_seconds=change_window_s,
                include_closed=include_closed,
            )
            projection.persist(snapshot)
    return _world_response(snapshot)


@router.get("/changes", response_model=WorldResponse)
async def get_world_changes(
    repo: RepoDep,
    region: RegionDep,
    limit: int = Query(default=200, ge=1, le=1000),
) -> WorldResponse:
    """The world view, rebuilt around what changed most recently.

    Same response shape on purpose. A caller polling for "what is new" should not
    have to parse a different schema from the one it already handles.
    """

    snapshot = WorldProjection(repo, region).snapshot(now=utcnow())
    return _world_response(snapshot, limit=limit)


def _world_response(snapshot: WorldSnapshot, *, limit: int | None = None) -> WorldResponse:
    events = snapshot.events[:limit] if limit is not None else snapshot.events
    return WorldResponse(
        snapshot_id=snapshot.snapshot_id,
        region_id=snapshot.region_id,
        generated_at=snapshot.generated_at,
        observation_total=snapshot.observation_total,
        events_total=snapshot.events_total,
        events_active=snapshot.events_active,
        events_quiescent=snapshot.events_quiescent,
        events_closed=snapshot.events_closed,
        events_changed_materially=snapshot.events_changed_materially,
        events=list(events),
        domain_activity=snapshot.domain_activity,
        sources_degraded=list(snapshot.sources_degraded),
        coverage_gaps=list(snapshot.coverage_gaps),
        source_health=snapshot.source_health,
    )


__all__ = ["router"]

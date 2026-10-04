"""Event read routes (brief section 50/51).

``GET /v1/events/{event_id}`` returns the section 51 envelope: the event, its
current reconstruction, top impacts, recent changes, forecast, evidence summary
and last-updated time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ...analysis.metrics import EventAnalysisMetrics
from ...domain.ids import parse_time
from ...domain.schemas import AnalysisRun, EventState, EvidenceSummary, StateDelta
from ..deps import RepoDep
from ..schemas import (
    AnalysisMetricsResponse,
    AnalysisResponse,
    AnalysisView,
    CurrentStateView,
    ErrorResponse,
    EvidenceResponse,
    EventDetail,
    EventListResponse,
    EventSummary,
    ForecastResponse,
    RecentChange,
    TimelineEntry,
    TimelineResponse,
)

router = APIRouter(prefix="/v1/events", tags=["events"])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _summary_from_row(row: dict[str, Any]) -> EventSummary:
    return EventSummary(
        event_id=row["event_id"],
        status=row.get("status", "active"),
        region_id=row.get("region_id", ""),
        first_observed=_maybe_dt(row.get("first_observed")),
        updated_at=_maybe_dt(row.get("updated_at")),
        closed_at=_maybe_dt(row.get("closed_at")),
        observation_count=int(row.get("observation_count") or 0),
    )


def _maybe_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return parse_time(str(value))
    except Exception:  # noqa: BLE001 - a malformed row must not 500 the list
        return None


def _current_state_view(state: EventState) -> CurrentStateView:
    return CurrentStateView(
        state_version=state.state_version,
        status=state.status,
        event_type_distribution=dict(state.event_type_distribution),
        geometry=dict(state.geometry) if state.geometry else None,
        geometry_confidence=state.geometry_confidence,
        first_observed=state.first_observed,
        last_observed=state.last_observed,
        reconstructed_at=state.reconstructed_at,
        movement=state.movement.model_dump(mode="json"),
        evidence=state.evidence,
        observation_ids=state.observation_ids,
        claim_ids=state.claim_ids,
    )


def _analysis_view(run: AnalysisRun) -> AnalysisView:
    # Section 51: audit trail yes, model internals no.
    metrics_obj = None
    if run.metrics:
        metrics_obj = (
            run.metrics
            if isinstance(run.metrics, EventAnalysisMetrics)
            else EventAnalysisMetrics.model_validate(run.metrics)
        )

    return AnalysisView(
        analysis_run_id=run.analysis_run_id,
        event_id=run.event_id,
        trigger=run.trigger,
        previous_state_version=run.previous_state_version,
        new_state_version=run.new_state_version,
        capabilities_invoked=run.capabilities_invoked,
        capabilities_skipped=run.capabilities_skipped,
        models_invoked=run.models_invoked,
        impacts=run.impacts,
        forecasts=run.forecasts,
        deltas=[d.model_dump(mode="json") for d in run.deltas],
        metrics=metrics_obj,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


def _event_row(repo: Any, event_id: str) -> dict[str, Any]:
    for row in repo.events.all_events():
        if row["event_id"] == event_id:
            return row
    raise HTTPException(
        status_code=404,
        detail=f"unknown event '{event_id}'",
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


@router.get("", response_model=EventListResponse, responses={404: {"model": ErrorResponse}})
async def list_events(
    repo: RepoDep,
    status: str | None = Query(default=None, description="active, closed, or omit for all"),
    limit: int = Query(default=50, ge=1, le=500),
    since: str | None = Query(default=None, description="ISO-8601 updated_at lower bound"),
) -> EventListResponse:
    """Newest-first event list."""
    rows = repo.events.all_events()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if since:
        cutoff = parse_time(since)
        if cutoff is None:
            raise HTTPException(status_code=400, detail=f"could not parse 'since'={since!r}")
        rows = [r for r in rows if (_maybe_dt(r.get("updated_at")) or cutoff) >= cutoff]
    rows = rows[:limit]

    summaries: list[EventSummary] = []
    for row in rows:
        summary = _summary_from_row(row)
        state = repo.states.latest(summary.event_id)
        if state is not None:
            summary = summary.model_copy(
                update={
                    "state_version": state.state_version,
                    "last_reconstructed_at": state.reconstructed_at,
                }
            )
        summaries.append(summary)
    return EventListResponse(count=len(summaries), events=summaries)


@router.get(
    "/{event_id}",
    response_model=EventDetail,
    responses={404: {"model": ErrorResponse}},
)
async def get_event(event_id: str, repo: RepoDep) -> EventDetail:
    """The section 51 response shape."""
    row = _event_row(repo, event_id)
    summary = _summary_from_row(row)
    state = repo.states.latest(event_id)
    run = repo.runs.latest_for_event(event_id)

    if state is not None:
        summary = summary.model_copy(
            update={
                "state_version": state.state_version,
                "last_reconstructed_at": state.reconstructed_at,
            }
        )

    return EventDetail(
        event=summary,
        current_state=_current_state_view(state) if state else None,
        top_impacts=list(state.affected_infrastructure) if state else [],
        recent_changes=_recent_changes(repo.runs.for_event(event_id, limit=5)),
        forecast=list(run.forecasts) if run else [],
        evidence_summary=state.evidence if state else EvidenceSummary(),
        last_updated=_maybe_dt(row.get("updated_at")),
    )


def _recent_changes(runs: list[AnalysisRun]) -> list[RecentChange]:
    """What changed, newest analysis first.

    Section 32 makes 'what changed' first-class, so the deltas recorded on each
    analysis run are the honest source. Recomputing them here from state rows
    would re-run the delta engine and could disagree with what the platform
    actually believed at the time.
    """
    return [
        RecentChange(
            state_version=run.new_state_version,
            previous_state_version=run.previous_state_version,
            reconstructed_at=run.completed_at,
            magnitude=max((d.magnitude for d in run.deltas), default=None),
            changes=[_change_view(d) for d in run.deltas],
        )
        for run in runs
    ]


def _change_view(delta: StateDelta) -> dict[str, Any]:
    return {
        "change": delta.change,
        "domain": delta.domain,
        "before": delta.before,
        "after": delta.after,
        "magnitude": delta.magnitude,
        "confidence": delta.confidence,
        "novelty": delta.novelty,
        "causes": list(delta.causes),
        "official_guidance": delta.official_guidance,
    }


@router.get(
    "/{event_id}/timeline",
    response_model=TimelineResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_timeline(event_id: str, repo: RepoDep) -> TimelineResponse:
    """Every state version, oldest first - the append-only ledger of belief."""
    _event_row(repo, event_id)
    entries = [
        TimelineEntry(
            state_version=s.state_version,
            reconstructed_at=s.reconstructed_at,
            status=s.status,
            observation_ids=s.observation_ids,
            claim_ids=s.claim_ids,
            state=s.model_dump(mode="json"),
        )
        for s in repo.states.history(event_id)
    ]
    return TimelineResponse(event_id=event_id, count=len(entries), entries=entries)


@router.get(
    "/{event_id}/evidence",
    response_model=EvidenceResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_evidence(event_id: str, repo: RepoDep) -> EvidenceResponse:
    """Observations and claims behind the current belief, with provenance."""
    _event_row(repo, event_id)
    state = repo.states.latest(event_id)
    evidence = state.evidence if state else EvidenceSummary()
    # The narrative rides on the latest run, not the state: state reconstruction
    # is deterministic and offline, so a model's reading of the evidence has
    # nowhere to live in ``EventState`` (see ``EvidenceNarrative``).
    run = repo.runs.latest_for_event(event_id)
    return EvidenceResponse(
        event_id=event_id,
        source_count=evidence.source_count,
        independent_source_count=evidence.independent_source_count,
        authorities=dict(evidence.authorities),
        observation_types=dict(evidence.observation_types),
        contradictions=evidence.contradictions,
        freshness_seconds=evidence.freshness_seconds,
        vector=dict(evidence.vector),
        observations=repo.observations.list_for_event(event_id),
        claims=repo.claims.claims_for_event(event_id),
        narrative=run.narrative if run else None,
    )


@router.get(
    "/{event_id}/analysis",
    response_model=AnalysisResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_analysis(
    event_id: str,
    repo: RepoDep,
    limit: int = Query(default=10, ge=1, le=100),
) -> AnalysisResponse:
    """Analysis runs, newest first."""
    _event_row(repo, event_id)
    runs = [_analysis_view(r) for r in repo.runs.for_event(event_id, limit=limit)]
    return AnalysisResponse(event_id=event_id, count=len(runs), runs=runs)


@router.get(
    "/{event_id}/metrics",
    response_model=AnalysisMetricsResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_event_metrics(event_id: str, repo: RepoDep) -> AnalysisMetricsResponse:
    """Explicitly expose the complete intermediate analytical metrics (§10, §17, §18, §21-32, §43-47).

    Preserves what the platform knows, what changed, why it reached a conclusion,
    and how confident it is across all 10 analytical dimensions.
    """
    _event_row(repo, event_id)
    run = repo.runs.latest_for_event(event_id)
    if run is None or run.metrics is None:
        raise HTTPException(
            status_code=404,
            detail=f"no analysis metrics available for event '{event_id}'",
        )
    metrics_obj = (
        run.metrics
        if isinstance(run.metrics, EventAnalysisMetrics)
        else EventAnalysisMetrics.model_validate(run.metrics)
    )
    return AnalysisMetricsResponse(
        event_id=event_id,
        analysis_run_id=run.analysis_run_id,
        metrics=metrics_obj,
    )


@router.get(
    "/{event_id}/forecast",
    response_model=ForecastResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_forecast(event_id: str, repo: RepoDep) -> ForecastResponse:
    """Latest forecasts for the event."""
    _event_row(repo, event_id)
    run = repo.runs.latest_for_event(event_id)
    return ForecastResponse(
        event_id=event_id,
        analysis_run_id=run.analysis_run_id if run else None,
        forecasts=list(run.forecasts) if run else [],
    )


__all__ = ["router"]
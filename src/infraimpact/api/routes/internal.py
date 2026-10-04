"""Internal operator routes (brief section 50 ``/internal/*``).

These are the routes section 51 implies: model internals are withheld from
``/v1`` but must be reachable by an operator, or the platform cannot be
debugged. Nothing here invents behaviour - each route delegates to the runtime
subsystem that owns the work.

Note: this V1 ships without authentication. Bind to loopback only.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...bus.event_bus import Topic
from ...domain.ids import parse_time, utcnow
from ...domain.schemas import Observation
from ..deps import get_bus, get_repository, get_runtime
from ..schemas import ErrorResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class IngestRequest(BaseModel):
    """Section 7: at-least-once ingest with idempotent consumers."""

    observations: list[Observation] = Field(default_factory=list)


class IngestResponse(BaseModel):
    accepted: int = 0
    duplicates: int = 0
    observation_ids: list[str] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    event_id: str
    trigger: str = "manual"
    trigger_observation_id: str | None = None


class ReplayRequest(BaseModel):
    event_id: str
    as_of: str = Field(description="ISO-8601 moment to reconstruct belief at.")


class EvaluateRequest(BaseModel):
    event_id: str
    features: dict[str, Any] | None = None


def _prior_state(repo: Any, event_id: str, state_version: int) -> Any:
    """The version immediately before this one, if it exists."""
    return repo.states.version(event_id, state_version - 1) if state_version > 1 else None


@router.post("/observations", response_model=IngestResponse, responses={400: {"model": ErrorResponse}})
async def post_observations(
    payload: IngestRequest, request: Request
) -> IngestResponse:
    """Ingest normalized observations directly.

    Persisting and then publishing to ``normalized.observations`` is exactly what
    ``IngestionPipeline`` does, so the runtime resolves these identically to
    adapter output rather than taking a shortcut.
    """
    repo = get_repository(request)
    bus = get_bus(request)

    result = IngestResponse()
    for observation in payload.observations:
        written = repo.observations.append(observation)
        if written:
            result.accepted += 1
            result.observation_ids.append(observation.observation_id)
            await bus.publish(
                Topic.NORMALIZED_OBSERVATIONS,
                observation.model_dump(mode="json"),
                key=observation.source_id,
            )
        else:
            result.duplicates += 1
    return result


@router.post(
    "/analysis/request",
    responses={404: {"model": ErrorResponse}},
)
async def post_analysis_request(request: Request, payload: AnalysisRequest) -> dict[str, Any]:
    """Run one analysis for one event, on demand."""
    runtime = get_runtime(request)
    repo = get_repository(request)

    state = repo.states.latest(payload.event_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"no state for event '{payload.event_id}'; nothing to analyze",
        )

    outcome = await runtime.orchestrator.analyze(
        payload.event_id,
        state,
        trigger=payload.trigger,
        trigger_observation_id=payload.trigger_observation_id,
        previous_state=_prior_state(repo, payload.event_id, state.state_version),
    )
    return {
        "event_id": payload.event_id,
        "analysis_run_id": outcome.analysis_run_id,
        "state_version": outcome.state.state_version,
        "capabilities_invoked": outcome.capabilities_invoked,
        "capabilities_skipped": [list(s) for s in outcome.capabilities_skipped],
        "impacts": len(outcome.impacts),
        "forecasts": len(outcome.forecasts),
        "notes": outcome.notes,
        "completed_at": utcnow().isoformat(),
    }


@router.post("/replay", responses={404: {"model": ErrorResponse}})
async def post_replay(request: Request, payload: ReplayRequest) -> dict[str, Any]:
    """Section 49: what did we believe at this moment?

    Returns the state version current *at* ``as_of`` plus only the observations
    and claims that existed by then, so the reconstruction carries no future
    information.
    """
    repo = get_repository(request)
    as_of = parse_time(payload.as_of)
    if as_of is None:
        raise HTTPException(status_code=400, detail=f"could not parse 'as_of'={payload.as_of!r}")

    state = repo.states.state_as_of(payload.event_id, as_of)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no belief about '{payload.event_id}' at {as_of.isoformat()}; "
                f"the event may not have existed yet"
            ),
        )

    known_at = {o.observation_id for o in repo.observations.list_for_event(payload.event_id)
                if (o.observed_at <= as_of)}
    return {
        "event_id": payload.event_id,
        "as_of": as_of.isoformat(),
        "state_version": state.state_version,
        "state": state.model_dump(mode="json"),
        "observations_known_at": sorted(known_at),
        "observation_count_at": len(known_at),
        "observation_count_now": len(repo.observations.list_for_event(payload.event_id)),
    }


@router.post("/models/evaluate", responses={404: {"model": ErrorResponse}})
async def post_models_evaluate(request: Request, payload: EvaluateRequest) -> dict[str, Any]:
    """Full model internals for an event - the /v1 answers withheld.

    Section 51 forbids leaking these to normal clients; an operator debugging a
    wrong call still needs to see the feature vector and raw outputs.
    """
    runtime = get_runtime(request)
    repo = get_repository(request)

    state = repo.states.latest(payload.event_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"no state for event '{payload.event_id}'",
        )

    outcome = await runtime.orchestrator.analyze(
        payload.event_id,
        state,
        trigger="evaluate",
        previous_state=_prior_state(repo, payload.event_id, state.state_version),
    )
    return {
        "event_id": payload.event_id,
        "analysis_run_id": outcome.analysis_run_id,
        "features": {k: v.model_dump(mode="json") for k, v in outcome.features.items()},
        "model_outputs": [m.model_dump(mode="json") for m in outcome.model_outputs],
        "jev_decisions": outcome.jev_decisions,
        "capabilities_invoked": outcome.capabilities_invoked,
        "capabilities_skipped": [list(s) for s in outcome.capabilities_skipped],
        "deltas": [d.model_dump(mode="json") for d in outcome.delta_report.deltas],
        "forecast_rationale_notes": outcome.notes,
    }


@router.get("/models/registry")
async def get_models_registry(request: Request) -> dict[str, Any]:
    """Section 47/50: inspect the model registry, champion, challenger, and shadow models."""
    runtime = get_runtime(request)
    reg = getattr(runtime.orchestrator, "model_registry", None)
    if reg is None:
        from ...models.portfolio import build_default_model_registry
        reg = build_default_model_registry()
    return {
        "models": reg.list_models(),
        "total": len(reg),
    }


class PromoteModelRequest(BaseModel):
    model_id: str
    target_mode: str = Field(description="'champion', 'challenger', or 'shadow'")


@router.post("/models/promote", responses={404: {"model": ErrorResponse}})
async def post_models_promote(request: Request, payload: PromoteModelRequest) -> dict[str, Any]:
    """Section 47: promote a model between shadow, challenger, and champion modes."""
    from ...models.base import ModelDeploymentMode
    runtime = get_runtime(request)
    reg = getattr(runtime.orchestrator, "model_registry", None)
    if reg is None:
        raise HTTPException(status_code=404, detail="no model registry configured")
    try:
        mode = ModelDeploymentMode(payload.target_mode.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid target_mode '{payload.target_mode}'; expected champion, challenger, or shadow",
        )
    success = reg.promote(payload.model_id, mode)
    if not success:
        raise HTTPException(status_code=404, detail=f"model '{payload.model_id}' not found in registry")
    return {
        "model_id": payload.model_id,
        "new_deployment_mode": mode.value,
        "promoted": True,
    }


class TrainingDatasetRequest(BaseModel):
    event_id: str
    horizons_minutes: list[int] = Field(default_factory=lambda: [5, 15, 30, 60])
    sample_step_minutes: int = 15


@router.post("/training/dataset", responses={404: {"model": ErrorResponse}})
async def post_training_dataset(request: Request, payload: TrainingDatasetRequest) -> dict[str, Any]:
    """Section 46: extract a point-in-time, leak-free training dataset for an event."""
    repo = get_repository(request)
    from ...evaluation.training import PointInTimeDatasetBuilder

    state = repo.states.latest(payload.event_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no event found for id '{payload.event_id}'")

    builder = PointInTimeDatasetBuilder(repo)
    examples = builder.build_examples_for_event(
        event_id=payload.event_id,
        horizons_minutes=tuple(payload.horizons_minutes),
        sample_step_minutes=payload.sample_step_minutes,
    )
    return {
        "event_id": payload.event_id,
        "example_count": len(examples),
        "examples": [e.as_dict() for e in examples],
    }


class MetricsEvaluationRequest(BaseModel):
    predictions: list[float]
    ground_truth: list[float]
    horizons_minutes: list[int] | None = None


@router.post("/models/metrics")
async def post_models_metrics(payload: MetricsEvaluationRequest) -> dict[str, Any]:
    """Section 45: compute Brier score, ECE, precision, recall, and F1."""
    from ...evaluation.metrics import evaluate_forecasts

    if len(payload.predictions) != len(payload.ground_truth):
        raise HTTPException(status_code=400, detail="predictions and ground_truth must have equal length")
    horizons = payload.horizons_minutes or [30] * len(payload.predictions)
    pairs = list(zip(payload.predictions, payload.ground_truth, horizons))
    report = evaluate_forecasts(pairs)
    return report.as_dict()


__all__ = ["router"]
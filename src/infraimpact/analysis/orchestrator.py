"""Analysis orchestrator (brief sections 17, 18, 43).

The orchestrator is the only component that knows the *order* of operations.
It does not know *which* analyses to run - that is the relevance engine's job
(section 15). Adding earthquake support or any other analytical capability
means registering a capability, not editing a workflow.

    state + delta -> relevance -> capabilities -> AnalysisRun

Two outputs, always, per section 10:
    A. absolute state (what is true now)
    B. delta (what changed since the previous analysis)

Every production decision is replayable: the run records which capabilities
ran, which were skipped and why, which models produced output, what Jev decided
and which features fed them (section 43).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..bus.event_bus import EventBus, Topic
from ..delta.engine import DeltaReport, StateDeltaEngine
from ..domain.enums import CapabilityTier, InfrastructureDomain, TruthStatus, Urgency
from ..domain.ids import deterministic_id, utcnow
from ..domain.schemas import (
    AffectedInfrastructure,
    AnalysisRun,
    EventState,
    EvidenceNarrative,
    FeatureValue,
    Forecast,
    ModelOutput,
    StateDelta,
)
from ..graph.model import InfrastructureGraph
from ..jev.client import JevClient, JevQuestion, build_jev_client
from ..llm.interpreter import InterpretationLayer
from ..storage.repository import PlatformRepository
from .capabilities import (
    CapabilityContext,
    CapabilityRegistry,
    CapabilityResult,
    build_default_registry,
)
from .relevance import RelevanceEngine, ScoredCapability, Selection

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"

#: Cheaper and more fundamental capabilities run first so downstream models can
#: read features produced earlier in the same run. The order is explicit and
#: deliberate: a logistic model that reads ``arterial_overlap_count`` must run
#: after the geospatial capability that computes it.
DEPENDENCY_ORDER = (
    "source_conflict_analysis",    # -> information_confidence
    "event_classification",        # -> event_class
    "historical_similarity",       # -> historical similarity & analogues
    "road_network_exposure",       # -> arterial_overlap_count, road_overlap_count
    "transit_disruption",          # -> transit_route_overlap, transit_route_redundancy
    "transit_disruption_forecast", # -> transit delay & cancellation forecasts
    "critical_facility_exposure",  # -> critical_facility_inside
    "user_route_exposure",         # -> route_redundancy
    "infrastructure_propagation",  # -> propagation_reach
    "traffic_anomaly",             # -> traffic_anomaly
    "infrastructure_impact",       # consumes all of the above
    "time_to_impact",              # consumes p_* features
    "uncertainty_reduction",
)

_SEVERITY_SCORE = {
    Urgency.NONE: 0.0,
    Urgency.LOW: 0.25,
    Urgency.MODERATE: 0.5,
    Urgency.HIGH: 0.8,
    Urgency.IMMEDIATE: 1.0,
}


@dataclass
class AnalysisOutcome:
    """Everything one analysis produced."""

    event_id: str
    analysis_run_id: str
    trigger: str
    trigger_observation_id: str | None
    state: EventState
    delta_report: DeltaReport
    features: dict[str, FeatureValue]
    impacts: list[AffectedInfrastructure]
    forecasts: list[Forecast]
    model_outputs: list[ModelOutput]
    capabilities_invoked: list[str]
    capabilities_skipped: list[tuple[str, str]]
    jev_decisions: dict[str, Any]
    #: Section 18 hypotheses: infrastructure relationships worth examining next.
    #: Advisory only - nothing here becomes an impact without a capability
    #: measuring it, which is why the field exists while no code path promotes
    #: it. The API keeps it on the ``/internal`` route and off ``/v1``.
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    #: Section 18 evidence synthesis for this run, or ``None`` when the evidence
    #: did not support one. Persisted onto the run rather than the state - see
    #: :class:`~infraimpact.domain.schemas.EvidenceNarrative` for why.
    narrative: EvidenceNarrative | None = None
    notes: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metrics: Any = None

    @property
    def deltas(self) -> tuple[StateDelta, ...]:
        return self.delta_report.deltas

    @property
    def is_material(self) -> bool:
        return self.delta_report.is_material

    def forecast_for(self, domain: InfrastructureDomain) -> list[Forecast]:
        """Highest-probability forecast per horizon for one domain."""
        by_horizon: dict[int, Forecast] = {}
        for forecast in self.forecasts:
            if forecast.domain is not domain:
                continue
            current = by_horizon.get(forecast.horizon_minutes)
            if current is None or forecast.probability > current.probability:
                by_horizon[forecast.horizon_minutes] = forecast
        return [by_horizon[h] for h in sorted(by_horizon)]

    def peak_probability(self, domain: InfrastructureDomain) -> float:
        return max((f.probability for f in self.forecast_for(domain)), default=0.0)

    def peak_severity(self) -> Urgency:
        if not self.impacts:
            return Urgency.NONE
        return max(self.impacts, key=lambda i: _SEVERITY_SCORE[i.severity]).severity

    def domain_severity(self, domain: InfrastructureDomain) -> Urgency:
        relevant = [i for i in self.impacts if i.domain is domain]
        if not relevant:
            return Urgency.NONE
        return max(relevant, key=lambda i: _SEVERITY_SCORE[i.severity]).severity


class AnalysisOrchestrator:
    """Runs one analysis over one event."""

    def __init__(
        self,
        repository: PlatformRepository,
        bus: EventBus,
        *,
        region_id: str,
        graph: InfrastructureGraph | None = None,
        registry: CapabilityRegistry | None = None,
        relevance: RelevanceEngine | None = None,
        jev: JevClient | None = None,
        delta_engine: StateDeltaEngine | None = None,
        interpretation: InterpretationLayer | None = None,
        model_registry: Any | None = None,
        budget_per_run: int = 12,
    ) -> None:
        self.repo = repository
        self.bus = bus
        self.region_id = region_id
        self.graph = graph
        self.registry = registry or build_default_registry(graph)
        self.relevance = relevance or RelevanceEngine(
            self.registry, budget_per_run=budget_per_run
        )
        self.jev = jev or build_jev_client()
        self.delta = delta_engine or StateDeltaEngine()
        if model_registry is None:
            from ..models.portfolio import build_default_model_registry
            self.model_registry = build_default_model_registry()
        else:
            self.model_registry = model_registry
        #: Section 18 interpretation/orchestration. Optional and advisory: with
        #: no client the orchestrator runs identically minus the prose, which is
        #: the behaviour every test in this suite exercises.
        self.interpretation = interpretation or InterpretationLayer()

    async def analyze(
        self,
        event_id: str,
        state: EventState,
        *,
        trigger: str,
        trigger_observation_id: str | None = None,
        previous_state: EventState | None = None,
        previous_impacts: Sequence[AffectedInfrastructure] = (),
        previous_forecasts: Sequence[Forecast] = (),
        affected_user_count: int = 0,
    ) -> AnalysisOutcome:
        started_at = utcnow()
        observations = self.repo.observations.list_for_event(event_id)
        claims = self.repo.claims.claims_for_event(event_id)
        health = self.repo.source_health.all()

        previous_state, previous_impacts, previous_forecasts = self._prior(
            event_id, state, previous_state, previous_impacts, previous_forecasts
        )

        # 1. Delta first. It feeds the relevance engine, and an update with no
        #    material delta must not trigger triggered or expensive work.
        delta_report = self.delta.compare(
            previous_state,
            state,
            previous_impacts=previous_impacts,
            affected_user_count=affected_user_count,
        )

        ctx = CapabilityContext(
            region_id=self.region_id,
            state=state,
            observations=observations,
            claims=claims,
            delta=delta_report.deltas,
            graph=self.graph,
            source_health=health,
            trigger=trigger,
            repository=self.repo,
        )

        selection = self._select(ctx, delta_report)
        results, invoked, notes = self._execute(selection, ctx)

        # 2. Merge impacts: the state's directly-reported impacts beat analysis
        #    inferences on conflict, because they carry stronger truth status.
        impacts = merge_impacts(state.affected_infrastructure, results.impacts)
        forecasts = merge_forecasts(
            dedupe_forecasts(results.forecasts),
            observed_impact_forecasts(impacts, state),
        )

        # 3. Recompute the delta with analysis impacts and forecasts included,
        #    so infrastructure discovered by analysis is also a first-class
        #    change rather than an invisible side effect.
        delta_report = self.delta.compare(
            previous_state,
            state,
            impacts=impacts,
            forecasts=forecasts,
            previous_impacts=previous_impacts,
            previous_forecasts=previous_forecasts,
            affected_user_count=affected_user_count,
        )
        ctx.delta = delta_report.deltas
        ctx.features = results.features

        jev_decisions = self._ask_jev(state, results.features, impacts, delta_report)

        # Section 18 hypothesis generation. Questions to examine next, recorded
        # on the run for the next cycle - never published to /v1, never promoted
        # to an impact.
        hypotheses = self._ask_hypotheses(state, impacts)

        # Section 18 evidence synthesis. Runs after the state exists so the model
        # reads the *measured* evidence vector rather than a reconstruction of it,
        # and is skipped entirely when the delta is immaterial - there is no new
        # evidence to interpret when nothing changed.
        narrative = None
        if delta_report.is_material:
            try:
                narrative = self.interpretation.narrate_evidence(state, observations)
            except Exception as exc:  # noqa: BLE001 - prose is never load-bearing
                log.warning("evidence synthesis failed for %s: %r", event_id, exc)

        completed_at = utcnow()

        # Section 44 & 47: Run champion, challenger & shadow models from registry
        registry_outputs: list[ModelOutput] = []
        champion_forecasts: list[Forecast] = []
        if self.model_registry is not None:
            from ..models.base import ModelContext, ModelTaskType
            m_ctx = ModelContext(
                event_id=event_id,
                state_version=state.state_version,
                features=results.features,
                state=state,
                observations=observations,
            )
            for task_type in ModelTaskType:
                champ = self.model_registry.get_champion(task_type)
                if champ is not None:
                    try:
                        champ_pred = champ.predict(m_ctx)
                        registry_outputs.append(champ.to_model_output(champ_pred, state.state_version))
                        # Only real, non-placeholder forecasts enter the public forecast list
                        if not champ_pred.is_placeholder and champ_pred.forecasts:
                            champion_forecasts.extend(champ_pred.forecasts)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("champion inference failed for %s: %r", champ.model_id, exc)

                for side_model in [
                    *self.model_registry.get_challengers(task_type),
                    *self.model_registry.get_shadows(task_type),
                ]:
                    try:
                        side_pred = side_model.predict(m_ctx)
                        registry_outputs.append(side_model.to_model_output(side_pred, state.state_version))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("side inference failed for %s: %r", side_model.model_id, exc)

        all_model_outputs = [*results.model_outputs, *registry_outputs]
        if champion_forecasts:
            forecasts = merge_forecasts(forecasts, champion_forecasts)

        analysis_run_id = deterministic_id(
            "run", event_id, state.state_version, started_at.isoformat()
        )
        from .metrics import derive_analysis_metrics
        history_tracker = getattr(self.repo, "history", None)
        all_notes = [*notes, *delta_report.notes, *selection.notes]
        derived_metrics = derive_analysis_metrics(
            state=state,
            previous_state=previous_state,
            observations=observations,
            claims=claims,
            impacts=impacts,
            forecasts=forecasts,
            delta_report=delta_report,
            features=results.features,
            model_outputs=all_model_outputs,
            graph=self.graph,
            model_registry=self.model_registry,
            history=history_tracker,
            notes=all_notes,
            analysis_run_id=analysis_run_id,
        )

        outcome = AnalysisOutcome(
            event_id=event_id,
            analysis_run_id=analysis_run_id,
            trigger=trigger,
            trigger_observation_id=trigger_observation_id,
            state=state,
            delta_report=delta_report,
            features=dict(results.features),
            impacts=impacts,
            forecasts=forecasts,
            model_outputs=all_model_outputs,
            capabilities_invoked=invoked,
            capabilities_skipped=selection.skipped_reasons,
            jev_decisions=jev_decisions,
            hypotheses=list(hypotheses),
            narrative=narrative,
            notes=all_notes,
            started_at=started_at,
            completed_at=completed_at,
            metrics=derived_metrics,
        )

        self._persist(outcome)
        await self._publish(outcome)
        return outcome

    # -- prior-version resolution ------------------------------------------

    def _prior(
        self,
        event_id: str,
        state: EventState,
        previous_state: EventState | None,
        previous_impacts: Sequence[AffectedInfrastructure],
        previous_forecasts: Sequence[Forecast],
    ) -> tuple[
        EventState | None,
        Sequence[AffectedInfrastructure],
        Sequence[Forecast],
    ]:
        """Find the state version this analysis is compared against.

        A state is only "previous" if it was genuinely written earlier. When a
        caller passes the freshly-appended state, the repository's latest is the
        same row and must be ignored, or every run would diff against itself.
        """
        if previous_state is None:
            previous_state = self.repo.states.latest(event_id)
            if previous_state is not None and previous_state.state_version >= state.state_version:
                previous_state = None

        if not previous_impacts and previous_state is not None:
            prior_run = self.repo.runs.latest_for_event(event_id)
            if prior_run is not None and prior_run.new_state_version == previous_state.state_version:
                previous_impacts = prior_run.impacts
                previous_forecasts = previous_forecasts or prior_run.forecasts
        return previous_state, previous_impacts, previous_forecasts

    # -- selection and execution ------------------------------------------

    def _select(self, ctx: CapabilityContext, delta_report: DeltaReport) -> Selection:
        selection = self.relevance.select(ctx)
        selection = self._promote(ctx, delta_report, selection)

        # Section 33: an immaterial update keeps the continuous tier (cheap,
        # always-on state reconstruction) but drops everything triggered or
        # expensive, because nothing about the world moved.
        if delta_report.is_material:
            return selection

        reason = f"no material state change ({delta_report.suppressed_reason})"
        retained: list[ScoredCapability] = []
        dropped: list[ScoredCapability] = []
        for scored in selection.selected:
            (retained if scored.capability.tier is CapabilityTier.CHEAP else dropped).append(scored)

        dropped_ids = {s.capability_id for s in dropped}
        selection.selected = retained
        selection.skipped = [s for s in selection.skipped if s.capability_id not in dropped_ids]
        selection.skipped.extend(
            ScoredCapability(s.capability, s.relevance, reason, True) for s in dropped
        )
        return selection

    def _promote(
        self, ctx: CapabilityContext, delta_report: DeltaReport, selection: Selection
    ) -> Selection:
        """Let the LLM reorder what the relevance engine already chose.

        Section 18 lists dynamic orchestration as a legitimate LLM job, and this
        is where it happens - but only over the engine's own output. Nothing is
        added to ``selected``; a promoted capability that was scored below the
        budget simply runs earlier. If the engine skipped it, it stays skipped,
        and the reason the engine recorded is preserved rather than overwritten.

        The asymmetry is deliberate. Letting a model *introduce* capabilities
        would mean trusting it to know what this build's registry contains, which
        it cannot know; the failure mode is a plausible-sounding analysis name
        that does not exist. Letting it reorder is safe and is where the actual
        value is: the relevance engine is good at scoring, and a language model
        reading the same state can tell that a cheap capability matters more here
        than its numeric relevance suggests.
        """

        if not selection.selected or not delta_report.deltas:
            return selection

        try:
            promoted = set(
                self.interpretation.recommend_capabilities(
                    state=ctx.state,
                    delta_report=delta_report,
                    candidates=[s.capability for s in selection.selected],
                )
            )
        except Exception as exc:  # noqa: BLE001 - an LLM failure changes nothing
            log.warning("capability advice failed: %r", exc)
            return selection

        if not promoted:
            return selection

        selection.selected.sort(
            key=lambda s: (0 if s.capability_id in promoted else 1, -s.relevance)
        )
        selection.notes.append(
            f"llm promoted {len(promoted)} of {len(selection.selected)} capabilities: "
            + ", ".join(sorted(promoted))
        )
        return selection

    def _execute(
        self, selection: Selection, ctx: CapabilityContext
    ) -> tuple[CapabilityResult, list[str], list[str]]:
        combined = CapabilityResult()
        invoked: list[str] = []
        notes: list[str] = []

        for scored in order_by_dependency(selection.selected):
            capability = scored.capability
            try:
                result = capability.run(ctx)
            except Exception as exc:  # noqa: BLE001 - one capability must not sink the run
                log.exception("capability %s failed", capability.capability_id)
                selection.skipped.append(
                    ScoredCapability(capability, scored.relevance, f"execution error: {exc!r}", True)
                )
                notes.append(f"{capability.capability_id} failed: {exc!r}")
                continue

            combined.merge(result)
            invoked.append(capability.capability_id)
            ctx.features = combined.features
            notes.extend(f"[{capability.capability_id}] {n}" for n in result.notes)
        return combined, invoked, notes

    # -- decision layer ----------------------------------------------------

    def _ask_jev(
        self,
        state: EventState,
        features: dict[str, FeatureValue],
        impacts: Sequence[AffectedInfrastructure],
        delta_report: DeltaReport,
    ) -> dict[str, Any]:
        """Bounded decisions over already-measured facts (section 21).

        Jev never forecasts here. It chooses between bounded options using
        values the analysis layer already computed, so every decision is
        explainable by the numbers that produced it.
        """
        reliability = features.get("information_confidence")
        questions = [
            JevQuestion(
                question_id="primary_impact_domain",
                kind="choice",
                text="Which infrastructure domain is most affected?",
                options=sorted(domain_scores(impacts)),
                state=domain_scores(impacts),
            ),
            JevQuestion(
                question_id="change_magnitude",
                kind="noul",
                text="How much did the world state change in this update?",
                state={"magnitude": delta_report.magnitude},
            ),
            JevQuestion(
                question_id="information_reliability",
                kind="noul",
                text="How much confidence should the presentation layer place in this?",
                state={
                    "user_exposure": float(
                        (reliability.value if reliability else None) or 0.4
                    )
                },
            ),
        ]
        try:
            return self.jev.ask(questions)
        except Exception as exc:  # noqa: BLE001 - a decision failure is not an analysis failure
            log.warning("jev decision layer failed: %r", exc)
            return {}

    def _ask_hypotheses(
        self, state: EventState, impacts: Sequence[AffectedInfrastructure]
    ) -> list[dict[str, Any]]:
        """Section 18 hypothesis generation, advisory only.

        Returns questions to examine on the next pass. Deliberately not executed:
        a hypothesis is an invitation to run a capability, and the capability has
        to do the measuring. The orchestrator therefore records the hypothesis
        and nothing more, which is the difference between the LLM suggesting a
        line of inquiry and the LLM asserting infrastructure is affected.
        """

        try:
            return list(self.interpretation.hypotheses(state, impacts))
        except Exception as exc:  # noqa: BLE001 - an LLM failure is not an analysis failure
            log.warning("hypothesis generation failed: %r", exc)
            return []

    # -- persistence and publication ---------------------------------------

    def _persist(self, outcome: AnalysisOutcome) -> None:
        self.repo.runs.append(
            AnalysisRun(
                analysis_run_id=outcome.analysis_run_id,
                schema_version=SCHEMA_VERSION,
                event_id=outcome.event_id,
                region_id=self.region_id,
                trigger=outcome.trigger,
                trigger_observation_id=outcome.trigger_observation_id,
                previous_state_version=(
                    outcome.state.state_version - 1
                    if outcome.state.state_version > 1
                    else None
                ),
                new_state_version=outcome.state.state_version,
                features=outcome.features,
                capabilities_invoked=tuple(outcome.capabilities_invoked),
                capabilities_skipped=tuple(outcome.capabilities_skipped),
                models_invoked=tuple(sorted({m.model_id for m in outcome.model_outputs})),
                jev_decisions=outcome.jev_decisions,
                llm_operations=self.interpretation.operations,
                hypotheses=tuple(outcome.hypotheses),
                narrative=outcome.narrative,
                forecasts=tuple(outcome.forecasts),
                impacts=tuple(outcome.impacts),
                deltas=outcome.deltas,
                model_outputs=tuple(outcome.model_outputs),
                metrics=outcome.metrics.model_dump(mode="json") if hasattr(outcome.metrics, "model_dump") else outcome.metrics,
                started_at=outcome.started_at or utcnow(),
                completed_at=outcome.completed_at or utcnow(),
                software_versions={"orchestrator": "1.0.0"},
            )
        )

    async def _publish(self, outcome: AnalysisOutcome) -> None:
        await self.bus.publish(
            Topic.ANALYSIS_COMPLETED,
            {
                "analysis_run_id": outcome.analysis_run_id,
                "event_id": outcome.event_id,
                "state_version": outcome.state.state_version,
                "trigger": outcome.trigger,
                "capabilities_invoked": outcome.capabilities_invoked,
                "capabilities_skipped": outcome.capabilities_skipped,
                "impact_count": len(outcome.impacts),
                "forecast_count": len(outcome.forecasts),
                "is_material": outcome.is_material,
                "deltas": [d.model_dump(mode="json") for d in outcome.deltas],
                "suppressed_reason": outcome.delta_report.suppressed_reason,
            },
            key=outcome.event_id,
        )
        if outcome.is_material:
            await self.bus.publish(
                Topic.IMPACT_UPDATED,
                {
                    "event_id": outcome.event_id,
                    "analysis_run_id": outcome.analysis_run_id,
                    "impacts": [i.model_dump(mode="json") for i in outcome.impacts],
                    "forecasts": [f.model_dump(mode="json") for f in outcome.forecasts],
                },
                key=outcome.event_id,
            )


# --------------------------------------------------------------------------
# helpers (module-level so the user layer can reuse them)
# --------------------------------------------------------------------------


def order_by_dependency(scored: Sequence[ScoredCapability]) -> list[ScoredCapability]:
    """Sort selected capabilities so intra-run feature dependencies resolve.

    Within the same dependency rank, higher relevance runs first.
    """
    return sorted(
        scored,
        key=lambda s: (dependency_rank(s.capability), -s.relevance),
    )


def dependency_rank(capability: Any) -> int:
    try:
        return DEPENDENCY_ORDER.index(capability.capability_id)
    except ValueError:
        return len(DEPENDENCY_ORDER)


def merge_impacts(
    reported: Sequence[AffectedInfrastructure],
    inferred: Sequence[AffectedInfrastructure],
) -> list[AffectedInfrastructure]:
    """Reported impacts win over analysis inferences with the same identifier."""
    merged: dict[str, AffectedInfrastructure] = {i.identifier: i for i in inferred}
    for impact in reported:
        existing = merged.get(impact.identifier)
        merged[impact.identifier] = (
            impact if existing is None else existing.model_copy(update=_prefer(impact, existing))
        )
    return sorted(
        merged.values(),
        key=lambda i: (-_SEVERITY_SCORE[i.severity], i.identifier),
    )


def _prefer(
    reported: AffectedInfrastructure, inferred: AffectedInfrastructure
) -> dict[str, Any]:
    """Reported truth status wins; geometry, name and evidence are unioned."""
    return {
        "truth_status": reported.truth_status,
        "severity": (
            inferred.severity
            if reported.severity == Urgency.LOW
            else reported.severity
        ),
        "geometry": reported.geometry or inferred.geometry,
        "observation_ids": tuple(
            dict.fromkeys((*reported.observation_ids, *inferred.observation_ids))
        ),
        "name": reported.name or inferred.name,
    }


def dedupe_forecasts(forecasts: Sequence[Forecast]) -> list[Forecast]:
    """Keep the highest probability per (target, horizon)."""
    best: dict[tuple[str, int], Forecast] = {}
    for forecast in forecasts:
        key = (forecast.target, forecast.horizon_minutes)
        current = best.get(key)
        if current is None or forecast.probability > current.probability:
            best[key] = forecast
    return [best[k] for k in sorted(best)]


def merge_forecasts(*groups: Sequence[Forecast]) -> list[Forecast]:
    out: list[Forecast] = []
    for group in groups:
        out.extend(group)
    return dedupe_forecasts(out)


def domain_scores(impacts: Sequence[AffectedInfrastructure]) -> dict[str, float]:
    """Per-domain impact score in 0..1, for the bounded choice question."""
    scores: dict[str, float] = {}
    for impact in impacts:
        domain = str(impact.domain)
        scores[domain] = max(scores.get(domain, 0.0), _SEVERITY_SCORE[impact.severity])
    for domain in ("road", "transit", "utility", "public_safety", "ferries"):
        scores.setdefault(domain, 0.0)
    return scores


def observed_impact_forecasts(
    impacts: Sequence[AffectedInfrastructure],
    state: EventState,
) -> list[Forecast]:
    """Present-tense 'impact is currently observed' signal per impacted domain.

    This is *not* a prediction: it reports impact already recorded in the
    state's own infrastructure facts. It is shaped as a Forecast only because
    downstream consumers read probabilities uniformly. It carries
    ``model_id="observed-impact"`` and the state's truth status, so it can never
    be mistaken for a model-generated probability.
    """
    confidence = state.evidence.vector.get("source_authority", 0.4)
    out: list[Forecast] = []
    for domain in dict.fromkeys(i.domain for i in impacts):
        relevant = [i for i in impacts if i.domain is domain]
        grounded = [
            i
            for i in relevant
            if i.truth_status in {TruthStatus.CONFIRMED, TruthStatus.REPORTED}
        ]
        if not grounded:
            continue
        probability = round(len(grounded) / len(relevant), 4)
        spread = round(0.2 * (1 - confidence), 4)
        for horizon in (5, 15, 30, 60):
            out.append(
                Forecast(
                    target=f"{domain.value}_impact_present",
                    domain=domain,
                    horizon_minutes=horizon,
                    probability=probability,
                    lower=round(max(0.0, probability - spread), 4),
                    upper=round(min(1.0, probability + spread), 4),
                    model_id="observed-impact",
                    model_version="1.0.0",
                    calibration_version="observed-v1",
                    truth_status=TruthStatus.CONFIRMED,
                )
            )
    return out


__all__ = [
    "DEPENDENCY_ORDER",
    "SCHEMA_VERSION",
    "AnalysisOrchestrator",
    "AnalysisOutcome",
    "dedupe_forecasts",
    "domain_scores",
    "merge_forecasts",
    "merge_impacts",
    "observed_impact_forecasts",
    "order_by_dependency",
]
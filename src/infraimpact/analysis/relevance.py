"""Relevance engine (brief section 15).

Every analysis *considers* the whole capability registry, but must not
*execute* everything. Expected relevance is ranked by:

    Expected Relevance
      x Potential User Impact
      x Information Gain
      x Uncertainty Reduction
      / Computation Cost

This is what makes the workflow dynamic instead of hardcoded, while still
feasible to run continuously.

"Full analysis" therefore means: consider all capabilities, refresh what the
state warrants (section 16).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..domain.enums import CapabilityTier, ObservationType
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityRegistry

log = logging.getLogger(__name__)


@dataclass
class ScoredCapability:
    capability: AnalysisCapability
    relevance: float
    reason: str
    applicable: bool

    @property
    def capability_id(self) -> str:
        return self.capability.capability_id


@dataclass
class Selection:
    selected: list[ScoredCapability] = field(default_factory=list)
    skipped: list[ScoredCapability] = field(default_factory=list)
    #: Advisory annotations from layers outside the relevance engine - currently
    #: only the LLM's section-18 capability advice. Kept on the selection so the
    #: orchestrator can surface them on the run without the engine knowing
    #: anything about who wrote them.
    notes: list[str] = field(default_factory=list)

    @property
    def selected_ids(self) -> list[str]:
        return [s.capability_id for s in self.selected]

    @property
    def skipped_reasons(self) -> list[tuple[str, str]]:
        return [(s.capability_id, s.reason) for s in self.skipped]


#: Observation types that make a "cheap" capability always worth running.
_ALWAYS_CHEAP = frozenset(
    {
        ObservationType.ROAD_CLOSURE,
        ObservationType.TRANSIT_SERVICE_ALERT,
        ObservationType.POLICE_RESPONSE,
        ObservationType.NEWS_ARTICLE,
        ObservationType.PERMIT_EVENT,
        ObservationType.OFFICIAL_EMERGENCY_NOTICE,
        ObservationType.EARTHQUAKE,
    }
)


class RelevanceEngine:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        budget_per_run: int = 12,
        min_relevance: float = 0.05,
    ) -> None:
        self.registry = registry
        self.budget_per_run = budget_per_run
        self.min_relevance = min_relevance

    def select(self, ctx: CapabilityContext) -> Selection:
        scored: list[ScoredCapability] = []
        for capability in self.registry.all():
            applicable, reason = capability.is_applicable(ctx)
            if not applicable:
                scored.append(ScoredCapability(capability, 0.0, reason, False))
                continue
            relevance = self._relevance(capability, ctx)
            scored.append(
                ScoredCapability(capability, relevance, self._reason(capability, ctx, relevance), True)
            )

        applicable = sorted(
            (s for s in scored if s.applicable), key=lambda s: -s.relevance
        )
        selection = Selection()

        for item in applicable:
            if len(selection.selected) >= self.budget_per_run:
                selection.skipped.append(
                    ScoredCapability(item.capability, item.relevance, "compute budget exhausted", True)
                )
                continue
            if item.relevance < self.min_relevance:
                selection.skipped.append(item)
                continue
            selection.selected.append(item)

        skipped_ids = {s.capability_id for s in selection.skipped}
        for item in scored:
            if not item.applicable and item.capability.capability_id not in skipped_ids:
                selection.skipped.append(item)

        return selection

    # -- scoring ----------------------------------------------------------

    def _relevance(self, capability: AnalysisCapability, ctx: CapabilityContext) -> float:
        expected_relevance = self._expected_relevance(capability, ctx)
        user_impact = self._potential_user_impact(ctx)
        information_gain = self._information_gain(capability, ctx)
        uncertainty_reduction = self._uncertainty_reduction(capability, ctx)
        cost = max(0.01, capability.estimated_cost)

        value = (expected_relevance * user_impact * information_gain * uncertainty_reduction) / cost

        # Cheap capabilities get a floor so the continuous tier always runs:
        # state reconstruction must never be skipped for being "irrelevant".
        if capability.tier == CapabilityTier.CHEAP:
            value = max(value, 0.2)
        # Observation types that are inherently user-relevant get a further
        # floor: a road closure must never be skipped for being "irrelevant".
        if capability.triggered_by & _ALWAYS_CHEAP & ctx.observation_types:
            value = max(value, 0.5)
        # Expensive work needs a genuinely interesting situation.
        if capability.tier == CapabilityTier.EXPENSIVE and expected_relevance < 0.35:
            value *= 0.25

        return round(min(value, 10.0), 6)

    def _expected_relevance(self, capability: AnalysisCapability, ctx: CapabilityContext) -> float:
        if not capability.triggered_by:
            return 0.4
        hits = capability.triggered_by & ctx.observation_types
        if not hits:
            return 0.0
        # More distinct triggering types => more reason to run.
        return round(min(1.0, 0.35 + 0.25 * len(hits)), 4)

    def _potential_user_impact(self, ctx: CapabilityContext) -> float:
        """Would anyone be affected? Derived from state, not from a route map."""
        domains = ctx.affected_domains
        severity = float(ctx.state.derived.get("severity_peak", 0.0)) / 4.0
        reach = min(1.0, float(ctx.feature("propagation_reach", 0.0) or 0.0) / 15.0)
        footprint = min(1.0, float(ctx.state.derived.get("footprint_radius_m", 0.0) or 0.0) / 1500.0)
        base = 0.2 + 0.4 * severity + 0.2 * reach + 0.2 * footprint
        if domains & {
            "road",
            "transit",
        }:
            base += 0.15
        return round(min(1.0, base), 4)

    def _information_gain(self, capability: AnalysisCapability, ctx: CapabilityContext) -> float:
        """How much would this capability tell us that we do not know?"""
        unknown = 1.0 - float(ctx.feature("information_confidence", 0.4) or 0.4)
        gaps = ctx.feature("open_information_gaps", []) or []
        gap_hit = 1.0 if any(g in str(capability.capability_id) for g in gaps) else 0.5
        return round(min(1.0, 0.4 * unknown + 0.6 * gap_hit), 4)

    def _uncertainty_reduction(self, capability: AnalysisCapability, ctx: CapabilityContext) -> float:
        contradictions = float(ctx.feature("source_contradictions", 0.0) or 0.0)
        if capability.capability_id == "source_conflict_analysis":
            return 1.0 if contradictions else 0.6
        if capability.capability_id == "uncertainty_reduction":
            return 0.9
        if capability.capability_id == "event_classification":
            return 0.7
        degraded = any(h.state.value in {"offline", "degraded"} for h in ctx.source_health)
        if degraded:
            return 0.8
        return 0.6

    def _reason(
        self, capability: AnalysisCapability, ctx: CapabilityContext, relevance: float
    ) -> str:
        if capability.tier == CapabilityTier.CHEAP:
            return f"always-run continuous tier (relevance={relevance:.3f})"
        triggers = sorted(str(t) for t in (capability.triggered_by & ctx.observation_types))
        if triggers:
            return (
                f"triggered by {', '.join(triggers[:3])}; "
                f"severity={ctx.state.derived.get('severity_peak', 0.0)}; "
                f"relevance={relevance:.3f}"
            )
        return f"domain={sorted(str(d) for d in ctx.affected_domains)}; relevance={relevance:.3f}"


__all__ = ["RelevanceEngine", "ScoredCapability", "Selection"]
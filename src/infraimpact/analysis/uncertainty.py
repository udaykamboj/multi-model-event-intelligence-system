"""Uncertainty-reduction capabilities (brief section 20).

The uncertainty-reduction loop asks: what do we not know that could materially
change the result? These capabilities surface that gap explicitly so the
orchestrator can act on it.
"""

from __future__ import annotations

from collections import defaultdict

from ..domain.enums import CapabilityTier, ObservationType
from ..domain.schemas import FeatureValue
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult


class SourceConflictCapability(AnalysisCapability):
    """Detect source disagreement and degraded inputs.

    Section 55: never silently treat "no data" as "nothing happening". A stale
    or offline source must lower confidence rather than vanish from the model.
    """

    capability_id = "source_conflict_analysis"
    tier = CapabilityTier.CHEAP
    description = "Source disagreement, corroboration quality, and input staleness."
    estimated_latency_ms = 20
    estimated_cost = 0.02
    model_version = "deterministic-v1"

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()

        vector = dict(ctx.state.evidence.vector)
        contradictions = ctx.state.evidence.contradictions

        offline = [h.source_id for h in ctx.source_health if h.state.value == "offline"]
        degraded = [h.source_id for h in ctx.source_health if h.state.value == "degraded"]
        delayed = [h.source_id for h in ctx.source_health if h.state.value == "delayed"]

        # Independence discount: 10 syndicated articles are not 10 sources.
        independence = vector.get("independent_corroboration", 0.0)
        authority = vector.get("source_authority", 0.0)
        freshness = vector.get("freshness", 0.0)

        base_confidence = (
            0.45 * authority + 0.35 * independence + 0.20 * freshness
        )
        penalty = min(0.3, 0.1 * contradictions)
        if offline or degraded:
            # Explicit confidence reduction proportional to how much of the
            # expected input picture is missing.
            penalty += 0.08 * (len(offline) + len(degraded))

        confidence = max(0.05, min(1.0, base_confidence - penalty))

        result.features.update(
            {
                "source_contradictions": FeatureValue(
                    name="source_contradictions", value=float(contradictions)
                ),
                "information_confidence": FeatureValue(
                    name="information_confidence", value=round(confidence, 4)
                ),
                "sources_offline": FeatureValue(
                    name="sources_offline", value=float(len(offline))
                ),
                "sources_degraded": FeatureValue(
                    name="sources_degraded", value=float(len(degraded))
                ),
                "sources_delayed": FeatureValue(
                    name="sources_delayed", value=float(len(delayed))
                ),
                "independent_source_ratio": FeatureValue(
                    name="independent_source_ratio",
                    value=round(
                        (ctx.state.evidence.independent_source_count / max(1, ctx.state.evidence.source_count)),
                        4,
                    ),
                ),
            }
        )

        if contradictions:
            result.notes.append(
                f"{contradictions} predicate(s) have conflicting values across sources; "
                "disagreement retained rather than averaged"
            )
        if offline or degraded or delayed:
            result.notes.append(
                f"input health: offline={offline or 'none'} degraded={degraded or 'none'} delayed={delayed or 'none'}"
            )
        return result


class UncertaintyGapCapability(AnalysisCapability):
    """Section 20: what would reduce uncertainty, and by how much."""

    capability_id = "uncertainty_reduction"
    tier = CapabilityTier.EXPENSIVE
    description = "Rank outstanding information gaps by expected value."
    estimated_latency_ms = 200
    estimated_cost = 0.4
    model_version = "deterministic-v1"

    #: gap -> what would resolve it
    RESOLVERS = {
        "event_geometry": "query recent police/news location reports",
        "road_status": "query current transportation closure feed",
        "transit_status": "query GTFS-RT service alerts",
        "user_location": "request user location (explicitly, with consent)",
        "crowd_size": "query additional corroborating reporting",
        "official_guidance": "query official emergency notification channel",
        "event_duration": "query permit record / organiser statements",
        "traffic_state": "query traffic flow sensors",
        "utility_status": "query utility outage feed",
    }

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        gaps: dict[str, float] = defaultdict(float)

        if ctx.state.geometry is None or ctx.state.geometry_confidence < 0.5:
            gaps["event_geometry"] += 0.8
        road_related = {ObservationType.ROAD_CLOSURE, ObservationType.ROAD_CONSTRUCTION}
        if ctx.observation_types & road_related and not ctx.state.affected_infrastructure:
            gaps["road_status"] += 0.7
        if not ctx.state.evidence.independent_source_count:
            gaps["crowd_size"] += 0.5
        if ctx.state.evidence.contradictions:
            gaps["crowd_size"] += 0.3 * min(3, ctx.state.evidence.contradictions)
        if any(h.state.value in {"offline", "degraded"} for h in ctx.source_health):
            gaps["road_status"] += 0.3
        if ctx.state.derived.get("is_official_guidance", 0.0) == 0.0 and ctx.state.evidence.independent_source_count >= 2:
            gaps["official_guidance"] += 0.2

        if not gaps:
            return result

        ranked = sorted(gaps.items(), key=lambda kv: -kv[1])
        result.features["open_information_gaps"] = FeatureValue(
            name="open_information_gaps",
            value=[name for name, _ in ranked],
            confidence=round(min(1.0, ranked[0][1]), 4),
        )
        result.features["information_value_top_gap"] = FeatureValue(
            name="information_value_top_gap",
            value=ranked[0][0],
            confidence=round(min(1.0, ranked[0][1]), 4),
        )
        for name, value in ranked[:3]:
            result.notes.append(
                f"information gap '{name}' (value {value:.2f}): {self.RESOLVERS.get(name, 'query available sources')}"
            )
        return result


__all__ = ["SourceConflictCapability", "UncertaintyGapCapability"]
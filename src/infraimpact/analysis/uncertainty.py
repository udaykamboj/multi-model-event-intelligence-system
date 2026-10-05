"""Uncertainty-reduction capabilities (brief section 20).

The uncertainty-reduction loop asks: what do we not know that could materially
change the result? These capabilities surface that gap explicitly so the
orchestrator can act on it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

from ..domain.enums import Authority, CapabilityTier, ObservationType
from ..domain.schemas import FeatureValue
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult


@dataclass
class BetaPrior:
    """Beta distribution parameters for source reliability conjugate updating."""

    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        a, b = self.alpha, self.beta
        return (a * b) / (((a + b) ** 2) * (a + b + 1.0))


class BayesianSourceReliabilityTracker:
    """Tracks Bayesian source reliability using conjugate Beta-Binomial updating (section 37)."""

    DEFAULT_PRIORS: dict[Authority, tuple[float, float]] = {
        Authority.OFFICIAL: (9.0, 1.0),
        Authority.SEMI_OFFICIAL: (7.5, 2.5),
        Authority.ESTABLISHED_MEDIA: (7.0, 3.0),
        Authority.COMMUNITY: (5.0, 5.0),
        Authority.UNVERIFIED: (3.0, 5.0),
        Authority.INTERNAL: (8.0, 2.0),
    }

    def __init__(self) -> None:
        self._sources: dict[str, BetaPrior] = {}

    def get_prior(self, authority: Authority | str | None) -> BetaPrior:
        if isinstance(authority, str):
            try:
                auth_enum = Authority(authority)
            except ValueError:
                auth_enum = Authority.COMMUNITY
        elif isinstance(authority, Authority):
            auth_enum = authority
        else:
            auth_enum = Authority.COMMUNITY

        a, b = self.DEFAULT_PRIORS.get(auth_enum, (5.0, 5.0))
        return BetaPrior(alpha=a, beta=b)

    def register_observation(
        self,
        source_id: str,
        authority: Authority | str | None,
        confirmed: bool = True,
        weight: float = 1.0,
    ) -> None:
        if source_id not in self._sources:
            prior = self.get_prior(authority)
            self._sources[source_id] = BetaPrior(alpha=prior.alpha, beta=prior.beta)

        curr = self._sources[source_id]
        if confirmed:
            curr.alpha += weight
        else:
            curr.beta += weight

    def expected_reliability(self, source_id: str, authority: Authority | str | None = None) -> float:
        if source_id in self._sources:
            return round(self._sources[source_id].mean, 4)
        return round(self.get_prior(authority).mean, 4)


def fuse_evidence_probabilities(
    reports: Sequence[tuple[bool, float]],
    prior_probability: float = 0.5,
) -> float:
    """Multi-source Bayesian evidence fusion via log-likelihood ratio updating (section 36).

    Given independent reports (affirms_hypothesis: bool, source_reliability: float),
    computes the posterior belief P(H | E).
    """
    if not reports:
        return prior_probability

    p0 = max(0.01, min(0.99, prior_probability))
    log_odds = math.log(p0 / (1.0 - p0))

    for affirms, reliability in reports:
        r = max(0.05, min(0.95, reliability))
        if affirms:
            lr = r / (1.0 - r)
        else:
            lr = (1.0 - r) / r
        log_odds += math.log(lr)

    posterior = 1.0 / (1.0 + math.exp(-log_odds))
    return round(max(0.01, min(0.99, posterior)), 4)


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

        tracker = BayesianSourceReliabilityTracker()
        reports: list[tuple[bool, float]] = []
        for obs in ctx.observations:
            auth = getattr(obs.provenance, "authority", None)
            rel = tracker.expected_reliability(obs.source_id, auth)
            reports.append((True, rel))

        if contradictions:
            for _ in range(contradictions):
                reports.append((False, 0.70))

        fusion_prob = fuse_evidence_probabilities(reports, prior_probability=0.5)

        if ctx.observations:
            reliabilities = [
                tracker.expected_reliability(o.source_id, getattr(o.provenance, "authority", None))
                for o in ctx.observations
            ]
            bayes_rel = sum(reliabilities) / len(reliabilities)
        else:
            bayes_rel = vector.get("source_reliability", authority)

        result.features.update(
            {
                "source_contradictions": FeatureValue(
                    name="source_contradictions", value=float(contradictions)
                ),
                "information_confidence": FeatureValue(
                    name="information_confidence", value=round(confidence, 4)
                ),
                "probabilistic_fusion_score": FeatureValue(
                    name="probabilistic_fusion_score", value=round(fusion_prob, 4)
                ),
                "source_authority_score": FeatureValue(
                    name="source_authority_score", value=round(authority, 4)
                ),
                "source_reliability_score": FeatureValue(
                    name="source_reliability_score",
                    value=round(bayes_rel, 4),
                ),
                "freshness_score": FeatureValue(
                    name="freshness_score", value=round(freshness, 4)
                ),
                "source_corroboration_score": FeatureValue(
                    name="source_corroboration_score", value=round(independence, 4)
                ),
                "source_disagreement_score": FeatureValue(
                    name="source_disagreement_score", value=round(penalty, 4)
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


__all__ = [
    "BayesianSourceReliabilityTracker",
    "BetaPrior",
    "SourceConflictCapability",
    "UncertaintyGapCapability",
    "fuse_evidence_probabilities",
]
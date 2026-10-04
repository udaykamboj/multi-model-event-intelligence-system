"""Source dependency graph and independent corroboration (brief section 40).

Section 40 requirements:
  - "Five articles do not equal five sources if all cite the same original report."
  - Maintain SourceDependencyGraph.
  - Identify: original reporting, syndicated copies, government source, secondary reporting.
  - Corroboration scores must discount dependent sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..domain.enums import Authority, SourceType
from ..domain.schemas import Observation


@dataclass(frozen=True)
class CorroborationAnalysis:
    """The result of analyzing observation sources for true independence."""

    total_observations: int
    raw_source_count: int
    independent_source_count: int
    independence_ratio: float
    corroboration_score: float
    primary_sources: tuple[str, ...]
    secondary_sources: tuple[str, ...]
    source_clusters: dict[str, list[str]]


class SourceDependencyGraph:
    """Tracks syndication, citations, and parentage across information sources."""

    # Known wire / syndication / aggregator patterns
    KNOWN_SYNDICATES: frozenset[str] = frozenset(
        {
            "associated_press",
            "reuters",
            "prnewswire",
            "businesswire",
            "gdelt",
        }
    )

    # Keywords in headlines or text indicating citation of official / primary sources
    CITATION_PATTERNS = re.compile(
        r"(according to|reported by|per|citing)\s+(police|spd|sdot|wsdot|metro|fire|officials|authorities|usgs|nws)",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        # source_id -> parent_source_id (if known fixed dependency)
        self._dependencies: dict[str, str] = {
            "news.king5_syndicated": "news.king5",
            "news.komo_syndicated": "news.komo",
        }

    def register_dependency(self, child_source_id: str, parent_source_id: str) -> None:
        self._dependencies[child_source_id] = parent_source_id

    def analyze_corroboration(self, observations: Sequence[Observation]) -> CorroborationAnalysis:
        """Analyze a collection of observations for true independent corroboration."""
        if not observations:
            return CorroborationAnalysis(
                total_observations=0,
                raw_source_count=0,
                independent_source_count=0,
                independence_ratio=0.0,
                corroboration_score=0.0,
                primary_sources=(),
                secondary_sources=(),
                source_clusters={},
            )

        clusters: dict[str, list[str]] = {}
        primary: set[str] = set()
        secondary: set[str] = set()

        for obs in observations:
            src = obs.source_id
            headline = (obs.headline or "").lower()
            text = str(obs.structured_payload).lower()

            # Determine cluster root
            if src in self._dependencies:
                root = self._dependencies[src]
                secondary.add(src)
            elif self.CITATION_PATTERNS.search(headline) or self.CITATION_PATTERNS.search(text):
                # Article quotes an official agency -> cluster with that agency
                if "police" in headline or "spd" in headline:
                    root = "spd.cad_911"
                elif "wsdot" in headline:
                    root = "wsdot.highway_alerts"
                elif "sdot" in headline:
                    root = "sdot.row_impacts"
                elif "fire" in headline:
                    root = "sfd.dispatch_911"
                else:
                    root = src
                secondary.add(src)
            elif obs.provenance.authority in {Authority.OFFICIAL, Authority.SEMI_OFFICIAL}:
                root = src
                primary.add(src)
            else:
                root = src
                primary.add(src)

            clusters.setdefault(root, []).append(obs.observation_id)

        independent_count = len(clusters)
        raw_sources = len({o.source_id for o in observations})
        total = len(observations)

        # Corroboration score: diminishing returns on clustered observations
        # Each independent cluster contributes up to 1.0, with diminishing returns
        score = 0.0
        for cluster_root, obs_ids in clusters.items():
            cluster_size = len(obs_ids)
            # First observation in cluster gets 1.0 weight; additional ones get discounted
            cluster_contrib = 1.0 + 0.2 * (cluster_size - 1) ** 0.5
            score += cluster_contrib

        # Normalized corroboration score in [0.0, 1.0]
        max_possible = max(1.0, float(total))
        normalized_score = min(1.0, round(score / max_possible, 4))
        ratio = round(independent_count / max(1, raw_sources), 4)

        return CorroborationAnalysis(
            total_observations=total,
            raw_source_count=raw_sources,
            independent_source_count=independent_count,
            independence_ratio=ratio,
            corroboration_score=normalized_score,
            primary_sources=tuple(sorted(primary)),
            secondary_sources=tuple(sorted(secondary)),
            source_clusters=clusters,
        )


__all__ = ["CorroborationAnalysis", "SourceDependencyGraph"]

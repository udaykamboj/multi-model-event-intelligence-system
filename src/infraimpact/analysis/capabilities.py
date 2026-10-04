"""Analysis capability registry (brief section 17).

Every analytical capability follows one interface and declares what it needs,
what it supports, and what it costs. The relevance engine selects from this
registry per analysis; nothing hardcodes a sequence of steps.

    capability_id
    input_requirements
    output_schema
    supported_event_types
    supported_geography
    estimated_latency
    estimated_cost
    freshness_requirements
    model_version
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..domain.enums import CapabilityTier, InfrastructureDomain, ObservationType
from ..domain.schemas import (
    AffectedInfrastructure,
    Claim,
    EventState,
    FeatureValue,
    Forecast,
    ModelOutput,
    Observation,
    SourceHealth,
    StateDelta,
)
from ..graph.model import InfrastructureGraph
from ..storage.repository import PlatformRepository

log = logging.getLogger(__name__)


@dataclass
class CapabilityContext:
    """Everything a capability may read. Passed explicitly, never imported."""

    region_id: str
    state: EventState
    observations: Sequence[Observation]
    claims: Sequence[Claim]
    delta: Sequence[StateDelta] = ()
    features: dict[str, FeatureValue] = field(default_factory=dict)
    graph: InfrastructureGraph | None = None
    source_health: Sequence[SourceHealth] = ()
    trigger: str = "unknown"
    repository: PlatformRepository | None = None

    @property
    def observation_types(self) -> set[ObservationType]:
        return {o.observation_type for o in self.observations}

    @property
    def affected_domains(self) -> set[InfrastructureDomain]:
        return {i.domain for i in self.state.affected_infrastructure}

    def feature(self, name: str, default: Any = None) -> Any:
        fv = self.features.get(name)
        return fv.value if fv is not None else default

    def evidence_ids(self, limit: int = 12) -> tuple[str, ...]:
        """Observation ids backing the current state, for traceability."""
        return tuple(o.observation_id for o in self.observations[-limit:])


@dataclass
class CapabilityResult:
    features: dict[str, FeatureValue] = field(default_factory=dict)
    impacts: list[AffectedInfrastructure] = field(default_factory=list)
    forecasts: list[Forecast] = field(default_factory=list)
    model_outputs: list[ModelOutput] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def merge(self, other: CapabilityResult) -> CapabilityResult:
        self.features.update(other.features)
        self.impacts.extend(other.impacts)
        self.forecasts.extend(other.forecasts)
        self.model_outputs.extend(other.model_outputs)
        self.notes.extend(other.notes)
        return self


class AnalysisCapability(ABC):
    """One analytical capability."""

    capability_id: str = ""
    tier: CapabilityTier = CapabilityTier.TRIGGERED
    description: str = ""
    #: Observation types that make this capability meaningful.
    triggered_by: frozenset[ObservationType] = frozenset()
    #: Domains this capability speaks about.
    domains: frozenset[InfrastructureDomain] = frozenset()
    estimated_latency_ms: int = 50
    estimated_cost: float = 0.1
    model_version: str = "deterministic-0.1.0"

    def is_applicable(self, ctx: CapabilityContext) -> tuple[bool, str]:
        """Cheap precondition check. Reason is recorded either way."""
        if self.triggered_by and not (self.triggered_by & ctx.observation_types):
            return False, "no triggering observation type present"
        if self.domains and not (self.domains & ctx.affected_domains) and not self.triggered_by:
            return False, "no affected infrastructure domain"
        if self.tier != CapabilityTier.CHEAP:
            degraded = self._degraded_sources(ctx)
            if degraded and not self._tolerates_degradation():
                return False, f"required sources degraded: {sorted(degraded)}"
        return True, "applicable"

    def _degraded_sources(self, ctx: CapabilityContext) -> set[str]:
        return {h.source_id for h in ctx.source_health if h.state.value in {"degraded", "offline"}}

    def _tolerates_degradation(self) -> bool:
        return False

    @abstractmethod
    def run(self, ctx: CapabilityContext) -> CapabilityResult: ...

    def describe(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "tier": str(self.tier),
            "description": self.description,
            "triggered_by": sorted(str(t) for t in self.triggered_by),
            "domains": sorted(str(d) for d in self.domains),
            "estimated_latency_ms": self.estimated_latency_ms,
            "estimated_cost": self.estimated_cost,
            "model_version": self.model_version,
        }


class CapabilityRegistry:
    """Holds every capability the deployment can run."""

    def __init__(self) -> None:
        self._capabilities: dict[str, AnalysisCapability] = {}

    def register(self, capability: AnalysisCapability) -> AnalysisCapability:
        if not capability.capability_id:
            raise ValueError("capability_id is required")
        self._capabilities[capability.capability_id] = capability
        return capability

    def get(self, capability_id: str) -> AnalysisCapability | None:
        return self._capabilities.get(capability_id)

    def all(self) -> list[AnalysisCapability]:
        return list(self._capabilities.values())

    def ids(self) -> list[str]:
        return sorted(self._capabilities)

    def by_tier(self, tier: CapabilityTier) -> list[AnalysisCapability]:
        return [c for c in self._capabilities.values() if c.tier == tier]

    def describe(self) -> list[dict[str, Any]]:
        return [c.describe() for c in self.all()]

    def __len__(self) -> int:
        return len(self._capabilities)

    def __iter__(self) -> Iterable[AnalysisCapability]:
        return iter(self._capabilities.values())


def build_default_registry(graph: InfrastructureGraph | None = None) -> CapabilityRegistry:
    """Assemble the full capability set.

    Adding earthquake support means adding capabilities here and adding data
    sources - not creating another application workflow (section 17).
    """
    from .geospatial import (
        CriticalFacilityExposureCapability,
        RoadOverlapCapability,
        TransitOverlapCapability,
        UserRouteExposureCapability,
    )
    from .graph_engine import PropagationCapability
    from .prediction import (
        EventClassificationCapability,
        InfrastructureImpactCapability,
        TimeToImpactCapability,
        TrafficAnomalyCapability,
    )
    from .uncertainty import SourceConflictCapability

    registry = CapabilityRegistry()
    for capability in (
        EventClassificationCapability(),
        SourceConflictCapability(),
        RoadOverlapCapability(graph),
        TransitOverlapCapability(graph),
        CriticalFacilityExposureCapability(graph),
        PropagationCapability(graph),
        TrafficAnomalyCapability(),
        InfrastructureImpactCapability(),
        TimeToImpactCapability(),
        UserRouteExposureCapability(graph),
    ):
        registry.register(capability)
    return registry


__all__ = [
    "AnalysisCapability",
    "CapabilityContext",
    "CapabilityRegistry",
    "CapabilityResult",
    "build_default_registry",
    "Callable",
]
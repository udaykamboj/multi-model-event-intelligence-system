"""Interpretation and orchestration (brief sections 18-20).

What this module is for
-----------------------
Section 18 gives the language model seven jobs. Section 20 forbids it five
things. Sections 43 and 51 require that every use be recorded and that no model
internals reach a client. All three are easier to satisfy in one place than in
the six call sites that need interpretation, so they live here.

    extract_claims            -> :class:`infraimpact.events.claims.LlmClaimExtractor`
    resolve_event             -> :meth:`InterpretationLayer.opinion_on_resolution`
    select_capabilities       -> :meth:`InterpretationLayer.recommend_capabilities`
    hypothesize_infrastructure-> :meth:`InterpretationLayer.hypotheses`
    synthesize_evidence       -> :meth:`InterpretationLayer.narrate_evidence`
    explain_to_user           -> :meth:`InterpretationLayer.explain_to_user`

The rule that keeps all of them honest
--------------------------------------
The LLM reads. It never writes a fact.

Every value it produces is INFERRED at best, none of it can create or alter an
observation, a claim, an ``AffectedInfrastructure`` entry or a ``Forecast``, and
none of it can outrank an official instruction. Capability recommendations may
only *promote* a capability the deterministic relevance engine already ranked;
they can never introduce one, because a model cannot know what the registry
contains better than the registry does. That asymmetry is the whole safety
argument, and :meth:`InterpretationLayer.recommend_capabilities` implements it by
intersecting against the relevance engine's own list.

Why the calls are conditional
-----------------------------
The loop fans out over events and then over users. Calling the LLM for every
event on every cycle, and again for every user on every event, would be slow and
expensive for no gain - the narrative for one event is identical across its
users. So:

* a narrative is produced **once per event rebuild**, not per user, and cached on
  the state version it described;
* a per-user explanation is produced only above an exposure threshold, and only
  when something actually changed;
* an event with fewer than :data:`MIN_OBSERVATIONS_FOR_NARRATIVE` observations, or
  a single-source one, gets no narrative at all - the deterministic summary
  already says everything there is to say, and inventing prose over it would add
  words without adding information.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..delta.engine import DeltaReport
from ..domain.schemas import (
    AffectedInfrastructure,
    EventState,
    EvidenceNarrative,
    Forecast,
    Observation,
    UserExposure,
)
from .client import LlmClient, NullLlmClient, llm_operations
from .big_pickle import MIN_OBSERVATIONS_FOR_NARRATIVE, library_tier

log = logging.getLogger(__name__)

#: Below this exposure score an explanation is not worth a completion call. The
#: exposure floor is set deliberately high: the platform already declines to
#: interrupt anyone below :data:`~infraimpact.users.presentation.INTERRUPT_FLOOR`,
#: so writing an explanation for them would be prose that is never shown.
MIN_EXPOSURE_FOR_EXPLANATION = 0.35

#: An explanation below this ``user_delta`` magnitude is not worth a call either -
#: "nothing has changed" does not need four paragraphs.
MIN_DELTA_FOR_EXPLANATION = 0.2

#: Cap on observations handed to the LLM. A single prompt with four hundred
#: observations is slow, expensive, and past a certain point the model stops
#: reading; the deterministic evidence vector carries the rest.
MAX_OBSERVATIONS_IN_PROMPT = 40


@dataclass(frozen=True)
class UserExplanation:
    """The section 18 user-communication payload, post-prohibition-check."""

    headline: str
    what_changed: str
    why_it_matters: str
    evidence: str
    uncertainty: str
    suggested_posture: str = "monitor"
    #: Reproduced verbatim from official guidance, never paraphrased (section 38).
    official_guidance_reference: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "what_changed": self.what_changed,
            "why_it_matters": self.why_it_matters,
            "evidence": self.evidence,
            "uncertainty": self.uncertainty,
            "suggested_posture": self.suggested_posture,
            "official_guidance_reference": self.official_guidance_reference,
            "is_llm_generated": True,
        }


@dataclass
class InterpretationLayer:
    """Every section-18 LLM call the platform makes, in one object.

    Held by the runtime and the orchestrator. Constructed with no client it is
    inert but still honest: every method returns ``None`` and the run record says
    the interpretation layer was not configured.
    """

    client: LlmClient | None = None
    enabled: bool = True

    # -- capability ---------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return self.client is not None and not isinstance(self.client, NullLlmClient)

    @property
    def operations(self) -> tuple[str, ...]:
        return llm_operations(self.client)

    def describe(self) -> dict[str, Any]:
        client = self.client
        detail = client.describe() if hasattr(client, "describe") else {}
        return {
            "configured": self.is_configured,
            "enabled": self.enabled,
            "client": type(client).__name__ if client else None,
            "live": bool(getattr(client, "is_live", False)),
            **detail,
        }

    def _usable(self) -> bool:
        return self.enabled and self.is_configured

    # -- 1. evidence synthesis ---------------------------------------------

    def narrate_evidence(
        self,
        state: EventState,
        observations: Sequence[Observation],
    ) -> EvidenceNarrative | None:
        """Section 18 evidence synthesis, once per event rebuild.

        Returns ``None`` - never an empty narrative - when the evidence does not
        support one. That is the common case for a single official closure, and
        a confident-sounding paragraph over one data point is worse than nothing.
        """

        if not self._usable():
            return None

        evidence = state.evidence
        if len(observations) < MIN_OBSERVATIONS_FOR_NARRATIVE:
            return None
        if evidence.independent_source_count < 2:
            # One source restating itself is not corroboration, and a narrative
            # over it would imply confidence the evidence does not have.
            return None

        facts = {
            "event_id": state.event_id,
            "status": state.status,
            "state_version": state.state_version,
            "first_observed": _iso(state.first_observed),
            "last_observed": _iso(state.last_observed),
            "movement": state.movement.model_dump(mode="json"),
            "geometry_confidence": round(state.geometry_confidence, 3),
            "evidence": {
                "source_count": evidence.source_count,
                "independent_source_count": evidence.independent_source_count,
                "authorities": evidence.authorities,
                "observation_types": evidence.observation_types,
                "contradictions": evidence.contradictions,
                "freshness_seconds": evidence.freshness_seconds,
                "quality_vector": evidence.vector,
            },
            "affected_infrastructure": [
                {
                    "domain": i.domain.value,
                    "identifier": i.identifier,
                    "name": i.name,
                    "severity": i.severity.value,
                    "truth_status": i.truth_status.value,
                }
                for i in state.affected_infrastructure[:15]
            ],
            "observations": [_observation_brief(o) for o in _recent(observations)],
        }

        result = self.client.synthesize_evidence(facts)  # type: ignore[union-attr]
        summary = (result or {}).get("summary")
        if not summary:
            return None

        return EvidenceNarrative(
            source="llm",
            summary=summary,
            confirmed_facts=tuple(result.get("confirmed_facts", ())),
            reported_claims=tuple(result.get("reported_claims", ())),
            inferred_points=tuple(result.get("inferred_points", ())),
            sources=tuple(result.get("sources", ())),
            confidence_score=float(result.get("confidence_score", 0.0)),
            operations=self.operations,
        )

    # -- 2. user communication ---------------------------------------------

    def explain_to_user(
        self,
        *,
        state: EventState,
        exposure: UserExposure,
        impacts: Sequence[AffectedInfrastructure] = (),
        forecasts: Sequence[Forecast] = (),
        delta_report: DeltaReport | None = None,
        observations: Sequence[Observation] = (),
    ) -> UserExplanation | None:
        """Section 18 user communication: what/why/evidence/uncertainty.

        Conditional on the exposure actually being worth a user's attention. The
        four questions are answered from measured values the caller supplies, so
        the model is organising known facts rather than reasoning about them.
        """

        if not self._usable():
            return None
        if exposure.exposure_score < MIN_EXPOSURE_FOR_EXPLANATION:
            return None
        if not (delta_report and delta_report.deltas):
            # Nothing changed since the last update, so "what changed" has no
            # honest answer and inventing one is exactly the failure section 20
            # prohibition 4 exists to prevent.
            return None
        top_delta = max(delta_report.deltas, key=lambda d: d.magnitude)
        if top_delta.magnitude < MIN_DELTA_FOR_EXPLANATION:
            return None

        official = [
            o.headline
            for o in observations
            if o.observation_type.value == "official_emergency_notice" and o.headline
        ]

        result = self.client.explain_to_user(  # type: ignore[union-attr]
            {
                "event": {
                    "event_id": state.event_id,
                    "status": state.status,
                    "event_types": state.event_type_distribution,
                    "movement": state.movement.model_dump(mode="json"),
                },
                "what_changed": {
                    "change": top_delta.change,
                    "before": top_delta.before,
                    "after": top_delta.after,
                    "magnitude": round(top_delta.magnitude, 3),
                    "confidence": round(top_delta.confidence, 3),
                    "causes": list(top_delta.causes),
                },
                "your_exposure": {
                    "level": exposure.exposure_level.value,
                    "score": round(exposure.exposure_score, 3),
                    "distance_m": (
                        round(exposure.distance_m, 1)
                        if exposure.distance_m is not None
                        else None
                    ),
                    "affected_places": list(getattr(exposure, "affected_places", ())),
                    "affected_routes": list(getattr(exposure, "affected_routes", ())),
                },
                "infrastructure": [
                    {
                        "domain": i.domain.value,
                        "identifier": i.identifier,
                        "severity": i.severity.value,
                        "truth_status": i.truth_status.value,
                    }
                    for i in impacts[:10]
                ],
                "forecasts": [
                    {
                        "domain": f.domain.value,
                        "probability": round(f.probability, 3),
                        "horizon_minutes": f.horizon_minutes,
                        "target": f.target,
                    }
                    for f in forecasts[:5]
                ],
                "evidence_quality": {
                    "source_count": state.evidence.source_count,
                    "independent_source_count": state.evidence.independent_source_count,
                    "contradictions": state.evidence.contradictions,
                },
                "official_guidance": official[:3],
                "instruction": (
                    "Explain only from these facts. State uncertainty plainly. "
                    "Never issue an evacuation directive, never invent a "
                    "destination, never assert a road closure, never characterise "
                    "any group. Reproduce official guidance verbatim."
                ),
            }
        )
        result = result or {}
        headline = result.get("headline")
        what_changed = result.get("what_changed")
        if not headline or not what_changed:
            return None

        return UserExplanation(
            headline=headline,
            what_changed=what_changed,
            why_it_matters=result.get("why_it_matters") or "",
            evidence=result.get("evidence") or "",
            uncertainty=result.get("uncertainty") or "",
            suggested_posture=result.get("suggested_posture", "monitor"),
            # Section 38 says official wording is reproduced *verbatim*. The
            # model's own ``official_guidance_reference`` is discarded
            # unconditionally, even when it looks perfect, because "looks like the
            # notice" and "is the notice" are different claims and only one of
            # them can be checked. The platform has the real text in the ledger;
            # that is the only version it can vouch for.
            #
            # When the ledger has no official notice there is nothing to
            # reproduce, so the field is None rather than the model's guess. A
            # citation to guidance the platform cannot see is a citation it
            # cannot stand behind.
            official_guidance_reference=official[0] if official else None,
        )

    # -- 3. dynamic orchestration ------------------------------------------

    def recommend_capabilities(
        self,
        *,
        state: EventState,
        delta_report: DeltaReport,
        candidates: Sequence[Any],
    ) -> list[str]:
        """Section 18 dynamic orchestration - advisory, and bounded.

        Only *promotion*. ``candidates`` is what the deterministic relevance
        engine already selected; the intersection below means the LLM can raise
        a capability's priority but can never introduce one. A model asked to
        pick capabilities would otherwise happily name a plausible-sounding
        analysis that does not exist, and the orchestrator would either crash
        trying to run it or, worse, silently ignore it and report success.

        Returns capability ids, strongest first.
        """

        if not self._usable() or not candidates:
            return []

        registry = [
            {
                "capability_id": _capability_id(c),
                "capability": _capability_name(c),
                "domain": _capability_domain(c),
                "description": _capability_description(c),
                # Deliberately the *library's* vocabulary, not ours. The client
                # adapter translates either way; sending ours would work until a
                # model echoed ``triggered`` back and the library's response
                # schema rejected the whole reply.
                "tier": library_tier(_capability_tier(c)) or "medium",
                "current_relevance": round(_capability_relevance(c), 4),
            }
            for c in candidates
        ]

        result = self.client.select_capabilities(  # type: ignore[union-attr]
            {
                "event_id": state.event_id,
                "status": state.status,
                "state_version": state.state_version,
                "movement": state.movement.model_dump(mode="json"),
                "evidence": state.evidence.vector,
                "affected_domains": sorted(
                    {i.domain.value for i in state.affected_infrastructure}
                ),
            },
            {
                "magnitude": round(delta_report.magnitude, 4),
                "material": delta_report.is_material,
                "deltas": [
                    {
                        "change": d.change,
                        "before": d.before,
                        "after": d.after,
                        "magnitude": round(d.magnitude, 4),
                    }
                    for d in delta_report.deltas[:10]
                ],
            },
            registry,
        )

        known = {entry["capability_id"] for entry in registry}
        out: list[str] = []
        for rec in (result or {}).get("recommended_capabilities", []) or []:
            name = rec.get("capability")
            if name in known and name not in out:
                out.append(name)
        if out:
            log.info("llm promoted %d/%d capabilities for %s", len(out), len(known), state.event_id)
        return out

    # -- 4. hypothesis generation ------------------------------------------

    def hypotheses(
        self, state: EventState, impacts: Sequence[AffectedInfrastructure]
    ) -> tuple[dict[str, Any], ...]:
        """Section 18 hypothesis generation.

        Returns questions to examine, stored on the run and never surfaced to
        ``/v1``. Filtering happens in :class:`BigPickleClient`; this only turns
        the result into the tuple the run record stores.
        """

        if not self._usable() or not state.affected_infrastructure and not impacts:
            return ()
        result = self.client.hypothesize_infrastructure(  # type: ignore[union-attr]
            {
                "event_id": state.event_id,
                "status": state.status,
                "movement": state.movement.model_dump(mode="json"),
                "affected_infrastructure": [
                    {"domain": i.domain.value, "identifier": i.identifier, "name": i.name}
                    for i in (impacts or state.affected_infrastructure)[:15]
                ],
                "question": (
                    "Which infrastructure relationships have not been examined yet "
                    "and would change the impact assessment if they were?"
                ),
            }
        )
        return tuple((result or {}).get("hypotheses", ()))

    # -- 5. event resolution ------------------------------------------------

    def opinion_on_resolution(
        self, candidate: dict[str, Any], options: Iterable[dict[str, Any]]
    ) -> dict[str, Any]:
        """Section 18 semantic event resolution, as a second opinion.

        Never decisive. The deterministic resolver in
        :mod:`infraimpact.events.resolver` owns the merge decision; this is asked
        only when that resolver lands in its ambiguous band, where a human
        reading the same two reports would beat a score. The opinion is recorded
        on the resolution and never applied directly.
        """

        if not self._usable():
            return {}
        return self.client.resolve_event(candidate, list(options)) or {}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _recent(observations: Sequence[Observation]) -> list[Observation]:
    """Newest observations first, capped. Recency is what a narrative needs."""

    return sorted(observations, key=lambda o: o.observed_at, reverse=True)[
        :MAX_OBSERVATIONS_IN_PROMPT
    ]


def _observation_brief(observation: Observation) -> dict[str, Any]:
    """What a model is allowed to read. Provenance and timestamps, not payloads.

    The raw payload is deliberately excluded: it is frequently megabytes of
    agency-internal JSON, it would blow the context window, and none of it is
    prose. ``headline`` plus the type plus the authority is the interpretable
    part; the structured facts are already in ``affected_infrastructure`` and
    the claims the deterministic layer extracted.
    """

    return {
        "observation_id": observation.observation_id,
        "type": observation.observation_type.value,
        "headline": observation.headline,
        "authority": observation.provenance.authority.value,
        "source_id": observation.source_id,
        "event_time": _iso(observation.event_time),
        "observed_at": _iso(observation.observed_at),
        "has_geometry": observation.geometry is not None,
        "spatial_precision_m": observation.quality.spatial_precision,
    }


def _capability_id(capability: Any) -> str:
    return str(getattr(capability, "capability_id", "") or getattr(capability, "capability", ""))


def _capability_name(capability: Any) -> str:
    return str(getattr(capability, "capability", _capability_id(capability)))


def _capability_domain(capability: Any) -> str | None:
    domain = getattr(capability, "domain", None)
    return getattr(domain, "value", None)


def _capability_description(capability: Any) -> str:
    return str(getattr(capability, "description", "") or "")[:400]


def _capability_tier(capability: Any) -> str | None:
    tier = getattr(capability, "tier", None)
    return getattr(tier, "value", None)


def _capability_relevance(capability: Any) -> float:
    if hasattr(capability, "relevance"):
        return float(capability.relevance or 0.0)
    inner = getattr(capability, "capability", None)
    return float(getattr(capability, "score", 0.0) or getattr(inner, "relevance", 0.0) or 0.0)


__all__ = [
    "MAX_OBSERVATIONS_IN_PROMPT",
    "MIN_DELTA_FOR_EXPLANATION",
    "MIN_EXPOSURE_FOR_EXPLANATION",
    "MIN_OBSERVATIONS_FOR_NARRATIVE",
    "InterpretationLayer",
    "UserExplanation",
]
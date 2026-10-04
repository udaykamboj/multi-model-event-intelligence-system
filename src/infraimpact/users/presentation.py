"""Dynamic presentation contract (brief sections 37, 38).

The backend ranks; the renderer decides.

The rule this module enforces is narrow and absolute: the backend never emits
a screen instruction. Not ``show_level_2_screen``, not ``navigate_to_alert``,
not a "display_level" field. It emits ranked objects carrying facts,
confidence and evidence, and the UI decides what to put on screen. That is
what lets one renderer display a protest, an earthquake and a utility failure
without the backend knowing anything about screens.

Section 38 adds one ordering constraint on top: official emergency guidance
takes the highest presentation priority, and its wording is reproduced
exactly. Predictive analysis may sit alongside official guidance; it may never
displace, soften or paraphrase it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..delta.engine import DeltaReport, has_official_guidance
from ..llm.interpreter import UserExplanation
from ..domain.enums import (
    InfrastructureDomain,
    ObservationType,
    PresentationType,
    TruthStatus,
    Urgency,
)
from ..domain.schemas import (
    AffectedInfrastructure,
    EventState,
    Forecast,
    Observation,
    PresentationItem,
    UserExposure,
    UserPriority,
)
from .exposure import EXPOSURE_RADIUS_M

log = logging.getLogger(__name__)

#: Section 38: nothing outranks this.
OFFICIAL_PRIORITY = 1.0

#: Below this a user is not shown anything that would interrupt them.
INTERRUPT_FLOOR = 0.35

#: Section 37, made checkable. A backend that starts emitting these tokens has
#: become a UI controller, which is exactly the coupling the section forbids.
#: The check runs on every emitted item.
_SCREEN_INSTRUCTION_TOKENS = (
    "show_",
    "hide_",
    "open_screen",
    "navigate_to",
    "display_level",
    "screen_level",
    "go_to_screen",
    "push_screen",
    "render_banner",
    "open_modal",
    "ui_instruction",
)

_SCREEN_INSTRUCTION_RE = re.compile(
    r"\b(" + "|".join(_SCREEN_INSTRUCTION_TOKENS) + r")[a-z0-9_]*\b", re.IGNORECASE
)


class ScreenInstructionError(ValueError):
    """Raised when presentation content contains a UI directive."""


@dataclass
class PresentationContext:
    """Everything presentation may read."""

    exposure: UserExposure
    priority: UserPriority
    state: EventState
    impacts: Sequence[AffectedInfrastructure] = ()
    forecasts: Sequence[Forecast] = ()
    observations: Sequence[Observation] = ()
    delta_report: DeltaReport | None = None
    is_new_event: bool = False
    #: Section 18 user communication, when the interpretation layer produced one.
    #: Optional and additive: every deterministic item below is built identically
    #: whether or not this is present, so a missing explanation degrades the
    #: presentation rather than changing it.
    explanation: UserExplanation | None = None


@dataclass
class PresentationPayload:
    """The section 37 response body."""

    event_id: str
    items: list[PresentationItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "presentation_items": [
                item.model_dump(mode="json") for item in self.items
            ],
        }

    def of_type(self, type_: PresentationType) -> list[PresentationItem]:
        return [i for i in self.items if i.type is type_]


class PresentationEngine:
    """Builds the ranked presentation list for one user and one event."""

    def build(self, ctx: PresentationContext) -> PresentationPayload:
        items: list[PresentationItem] = []
        priority = ctx.priority.priority

        # Official guidance first, always (section 38).
        if has_official_guidance(ctx.state):
            item = self._official_guidance(ctx)
            if item is not None:
                items.append(item)

        if priority > 0.0:
            items.extend(self._route_items(ctx))
            items.extend(self._exposure_item(ctx))
            items.extend(self._explanation_item(ctx))
            items.extend(self._impact_items(ctx))
            items.extend(self._change_items(ctx))
            items.extend(self._forecast_items(ctx))
            items.append(self._evidence_item(ctx))
            if items:
                items.extend(self._uncertainty_items(ctx))

        if not self._has_content(items):
            # Nothing worth interrupting for. Saying so is itself a ranked
            # object - the renderer can then stay quiet without the backend
            # inventing urgency to fill the space.
            items.append(self._quiet_item(ctx))

        ranked = self._rank(items)
        for item in ranked:
            self._assert_no_screen_instructions(item)
        return PresentationPayload(event_id=ctx.state.event_id, items=ranked)

    # -- ranking ----------------------------------------------------------

    @staticmethod
    def _rank(items: Sequence[PresentationItem]) -> list[PresentationItem]:
        """Sort by priority, then by a stable type order.

        The type order is a deterministic tiebreak, not a layout decision:
        official guidance outranks route disruption, which outranks exposure,
        and so on, so two clients rendering the same payload agree.
        """
        return sorted(items, key=lambda i: (-i.priority, _TYPE_ORDER.get(i.type, 99)))

    # -- builders ---------------------------------------------------------

    def _official_guidance(
        self, ctx: PresentationContext
    ) -> PresentationItem | None:
        """Verbatim official wording. Never paraphrased, never summarised."""
        official = [
            o
            for o in ctx.observations
            if o.observation_type is ObservationType.OFFICIAL_EMERGENCY_NOTICE
        ]
        if not official:
            return None
        # Most recent official notice is the operative one.
        notice = max(official, key=lambda o: o.event_time)
        return PresentationItem(
            type=PresentationType.OFFICIAL_GUIDANCE,
            priority=OFFICIAL_PRIORITY,
            # Exact source meaning preserved (section 38).
            headline=notice.headline,
            detail=notice.structured_payload.get("instruction") or None,
            truth_status=TruthStatus.CONFIRMED,
            evidence_ids=(notice.observation_id,),
            confidence=1.0,
            payload={
                "source_id": notice.source_id,
                "source_url": notice.source_url,
                "event_time": notice.event_time.isoformat(),
                "verbatim": True,
                "may_not_be_contradicted_by_prediction": True,
            },
        )

    def _route_items(self, ctx: PresentationContext) -> list[PresentationItem]:
        items: list[PresentationItem] = []
        for route in ctx.exposure.route_impacts:
            if not route.intersects:
                continue
            delay = (
                f" About {route.delay_estimate_min:.0f} min added."
                if route.delay_estimate_min
                else ""
            )
            items.append(
                PresentationItem(
                    type=PresentationType.ROUTE_DISRUPTION,
                    priority=round(min(0.99, 0.7 + 0.3 * ctx.priority.priority), 4),
                    headline=f"Your usual route is affected: {route.route_name}",
                    detail=(
                        f"{len(route.blocked_node_ids)} segment(s) affected.{delay}"
                    ),
                    truth_status=TruthStatus.CONFIRMED,
                    evidence_ids=route.evidence_ids,
                    confidence=round(ctx.priority.evidence_confidence, 4),
                    payload={
                        "route_id": route.route_id,
                        "blocked_node_ids": list(route.blocked_node_ids),
                        "delay_estimate_min": route.delay_estimate_min,
                    },
                )
            )
            # An alternative is a separate object with its own priority: the
            # renderer may show it inline or not at all, and a client with no
            # map can still render the disruption.
            if route.alternative_available:
                items.append(
                    PresentationItem(
                        type=PresentationType.ALTERNATIVE_ROUTE,
                        priority=round(min(0.9, 0.6 + 0.3 * ctx.priority.priority), 4),
                        headline=f"An alternative is available for {route.route_name}",
                        truth_status=TruthStatus.CONFIRMED,
                        evidence_ids=route.evidence_ids,
                        confidence=round(ctx.priority.evidence_confidence, 4),
                        payload={
                            "route_id": route.route_id,
                            "alternative_geometry": route.alternative_geometry,
                        },
                    )
                )
        return items

    def _exposure_item(self, ctx: PresentationContext) -> list[PresentationItem]:
        exposure = ctx.exposure
        if exposure.exposure_level is Urgency.NONE:
            return []
        if exposure.distance_m is None and not exposure.inside_impact_area:
            return []
        where = (
            "You are inside the affected area."
            if exposure.inside_impact_area
            else f"You are about {exposure.distance_m:,.0f} m from the affected area."
        )
        return [
            PresentationItem(
                type=PresentationType.USER_EXPOSURE,
                priority=round(min(0.95, exposure.exposure_score), 4),
                headline=where,
                detail=self._saved_place_detail(ctx),
                truth_status=TruthStatus.INFERRED,
                confidence=round(exposure.confidence, 4),
                payload={
                    "exposure_level": exposure.exposure_level.value,
                    "distance_m": exposure.distance_m,
                    "inside_impact_area": exposure.inside_impact_area,
                    "saved_place_impacts": dict(exposure.saved_place_impacts),
                },
            )
        ]

    @staticmethod
    def _saved_place_detail(ctx: PresentationContext) -> str | None:
        hits = sorted(
            ctx.exposure.saved_place_impacts.items(), key=lambda kv: -kv[1]
        )
        if not hits:
            return None
        worst_id, worst = hits[0]
        label = worst_id.replace("_", " ")
        return f"Your {label} is the most affected saved place (exposure {worst:.2f})."

    def _impact_items(self, ctx: PresentationContext) -> list[PresentationItem]:
        """Impacts that plausibly touch this user.

        Relevance is a personal question: the same closure matters to a user
        whose route crosses it and not at all to a user three neighbourhoods
        away. Two things qualify - an impact already blocking one of the
        user's routes, or any impact anywhere in the footprint once the user
        is inside the exposure radius of it.
        """
        on_my_routes = {
            node
            for route in ctx.exposure.route_impacts
            for node in route.blocked_node_ids
        }
        near_me = (
            ctx.exposure.distance_m is not None
            and ctx.exposure.distance_m <= EXPOSURE_RADIUS_M
        )

        relevant = [
            i
            for i in ctx.impacts
            if i.geometry and (i.identifier in on_my_routes or near_me)
        ]
        relevant.sort(key=lambda i: (-_impact_score(i), i.identifier))
        items: list[PresentationItem] = []
        for impact in relevant[:5]:
            items.append(
                PresentationItem(
                    type=PresentationType.INFRASTRUCTURE_IMPACT,
                    priority=round(min(0.9, 0.35 + 0.5 * _impact_score(impact)), 4),
                    headline=_impact_headline(impact),
                    truth_status=impact.truth_status,
                    evidence_ids=impact.observation_ids,
                    confidence=round(_impact_confidence(impact), 4),
                    payload={
                        "domain": impact.domain.value,
                        "identifier": impact.identifier,
                        "severity": impact.severity.value,
                        "geometry": impact.geometry,
                    },
                )
            )
        return items

    def _change_items(self, ctx: PresentationContext) -> list[PresentationItem]:
        """Section 54: say what changed, do not re-explain the event."""
        report = ctx.delta_report
        if report is None or not report.deltas:
            return []
        top = max(report.deltas, key=lambda d: d.magnitude)
        return [
            PresentationItem(
                type=PresentationType.WHAT_CHANGED,
                priority=round(min(0.85, 0.4 + 0.45 * top.magnitude), 4),
                headline=_change_headline(top.change),
                detail=top.causes[0] if top.causes else None,
                truth_status=TruthStatus.INFERRED,
                evidence_ids=(),
                confidence=round(top.confidence, 4),
                payload={
                    "change": top.change,
                    "before": top.before,
                    "after": top.after,
                    "magnitude": top.magnitude,
                    "causes": list(top.causes),
                    "official_guidance": top.official_guidance,
                    "all_changes": [d.change for d in report.deltas],
                },
            )
        ]

    def _forecast_items(self, ctx: PresentationContext) -> list[PresentationItem]:
        """Forward-looking risk, always tagged as prediction.

        Never emitted above the user's actual exposure: a forecast is not an
        observation and must not outrank what has already happened to them.
        """
        if not ctx.forecasts:
            return []
        peak = max(ctx.forecasts, key=lambda f: f.probability)
        if peak.probability < 0.4:
            return []
        return [
            PresentationItem(
                type=PresentationType.EVENT_SUMMARY,
                priority=round(
                    min(ctx.exposure.exposure_score, 0.5 * peak.probability), 4
                ),
                headline=(
                    f"Predicted {peak.domain.value.replace('_', ' ')} impact"
                    f" in about {peak.horizon_minutes} min"
                ),
                detail=peak.target or None,
                truth_status=TruthStatus.PREDICTED,
                confidence=round(_forecast_confidence(peak), 4),
                payload={
                    "is_prediction": True,
                    "domain": peak.domain.value,
                    "target": peak.target,
                    "probability": peak.probability,
                    "interval": [peak.lower, peak.upper],
                    "horizon_minutes": peak.horizon_minutes,
                    "model_id": peak.model_id,
                },
            )
        ]

    def _explanation_item(self, ctx: PresentationContext) -> list[PresentationItem]:
        """Section 18's four questions, as one ranked object.

        The renderer decides how to lay this out; the backend only supplies the
        four answers and the facts behind them. Its priority is capped at the
        user's own exposure, and it is always below official guidance, so a
        well-written explanation can never displace an agency instruction or
        outrank what has actually happened to this user.

        ``what_changed`` is repeated in ``payload`` alongside the deterministic
        change text, because a client that wants to diff before and after needs
        the machine-readable version and the prose version side by side.
        """

        explanation = ctx.explanation
        if explanation is None:
            return []

        detail_parts = [explanation.why_it_matters, explanation.evidence]
        detail = " ".join(p for p in detail_parts if p) or None

        payload = explanation.as_payload()
        top_change = None
        if ctx.delta_report and ctx.delta_report.deltas:
            top = max(ctx.delta_report.deltas, key=lambda d: d.magnitude)
            top_change = {"change": top.change, "before": top.before, "after": top.after}

        return [
            PresentationItem(
                type=PresentationType.EVENT_SUMMARY,
                priority=round(min(ctx.exposure.exposure_score, 0.9), 4),
                headline=explanation.headline,
                detail=detail,
                # Prose from a language model is an inference, always - even
                # though every fact it organises may be CONFIRMED. There is no
                # promotion path and this literal is the enforcement point.
                truth_status=TruthStatus.INFERRED,
                confidence=round(ctx.priority.evidence_confidence, 4),
                payload={
                    **payload,
                    "deterministic_change": top_change,
                    "suggested_posture": explanation.suggested_posture,
                },
            )
        ]

    @staticmethod
    def _evidence_item(ctx: PresentationContext) -> PresentationItem:
        evidence = ctx.state.evidence
        return PresentationItem(
            type=PresentationType.EVIDENCE,
            priority=0.2,
            headline=(
                f"Based on {evidence.source_count} report(s) from "
                f"{evidence.independent_source_count} independent source(s)"
            ),
            truth_status=TruthStatus.INFERRED,
            confidence=round(ctx.priority.evidence_confidence, 4),
            payload={
                "source_count": evidence.source_count,
                "independent_source_count": evidence.independent_source_count,
                "authorities": dict(evidence.authorities),
                "observation_types": dict(evidence.observation_types),
                "contradictions": evidence.contradictions,
                "freshness_seconds": evidence.freshness_seconds,
                "quality_vector": dict(evidence.vector),
            },
        )

    def _uncertainty_items(self, ctx: PresentationContext) -> list[PresentationItem]:
        """Disagreement and stale evidence are content, not noise."""
        evidence = ctx.state.evidence
        reasons: list[str] = []
        if evidence.contradictions:
            reasons.append(f"{evidence.contradictions} conflicting report(s)")
        if ctx.state.geometry_confidence < 0.6:
            reasons.append("the affected area is not precisely located")
        if evidence.freshness_seconds and evidence.freshness_seconds > 3600:
            reasons.append("the newest report is over an hour old")
        if not reasons:
            return []
        return [
            PresentationItem(
                type=PresentationType.UNCERTAINTY,
                priority=0.15,
                headline="Some of this is uncertain",
                detail="; ".join(reasons),
                truth_status=TruthStatus.INFERRED,
                confidence=round(1.0 - ctx.priority.evidence_confidence, 4),
                payload={"reasons": reasons},
            )
        ]

    @staticmethod
    def _quiet_item(ctx: PresentationContext) -> PresentationItem:
        return PresentationItem(
            type=PresentationType.QUIET,
            priority=0.0,
            headline="Nothing here affects you right now",
            detail="This event is not close to your places or routes.",
            truth_status=TruthStatus.INFERRED,
            confidence=round(ctx.priority.evidence_confidence, 4),
            payload={"reason": "no_personal_impact"},
        )

    @staticmethod
    def _has_content(items: Sequence[PresentationItem]) -> bool:
        return any(
            i.type is not PresentationType.QUIET and i.priority >= INTERRUPT_FLOOR
            for i in items
        )

    # -- section 37 guard -------------------------------------------------

    @staticmethod
    def _assert_no_screen_instructions(item: PresentationItem) -> None:
        """Fail loudly rather than leak a UI directive to a client."""
        blob = " ".join(
            filter(
                None,
                [
                    item.headline,
                    item.detail,
                    str(item.payload.get("screen")),
                    " ".join(str(k) for k in item.payload),
                ],
            )
        )
        match = _SCREEN_INSTRUCTION_RE.search(blob)
        if match:
            raise ScreenInstructionError(
                f"presentation item {item.type.value!r} contains a screen "
                f"instruction ({match.group(0)!r}); section 37 forbids the "
                "backend from directing the renderer"
            )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_TYPE_ORDER = {
    PresentationType.OFFICIAL_GUIDANCE: 0,
    PresentationType.ROUTE_DISRUPTION: 1,
    PresentationType.ALTERNATIVE_ROUTE: 2,
    PresentationType.USER_EXPOSURE: 3,
    PresentationType.WHAT_CHANGED: 4,
    PresentationType.INFRASTRUCTURE_IMPACT: 5,
    PresentationType.EVENT_SUMMARY: 6,
    PresentationType.UNCERTAINTY: 7,
    PresentationType.EVIDENCE: 8,
    PresentationType.QUIET: 9,
    PresentationType.HISTORICAL_COMPARISON: 10,
}

_SEVERITY_SCORE = {
    Urgency.NONE: 0.0,
    Urgency.LOW: 0.25,
    Urgency.MODERATE: 0.5,
    Urgency.HIGH: 0.8,
    Urgency.IMMEDIATE: 1.0,
}

_DOMAIN_LABEL = {
    InfrastructureDomain.ROAD: "road",
    InfrastructureDomain.TRANSIT: "transit service",
    InfrastructureDomain.UTILITY: "utility",
    InfrastructureDomain.PUBLIC_SAFETY: "public safety",
    InfrastructureDomain.PUBLIC_FACILITY: "public facility",
    InfrastructureDomain.FERRIES: "ferry service",
    InfrastructureDomain.UNKNOWN: "infrastructure",
}


def _forecast_confidence(forecast: Forecast) -> float:
    """A wide prediction interval is low confidence, by definition.

    Derived from the stored interval rather than asserted, so section 39's
    "do not produce a simplistic confidence without its components" holds:
    ``upper - lower`` is the component, and it stays in the payload.
    """
    return max(0.0, 1.0 - (forecast.upper - forecast.lower))


def _impact_score(impact: AffectedInfrastructure) -> float:
    return _SEVERITY_SCORE[impact.severity]


def _impact_confidence(impact: AffectedInfrastructure) -> float:
    """Confidence attaches to how the impact is *known*, never to how bad it is."""
    return {
        TruthStatus.CONFIRMED: 0.95,
        TruthStatus.REPORTED: 0.75,
        TruthStatus.INFERRED: 0.55,
        TruthStatus.PREDICTED: 0.3,
    }.get(impact.truth_status, 0.4)


def _impact_headline(impact: AffectedInfrastructure) -> str:
    label = _DOMAIN_LABEL.get(impact.domain, impact.domain.value.replace("_", " "))
    name = impact.name or impact.identifier
    return f"{label.capitalize()} affected: {name}"


def _change_headline(change: str) -> str:
    """Turn a machine change name into one readable clause."""
    readable = change.replace("_", " ")
    return f"What changed: {readable}"


__all__ = [
    "INTERRUPT_FLOOR",
    "OFFICIAL_PRIORITY",
    "PresentationContext",
    "PresentationEngine",
    "PresentationPayload",
    "ScreenInstructionError",
]
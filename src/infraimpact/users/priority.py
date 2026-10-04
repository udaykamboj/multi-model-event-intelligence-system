"""Personalized priority (brief section 36).

The section gives an initial ranking model:

    UserPriority =
        ImpactMagnitude
      x UserExposure
      x Urgency
      x ChangeMagnitude
      x EvidenceConfidence

Two properties of that formula drive this implementation.

1.  *Do not collapse these dimensions permanently.* Every factor is computed
    and stored on its own field. Nothing is folded together on the way out,
    so a later learned ranker can read the same components.

2.  It is a product of five independent factors, so a raw product of five
    values in 0..1 spends almost all of its range near zero (0.7 ** 5 =
    0.168). The ordering is exactly right; the *scale* is unusable for
    thresholds and for display. We therefore take the fifth root - the
    geometric mean - which is a strictly monotone transform of the product
    and therefore produces an identical ranking, while landing in a usable
    0..1 range. The raw product is retained in ``components`` so the
    documented formula stays inspectable and reversible.

The one semantic that must survive any rescaling: if any factor is zero the
result is zero. No exposure means no priority. No confidence means no
priority. A user is never handed something alarming that the platform cannot
support.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from ..delta.engine import DeltaReport, has_official_guidance, peak_urgency, truth_confidence
from ..domain.enums import Urgency
from ..domain.schemas import AffectedInfrastructure, EventState, UserExposure, UserPriority

log = logging.getLogger(__name__)

_URGENCY_SCORE = {
    Urgency.NONE: 0.0,
    Urgency.LOW: 0.2,
    Urgency.MODERATE: 0.5,
    Urgency.HIGH: 0.8,
    Urgency.IMMEDIATE: 1.0,
}

#: Ordered for section 38: official guidance always outranks everything.
_FACTORS = (
    "impact_magnitude",
    "user_exposure",
    "urgency",
    "change_magnitude",
    "evidence_confidence",
)

#: Official guidance is never allowed to sit below this, whatever the
#: arithmetic says. This is the presentation-side twin of ``exposure_band``.
_OFFICIAL_PRIORITY_FLOOR = 0.9


@dataclass
class PriorityContext:
    """Everything priority may read. Passed explicitly, never imported."""

    exposure: UserExposure
    state: EventState
    impacts: Sequence[AffectedInfrastructure] = ()
    delta_report: DeltaReport | None = None
    #: True when this is the first state the user has ever seen for the event.
    is_new_event: bool = False


class UserPriorityEngine:
    """Scores one user's priority for one event."""

    def __init__(self, jev: Any | None = None) -> None:
        self.jev = jev

    def compute(self, ctx: PriorityContext) -> UserPriority:
        components = {
            "impact_magnitude": self._impact_magnitude(ctx),
            "user_exposure": ctx.exposure.exposure_score,
            "urgency": _URGENCY_SCORE[self._urgency_band(ctx)],
            "change_magnitude": self._change_magnitude(ctx),
            "evidence_confidence": self._evidence_confidence(ctx),
        }

        # Section 21: Jev bounded decision queries
        jev_decisions: dict[str, Any] = {}
        if self.jev is not None:
            try:
                from ..jev.client import JevQuestion
                questions = [
                    JevQuestion(
                        question_id="route_materially_affected",
                        kind="noul",
                        text="Does the new state materially affect this user's route?",
                        state={
                            "route_impacted": bool(ctx.exposure.route_impacts),
                            "user_exposure": ctx.exposure.exposure_score,
                        },
                    ),
                    JevQuestion(
                        question_id="urgency_score",
                        kind="score",
                        text="Rate the urgency of this user's infrastructure impact.",
                        state={
                            "exposure_score": ctx.exposure.exposure_score,
                            "change_magnitude": components["change_magnitude"],
                            "confidence": components["evidence_confidence"],
                        },
                    ),
                ]
                jev_decisions = self.jev.ask(questions)
            except Exception as exc:  # noqa: BLE001
                log.warning("jev priority decision failed: %r", exc)

        components["product"] = self._product(components)

        priority = self._geometric_mean(components)
        official = has_official_guidance(ctx.state)
        if official:
            priority = max(priority, _OFFICIAL_PRIORITY_FLOOR)

        return UserPriority(
            user_id=ctx.exposure.user_id,
            event_id=ctx.exposure.event_id,
            impact_magnitude=round(components["impact_magnitude"], 4),
            user_exposure=round(components["user_exposure"], 4),
            urgency=round(components["urgency"], 4),
            change_magnitude=round(components["change_magnitude"], 4),
            evidence_confidence=round(components["evidence_confidence"], 4),
            priority=round(priority, 4),
            urgency_band=self._urgency_band(ctx),
            components=self._public_components(components, official, jev_decisions),
        )

    @staticmethod
    def _public_components(
        components: dict[str, float], official: bool, jev_decisions: dict[str, Any] | None = None
    ) -> dict[str, float]:
        """Factors as stored on the contract, without the scratch product."""
        public = {k: round(v, 4) for k, v in components.items() if k != "product"}
        public["official_guidance"] = 1.0 if official else 0.0
        if jev_decisions:
            for k, d in jev_decisions.items():
                val = d.get("value")
                if isinstance(val, (int, float)):
                    public[f"jev_{k}"] = round(float(val), 4)
        return public

    # -- factors ----------------------------------------------------------

    @staticmethod
    def _impact_magnitude(ctx: PriorityContext) -> float:
        """How big is the hit, regardless of whether this user feels it."""
        if not ctx.impacts:
            return 0.0
        severity = _URGENCY_SCORE[peak_urgency(ctx.impacts)]
        # Breadth saturates at ~6 affected assets; beyond that the count stops
        # adding information about how bad it is for any one person.
        breadth = min(1.0, len(ctx.impacts) / 6.0)
        return min(1.0, 0.7 * severity + 0.3 * breadth)

    def _urgency_band(self, ctx: PriorityContext) -> Urgency:
        """The more urgent of the user's own band and the event's severity.

        A user who is only mildly exposed to a severe event is not thereby
        in an urgent situation; a user with official guidance is always in an
        urgent situation regardless of their own arithmetic.
        """
        if has_official_guidance(ctx.state):
            return Urgency.IMMEDIATE
        own = ctx.exposure.exposure_level
        event = peak_urgency(ctx.impacts)
        if _URGENCY_SCORE[event] > _URGENCY_SCORE[own]:
            return event
        return own

    @staticmethod
    def _change_magnitude(ctx: PriorityContext) -> float:
        """Section 54: only changes are worth the user's attention.

        A brand-new event is maximally "changed" for a user meeting it for the
        first time, otherwise the initial alert would score zero.
        """
        report = ctx.delta_report
        if report is None:
            return 1.0 if ctx.is_new_event else 0.0
        if ctx.is_new_event:
            return 1.0
        if not report.deltas:
            return 0.0
        return min(1.0, max(d.magnitude for d in report.deltas))

    @staticmethod
    def _evidence_confidence(ctx: PriorityContext) -> float:
        """Confidence that this user is affected - not that an event exists.

        The exposure confidence already folds in source authority, spatial
        precision, corroboration and impact truth status. Impact grounding is
        blended in so that a well-located user near a weakly-evidenced impact
        does not inherit full confidence.
        """
        grounding = truth_confidence(ctx.impacts) if ctx.impacts else 0.0
        return 0.6 * ctx.exposure.confidence + 0.4 * grounding

    # -- combination ------------------------------------------------------

    @staticmethod
    def _product(components: dict[str, float]) -> float:
        value = 1.0
        for name in _FACTORS:
            value *= components[name]
        return value

    @staticmethod
    def _geometric_mean(components: dict[str, float]) -> float:
        """Fifth root of the product: same ranking, usable scale."""
        return UserPriorityEngine._product(components) ** (1.0 / len(_FACTORS))


def rank_priorities(priorities: Sequence[UserPriority]) -> list[UserPriority]:
    """Order for display.

    Ties break on the section 38 safety rule (official guidance first), then
    on the individual factors a user would care about more than the
    composite - a tie in priority should never hide an official alert behind
    a merely-loud prediction.
    """
    return sorted(
        priorities,
        key=lambda p: (
            -p.priority,
            -p.components.get("official_guidance", 0.0),
            -_URGENCY_SCORE[p.urgency_band],
            -p.user_exposure,
            -p.change_magnitude,
        ),
    )


__all__: list[str] = ["PriorityContext", "UserPriorityEngine", "rank_priorities"]
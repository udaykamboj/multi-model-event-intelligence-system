"""Section 36: personalized priority.

The section's formula is a product of five factors, and its first rule is
"Do not collapse these dimensions permanently. Store every component
independently." These tests pin both the formula's ordering semantics and the
independence of the stored components.
"""

from __future__ import annotations

import pytest
from conftest import make_exposure, make_impact, make_state

from infraimpact.delta.engine import DeltaReport, StateDelta
from infraimpact.domain.enums import Urgency
from infraimpact.domain.schemas import UserPriority
from infraimpact.users.priority import (
    PriorityContext,
    UserPriorityEngine,
    rank_priorities,
)


@pytest.fixture
def engine():
    return UserPriorityEngine()


def ctx(exposure, *, impacts=(), state=None, report=None, is_new=False):
    return PriorityContext(
        exposure=exposure,
        state=state or make_state(impacts=tuple(impacts)),
        impacts=tuple(impacts),
        delta_report=report,
        is_new_event=is_new,
    )


def report_with(magnitude: float, change: str = "road_closed") -> DeltaReport:
    return DeltaReport(
        deltas=(StateDelta(change=change, magnitude=magnitude, confidence=0.8, novelty=0.9),),
        magnitude=magnitude,
        novelty=0.9,
        confidence=0.8,
        is_material=True,
    )


class TestComponentIndependence:
    def test_all_five_factors_are_stored_separately(self, engine):
        impact = make_impact(severity=Urgency.HIGH)
        p = engine.compute(
            ctx(
                make_exposure(level=Urgency.HIGH, score=0.7, confidence=0.8),
                impacts=[impact],
                report=report_with(0.6),
            )
        )
        for field in (
            "impact_magnitude",
            "user_exposure",
            "urgency",
            "change_magnitude",
            "evidence_confidence",
        ):
            assert hasattr(p, field)
            assert 0.0 <= getattr(p, field) <= 1.0

    def test_components_mirror_the_top_level_fields(self, engine):
        p = engine.compute(ctx(make_exposure(score=0.66, confidence=0.72)))
        assert p.components["user_exposure"] == p.user_exposure
        assert p.components["change_magnitude"] == p.change_magnitude
        assert p.components["evidence_confidence"] == p.evidence_confidence

    def test_components_are_not_all_equal_to_the_composite(self, engine):
        # If priority were simply a copy of one factor, collapsing would have
        # already happened.
        p = engine.compute(ctx(make_exposure(score=0.7)))
        assert len({p.user_exposure, p.urgency, p.change_magnitude}) > 1

    def test_stored_priority_survives_round_trip(self, engine):
        p = engine.compute(ctx(make_exposure(score=0.5)))
        again = UserPriority.model_validate(p.model_dump(mode="json"))
        assert again.priority == p.priority
        assert again.components == p.components


class TestRankingSemantics:
    def test_higher_exposure_ranks_higher(self, engine):
        # change_magnitude must be non-zero for both, or the multiplicative
        # model correctly returns zero for both and nothing can be compared.
        impact = [make_impact(severity=Urgency.HIGH)]
        report = report_with(0.5)
        low = engine.compute(
            ctx(make_exposure(score=0.3, level=Urgency.LOW), impacts=impact, report=report)
        )
        high = engine.compute(
            ctx(make_exposure(score=0.8, level=Urgency.HIGH), impacts=impact, report=report)
        )
        assert high.priority > low.priority

    def test_more_change_ranks_higher(self, engine):
        impact = [make_impact(severity=Urgency.HIGH)]
        exposure = make_exposure(score=0.6, level=Urgency.MODERATE)
        still = engine.compute(ctx(exposure, impacts=impact, report=report_with(0.05, "minor")))
        moved = engine.compute(ctx(exposure, impacts=impact, report=report_with(0.8, "road_closed")))
        assert moved.priority > still.priority

    def test_lower_confidence_ranks_lower(self, engine):
        impact = [make_impact(severity=Urgency.HIGH)]
        exposure = make_exposure(score=0.6, level=Urgency.MODERATE, confidence=0.6)
        sure = engine.compute(ctx(exposure, impacts=impact, report=report_with(0.5)))
        unsure = engine.compute(
            ctx(
                exposure.model_copy(update={"confidence": 0.1}),
                impacts=impact,
                report=report_with(0.5),
            )
        )
        assert sure.priority > unsure.priority

    def test_any_zero_factor_zeroes_priority(self, engine):
        # No exposure means no priority, however alarming the event.
        p = engine.compute(
            ctx(make_exposure(score=0.0, level=Urgency.NONE), report=report_with(1.0))
        )
        assert p.priority == 0.0

    def test_no_change_means_no_priority_for_a_known_user(self, engine):
        exposure = make_exposure(score=0.6, level=Urgency.MODERATE)
        p = engine.compute(ctx(exposure, report=DeltaReport()))
        assert p.change_magnitude == 0.0
        assert p.priority == 0.0

    def test_new_event_counts_as_maximal_change(self, engine):
        exposure = make_exposure(score=0.6, level=Urgency.MODERATE)
        fresh = engine.compute(
            ctx(exposure, impacts=[make_impact()], report=DeltaReport(), is_new=True)
        )
        assert fresh.change_magnitude == 1.0
        assert fresh.priority > 0.0


class TestUrgencyBand:
    def test_band_follows_the_more_urgent_of_user_and_event(self, engine):
        # Mildly exposed user, but the event itself is severe.
        impact = make_impact(severity=Urgency.IMMEDIATE)
        p = engine.compute(
            ctx(make_exposure(score=0.3, level=Urgency.LOW), impacts=[impact])
        )
        assert p.urgency_band is Urgency.IMMEDIATE

    def test_a_severe_event_does_not_inflate_a_distant_user(self, engine):
        remote = make_exposure(
            user_id="u_remote", score=0.05, level=Urgency.LOW, distance_m=8000.0
        )
        impact = make_impact(severity=Urgency.IMMEDIATE)
        p = engine.compute(ctx(remote, impacts=[impact]))
        assert p.urgency_band is Urgency.IMMEDIATE
        # But priority still reflects that the user is barely exposed.
        assert p.user_exposure == 0.05

    def test_official_guidance_forces_immediate(self, engine):
        p = engine.compute(
            ctx(
                make_exposure(score=0.2, level=Urgency.LOW),
                state=make_state(official=True),
            )
        )
        assert p.urgency_band is Urgency.IMMEDIATE


class TestOfficialGuidanceFloor:
    def test_official_guidance_never_sits_low(self, engine):
        # Deliberately weak on every factor, but officially advised.
        weak = make_exposure(score=0.15, level=Urgency.LOW, confidence=0.3)
        p = engine.compute(
            ctx(weak, state=make_state(official=True), report=report_with(0.1))
        )
        assert p.priority >= 0.9
        assert p.components["official_guidance"] == 1.0

    def test_official_flag_absent_otherwise(self, engine):
        p = engine.compute(ctx(make_exposure(score=0.7, level=Urgency.HIGH)))
        assert p.components["official_guidance"] == 0.0


class TestImpactMagnitude:
    def test_severity_drives_magnitude(self, engine):
        mild = engine.compute(
            ctx(make_exposure(score=0.6), impacts=[make_impact("a", severity=Urgency.LOW)])
        )
        severe = engine.compute(
            ctx(
                make_exposure(score=0.6),
                impacts=[make_impact("b", severity=Urgency.IMMEDIATE)],
            )
        )
        assert severe.impact_magnitude > mild.impact_magnitude

    def test_no_impacts_means_zero_magnitude(self, engine):
        p = engine.compute(ctx(make_exposure(score=0.6)))
        assert p.impact_magnitude == 0.0
        assert p.priority == 0.0

    def test_breadth_saturates(self, engine):
        few = engine.compute(
            ctx(
                make_exposure(score=0.6),
                impacts=[make_impact(f"a{i}", severity=Urgency.HIGH) for i in range(2)],
            )
        )
        many = engine.compute(
            ctx(
                make_exposure(score=0.6),
                impacts=[make_impact(f"a{i}", severity=Urgency.HIGH) for i in range(40)],
            )
        )
        assert many.impact_magnitude > few.impact_magnitude
        assert many.impact_magnitude <= 1.0


class TestRankPriorities:
    def test_orders_descending(self, engine):
        items = [
            engine.compute(ctx(make_exposure(user_id=f"u{i}", score=s, level=Urgency.MODERATE)))
            for i, s in enumerate([0.2, 0.8, 0.5])
        ]
        ranked = rank_priorities(items)
        assert [p.priority for p in ranked] == sorted(
            [p.priority for p in items], reverse=True
        )

    def test_official_breaks_a_tie(self):
        normal = UserPriority(
            user_id="a", event_id="e", priority=0.5, urgency_band=Urgency.HIGH
        )
        official = UserPriority(
            user_id="b",
            event_id="e",
            priority=0.5,
            urgency_band=Urgency.HIGH,
            components={"official_guidance": 1.0},
        )
        assert rank_priorities([normal, official])[0] is official

    def test_empty_input_is_fine(self):
        assert rank_priorities([]) == []
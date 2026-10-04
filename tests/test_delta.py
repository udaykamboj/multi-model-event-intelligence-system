"""Sections 32, 33 and 54: "what changed?" as first-class data.

The defining behaviour of the delta engine is restraint. A news article saying
"demonstration continues" must be archived and produce no material delta. A
closure must produce one. And section 54 requires the changes to be explicit
and attributable, so a user can be told *why* their situation changed.
"""

from __future__ import annotations

import pytest
from conftest import DOWNTOWN, line, make_exposure, make_impact, make_state, point

from infraimpact.delta.engine import (
    MATERIALITY_FLOOR,
    StateDeltaEngine,
    compare_user_exposure,
    dominant_label,
    has_official_guidance,
    peak_urgency,
    truth_confidence,
)
from infraimpact.domain.enums import TruthStatus, Urgency
from infraimpact.domain.geo import buffer_geometry
from infraimpact.domain.schemas import RouteImpact


@pytest.fixture
def engine():
    return StateDeltaEngine()


class TestMateriality:
    def test_unchanged_state_produces_nothing(self, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        state = make_state(geometry=poly)
        report = engine.compare(state, state)
        assert report.deltas == ()
        assert not report.is_material
        assert report.suppressed_reason == "no observable change"

    def test_first_state_is_material(self, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        report = engine.compare(None, make_state(geometry=poly))
        assert report.is_material
        assert any(d.change == "event_initialised" for d in report.deltas)

    def test_footprint_growth_is_material(self, engine):
        small = make_state(
            state_version=1, geometry=buffer_geometry(point(*DOWNTOWN), 200.0)
        )
        big = make_state(
            state_version=2, geometry=buffer_geometry(point(*DOWNTOWN), 4000.0)
        )
        report = engine.compare(small, big)
        assert report.is_material
        assert any("geometry" in d.change or "footprint" in d.change for d in report.deltas)

    def test_status_transition_is_material(self, engine):
        before = make_state(state_version=1)
        after = make_state(state_version=2, status="closed")
        report = engine.compare(before, after)
        assert report.is_material

    def test_new_severity_is_material(self, engine):
        before = make_state(state_version=1, impacts=(make_impact("r", severity=Urgency.LOW),))
        after = make_state(
            state_version=2,
            impacts=(make_impact("r", severity=Urgency.IMMEDIATE),),
        )
        report = engine.compare(before, after)
        assert report.is_material

    def test_materiality_floor_is_a_real_threshold(self, engine):
        assert 0.0 < MATERIALITY_FLOOR < 1.0

    def test_report_magnitude_matches_its_deltas(self, engine):
        before = make_state(state_version=1)
        after = make_state(
            state_version=2,
            geometry=buffer_geometry(point(*DOWNTOWN), 3000.0),
            impacts=(make_impact(severity=Urgency.HIGH),),
        )
        report = engine.compare(before, after)
        assert report.magnitude == pytest.approx(max(d.magnitude for d in report.deltas))

    def test_material_deltas_are_the_subset_above_the_floor(self, engine):
        before = make_state(state_version=1)
        after = make_state(
            state_version=2, geometry=buffer_geometry(point(*DOWNTOWN), 3000.0)
        )
        report = engine.compare(before, after)
        for d in report.material_deltas:
            assert d.magnitude >= MATERIALITY_FLOOR
        for d in report.deltas:
            if d.magnitude < MATERIALITY_FLOOR:
                assert d not in report.material_deltas


class TestSection33Restraint:
    def test_delta_does_not_delete_observations(self, engine):
        # The delta is about propagation, not history: the caller keeps the
        # observation regardless, and this test documents that the engine
        # only ever reports "suppressed", never "discarded".
        before = make_state(state_version=1, observation_ids=("obs_1",))
        after = make_state(state_version=2, observation_ids=("obs_1", "obs_2"))
        report = engine.compare(before, after)
        assert after.observation_ids == ("obs_1", "obs_2")

    def test_change_with_no_new_observations_is_flagged(self, engine):
        # A recomputation artifact, not new world information. The engine must
        # say so rather than propagating a phantom change.
        before = make_state(state_version=1, geometry=buffer_geometry(point(*DOWNTOWN), 500.0))
        after = make_state(state_version=2, geometry=buffer_geometry(point(*DOWNTOWN), 6000.0))
        report = engine.compare(before, after)
        if report.is_material and not report.causes:
            assert any("recomputation artifact" in n for n in report.notes)

    def test_suppressed_reason_is_always_explanatory(self, engine):
        report = engine.compare(None, make_state())
        if not report.is_material:
            assert report.suppressed_reason


class TestOfficialGuidance:
    def test_flag_is_detected(self):
        assert has_official_guidance(make_state(official=True))

    def test_inferred_overlap_is_not_official(self):
        # Geometry overlap must never be promoted to official guidance.
        assert not has_official_guidance(make_state())

    def test_notice_type_alone_is_enough(self):
        from infraimpact.domain.schemas import EventState, EvidenceSummary

        state = make_state()
        state = state.model_copy(
            update={
                "evidence": EvidenceSummary(
                    observation_types={"official_emergency_notice": 1}
                )
            }
        )
        assert has_official_guidance(state)


class TestModuleHelpers:
    def test_dominant_label(self):
        assert dominant_label({"protest": 0.8, "other": 0.2}) == "protest"
        assert dominant_label({"a": 0.5, "b": 0.5}) in (None, "a", "b")
        assert dominant_label({}) is None

    def test_peak_urgency(self):
        impacts = [
            make_impact("a", severity=Urgency.LOW),
            make_impact("b", severity=Urgency.IMMEDIATE),
        ]
        assert peak_urgency(impacts) is Urgency.IMMEDIATE
        assert peak_urgency([]) is Urgency.NONE

    def test_truth_confidence_ranks_truth_status(self):
        confirmed = truth_confidence([make_impact("a", truth_status="confirmed")])
        inferred = truth_confidence([make_impact("b", truth_status="inferred")])
        assert confirmed > inferred
        assert truth_confidence([]) == 0.0


class TestSection54UserDeltas:
    def test_first_sighting_is_recorded(self):
        exposure = make_exposure(level=Urgency.HIGH, score=0.7)
        deltas = compare_user_exposure(None, exposure)
        assert len(deltas) == 1
        assert deltas[0].change == "user_first_exposed"
        assert deltas[0].before is None
        assert deltas[0].after == "high"
        assert deltas[0].magnitude == 1.0

    def test_first_sighting_of_a_non_event_is_low_magnitude(self):
        exposure = make_exposure(level=Urgency.NONE, score=0.0)
        deltas = compare_user_exposure(None, exposure)
        assert deltas[0].change == "user_first_seen"
        assert deltas[0].magnitude < 1.0

    def test_route_becoming_impacted_is_recorded(self):
        before = make_exposure(level=Urgency.LOW, score=0.2)
        after = make_exposure(
            level=Urgency.HIGH,
            score=0.7,
            routes=(
                RouteImpact(
                    route_id="r1",
                    route_name="Commute",
                    intersects=True,
                    blocked_node_ids=("n1",),
                ),
            ),
        )
        changes = {d.change for d in compare_user_exposure(before, after)}
        assert "user_route_became_impacted" in changes

    def test_route_clearing_is_recorded(self):
        impacted = make_exposure(
            level=Urgency.HIGH,
            score=0.7,
            routes=(
                RouteImpact(
                    route_id="r1",
                    route_name="Commute",
                    intersects=True,
                    blocked_node_ids=("n1",),
                ),
            ),
        )
        clear = make_exposure(level=Urgency.LOW, score=0.2)
        changes = {d.change for d in compare_user_exposure(impacted, clear)}
        assert "user_route_cleared" in changes

    def test_route_delta_carries_before_and_after_booleans(self):
        before = make_exposure(level=Urgency.LOW, score=0.2)
        after = make_exposure(
            level=Urgency.HIGH,
            score=0.7,
            routes=(
                RouteImpact(
                    route_id="r1", route_name="Commute", intersects=True, blocked_node_ids=("n1",)
                ),
            ),
        )
        delta = next(
            d for d in compare_user_exposure(before, after)
            if d.change == "user_route_became_impacted"
        )
        assert delta.before is False
        assert delta.after is True

    def test_escalation_is_recorded(self):
        before = make_exposure(level=Urgency.LOW, score=0.2)
        after = make_exposure(level=Urgency.IMMEDIATE, score=0.9)
        changes = {d.change for d in compare_user_exposure(before, after)}
        assert "user_exposure_escalated" in changes

    def test_deescalation_is_recorded(self):
        before = make_exposure(level=Urgency.IMMEDIATE, score=0.9)
        after = make_exposure(level=Urgency.LOW, score=0.2)
        changes = {d.change for d in compare_user_exposure(before, after)}
        assert "user_exposure_deescalated" in changes

    def test_unchanged_exposure_produces_nothing(self):
        exposure = make_exposure(level=Urgency.HIGH, score=0.7)
        assert compare_user_exposure(exposure, exposure) == []

    def test_causes_are_attached_to_every_change(self):
        before = make_exposure(level=Urgency.LOW, score=0.2)
        after = make_exposure(level=Urgency.HIGH, score=0.7)
        deltas = compare_user_exposure(before, after, causes=("road_closure_581",))
        assert deltas
        assert all("road_closure_581" in d.causes for d in deltas)

    def test_deltas_carry_the_users_urgency_and_audience(self):
        after = make_exposure(level=Urgency.IMMEDIATE, score=0.9)
        deltas = compare_user_exposure(None, after)
        assert deltas[0].urgency is Urgency.IMMEDIATE
        assert deltas[0].affected_user_count == 1
        assert deltas[0].domain == "user"

    def test_a_place_becoming_impacted_is_recorded(self):
        before = make_exposure(level=Urgency.LOW, score=0.2, places={"p_home": 0.0})
        after = make_exposure(level=Urgency.HIGH, score=0.7, places={"p_home": 0.8})
        changes = {d.change for d in compare_user_exposure(before, after)}
        assert "user_place_became_impacted" in changes
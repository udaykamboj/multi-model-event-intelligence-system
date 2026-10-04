"""Sections 37 and 38: the presentation contract.

Section 37's rule is that the backend ranks and the renderer decides: it must
never emit a screen instruction. Section 38's rule is that official emergency
guidance takes the highest presentation priority and its meaning is
preserved exactly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import make_exposure, make_forecast, make_impact, make_state

from infraimpact.delta.engine import DeltaReport, StateDelta
from infraimpact.domain.enums import (
    ObservationType,
    PresentationType,
    SourceType,
    TruthStatus,
    Urgency,
)
from infraimpact.domain.ids import content_hash
from infraimpact.domain.schemas import (
    Observation,
    ObservationQuality,
    Provenance,
    RouteImpact,
)
from infraimpact.users.presentation import (
    INTERRUPT_FLOOR,
    OFFICIAL_PRIORITY,
    PresentationContext,
    PresentationEngine,
    PresentationPayload,
    ScreenInstructionError,
)
from infraimpact.users.priority import PriorityContext, UserPriorityEngine


@pytest.fixture
def engine():
    return PresentationEngine()


@pytest.fixture
def priority_engine():
    return UserPriorityEngine()


def make_observation(
    *,
    observation_type: ObservationType = ObservationType.OFFICIAL_EMERGENCY_NOTICE,
    headline: str = "Avoid the downtown corridor until further notice",
    instruction: str | None = None,
    source_id: str = "alertseattle",
) -> Observation:
    payload = {"instruction": instruction} if instruction else {}
    now = datetime.now(UTC)
    return Observation(
        observation_id="obs_notice",
        source_id=source_id,
        source_record_id="rec_1",
        event_time=now,
        observed_at=now,
        ingested_at=now,
        source_type=SourceType.OFFICIAL_HUMAN_READABLE,
        observation_type=observation_type,
        headline=headline,
        structured_payload=payload,
        source_url="https://alert.seattle.gov/1",
        provenance=Provenance(
            authority="official",
            retrieval_method="feed",
            content_hash=content_hash(payload),
        ),
        quality=ObservationQuality(source_reliability=0.98, spatial_precision=0.9),
    )


def build(
    engine,
    priority_engine,
    *,
    exposure=None,
    impacts=(),
    observations=(),
    forecasts=(),
    state=None,
    report=None,
    is_new=False,
):
    """Build a payload.

    Unless told otherwise, this supplies a material delta report. The
    multiplicative priority model (section 36) returns zero when
    ``change_magnitude`` is zero, which would collapse every payload to a
    single QUIET item and make these tests vacuous.
    """
    exposure = exposure or make_exposure(level=Urgency.HIGH, score=0.7, confidence=0.8)
    if report is None and not is_new:
        report = DeltaReport(
            deltas=(
                StateDelta(change="road_closed", magnitude=0.7, confidence=0.8, novelty=0.9),
            ),
            magnitude=0.7,
            novelty=0.9,
            confidence=0.8,
            is_material=True,
        )
    state = state or make_state(impacts=tuple(impacts))
    priority = priority_engine.compute(
        PriorityContext(
            exposure=exposure,
            state=state,
            impacts=tuple(impacts),
            delta_report=report,
            is_new_event=is_new,
        )
    )
    return engine.build(
        PresentationContext(
            exposure=exposure,
            priority=priority,
            state=state,
            impacts=tuple(impacts),
            forecasts=tuple(forecasts),
            observations=tuple(observations),
            delta_report=report,
            is_new_event=is_new,
        )
    )


class TestNoScreenInstructions:
    def test_types_are_all_content_shaped(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        allowed = set(PresentationType)
        assert {i.type for i in payload.items} <= allowed

    def test_no_forbidden_tokens_in_any_payload(self, engine, priority_engine):
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            observations=[make_observation()],
            state=make_state(impacts=(make_impact(),), official=True),
            report=DeltaReport(
                deltas=(StateDelta(change="road_closed", magnitude=0.7, confidence=0.8),),
                magnitude=0.7,
                is_material=True,
            ),
        )
        blob = json.dumps(payload.as_dict()).lower()
        for token in ("show_level", "navigate_to", "screen_level", "open_screen"):
            assert token not in blob

    def test_guard_actually_fires(self, engine):
        from infraimpact.domain.schemas import PresentationItem

        bad = PresentationItem(
            type=PresentationType.EVENT_SUMMARY,
            headline="show_level_2_screen",
            payload={},
        )
        with pytest.raises(ScreenInstructionError):
            engine._assert_no_screen_instructions(bad)

    def test_guard_accepts_ordinary_content(self, engine):
        from infraimpact.domain.schemas import PresentationItem

        good = PresentationItem(
            type=PresentationType.EVENT_SUMMARY,
            headline="A road closure is affecting your route",
            payload={"route_id": "r1"},
        )
        engine._assert_no_screen_instructions(good)

    def test_payload_serialises_to_json(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        assert json.loads(json.dumps(payload.as_dict()))["event_id"] == "evt_test"


class TestOfficialGuidance:
    def test_official_item_ranks_first(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), official=True)
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            observations=[make_observation()],
            state=state,
        )
        assert payload.items[0].type is PresentationType.OFFICIAL_GUIDANCE

    def test_official_priority_is_the_maximum(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), official=True)
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            observations=[make_observation()],
            state=state,
        )
        official = payload.of_type(PresentationType.OFFICIAL_GUIDANCE)[0]
        assert official.priority == OFFICIAL_PRIORITY
        assert all(official.priority >= i.priority for i in payload.items)

    def test_official_wording_is_verbatim(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), official=True)
        text = "CITY OF SEATTLE: shelter in place immediately"
        notice = make_observation(headline=text)
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            observations=[notice],
            state=state,
        )
        official = payload.of_type(PresentationType.OFFICIAL_GUIDANCE)[0]
        assert official.headline == text

    def test_official_instruction_is_carried_through(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), official=True)
        notice = make_observation(instruction="Go to the nearest shelter")
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            observations=[notice],
            state=state,
        )
        official = payload.of_type(PresentationType.OFFICIAL_GUIDANCE)[0]
        assert official.detail == "Go to the nearest shelter"

    def test_official_is_marked_as_not_contradictable(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), official=True)
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            observations=[make_observation()],
            state=state,
        )
        official = payload.of_type(PresentationType.OFFICIAL_GUIDANCE)[0]
        assert official.payload["verbatim"] is True
        assert official.payload["may_not_be_contradicted_by_prediction"] is True
        assert official.truth_status is TruthStatus.CONFIRMED

    def test_no_official_notice_means_no_official_item(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        assert payload.of_type(PresentationType.OFFICIAL_GUIDANCE) == []

    def test_official_state_without_a_notice_emits_no_item(self, engine, priority_engine):
        # The flag is set but no notice observation is available to quote, so
        # there is nothing verbatim to present.
        payload = build(
            engine,
            priority_engine,
            impacts=[make_impact()],
            state=make_state(impacts=(make_impact(),), official=True),
        )
        assert payload.of_type(PresentationType.OFFICIAL_GUIDANCE) == []


class TestRanking:
    def test_items_are_sorted_by_priority(self, engine, priority_engine):
        route = RouteImpact(
            route_id="r1", route_name="Commute", intersects=True, blocked_node_ids=("n1",)
        )
        exposure = make_exposure(
            level=Urgency.HIGH, score=0.8, confidence=0.85, routes=(route,)
        )
        payload = build(
            engine,
            priority_engine,
            exposure=exposure,
            impacts=[make_impact()],
            report=DeltaReport(
                deltas=(StateDelta(change="road_closed", magnitude=0.8, confidence=0.8),),
                magnitude=0.8,
                is_material=True,
            ),
        )
        priorities = [i.priority for i in payload.items]
        assert priorities == sorted(priorities, reverse=True)

    def test_route_disruption_precedes_exposure_on_a_tie(self, engine, priority_engine):
        route = RouteImpact(
            route_id="r1", route_name="Commute", intersects=True, blocked_node_ids=("n1",)
        )
        exposure = make_exposure(
            level=Urgency.HIGH, score=0.8, confidence=0.85, routes=(route,)
        )
        payload = build(engine, priority_engine, exposure=exposure, impacts=[make_impact()])
        types = [i.type for i in payload.items]
        if PresentationType.ROUTE_DISRUPTION in types and PresentationType.USER_EXPOSURE in types:
            assert types.index(PresentationType.ROUTE_DISRUPTION) < types.index(
                PresentationType.USER_EXPOSURE
            )

    def test_evidence_item_is_always_present_and_low(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        evidence = payload.of_type(PresentationType.EVIDENCE)
        assert len(evidence) == 1
        assert evidence[0].priority <= 0.3

    def test_ranking_is_deterministic(self, engine, priority_engine):
        first = build(engine, priority_engine, impacts=[make_impact()])
        second = build(engine, priority_engine, impacts=[make_impact()])
        assert [i.type for i in first.items] == [i.type for i in second.items]


class TestQuiet:
    def test_unexposed_user_gets_a_quiet_item(self, engine, priority_engine):
        exposure = make_exposure(
            user_id="u_remote",
            level=Urgency.NONE,
            score=0.0,
            confidence=0.5,
            distance_m=9000.0,
        )
        payload = build(engine, priority_engine, exposure=exposure, impacts=[make_impact()])
        assert len(payload.items) == 1
        assert payload.items[0].type is PresentationType.QUIET
        assert payload.items[0].priority == 0.0

    def test_quiet_item_explains_why(self, engine, priority_engine):
        exposure = make_exposure(
            level=Urgency.NONE, score=0.0, confidence=0.5, distance_m=9000.0
        )
        payload = build(engine, priority_engine, exposure=exposure)
        assert payload.items[0].payload["reason"] == "no_personal_impact"

    def test_exposed_user_does_not_get_a_quiet_item(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        assert payload.of_type(PresentationType.QUIET) == []


class TestUncertainty:
    def test_contradictions_surface_as_uncertainty(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), contradictions=4, independent=1)
        payload = build(engine, priority_engine, impacts=[make_impact()], state=state)
        uncertainty = payload.of_type(PresentationType.UNCERTAINTY)
        assert len(uncertainty) == 1
        assert any("conflicting" in r for r in uncertainty[0].payload["reasons"])

    def test_poor_geometry_surfaces_as_uncertainty(self, engine, priority_engine):
        state = make_state(impacts=(make_impact(),), geometry_confidence=0.2)
        payload = build(engine, priority_engine, impacts=[make_impact()], state=state)
        assert payload.of_type(PresentationType.UNCERTAINTY)

    def test_clean_evidence_produces_no_uncertainty(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        assert payload.of_type(PresentationType.UNCERTAINTY) == []


class TestPredictionsNeverOutrankReality:
    def test_forecast_caps_at_the_exposure_score(self, engine, priority_engine):
        exposure = make_exposure(level=Urgency.LOW, score=0.3, confidence=0.8)
        payload = build(
            engine,
            priority_engine,
            exposure=exposure,
            forecasts=[make_forecast(1.0)],
            impacts=[make_impact(severity=Urgency.LOW)],
        )
        for item in payload.of_type(PresentationType.EVENT_SUMMARY):
            assert item.priority <= exposure.exposure_score

    def test_forecast_is_marked_as_a_prediction(self, engine, priority_engine):
        payload = build(
            engine,
            priority_engine,
            forecasts=[make_forecast(0.9)],
            impacts=[make_impact()],
        )
        summaries = payload.of_type(PresentationType.EVENT_SUMMARY)
        assert summaries
        assert summaries[0].payload["is_prediction"] is True
        assert summaries[0].truth_status is TruthStatus.PREDICTED

    def test_low_probability_forecast_is_not_presented(self, engine, priority_engine):
        payload = build(
            engine,
            priority_engine,
            forecasts=[make_forecast(0.2)],
            impacts=[make_impact()],
        )
        assert payload.of_type(PresentationType.EVENT_SUMMARY) == []


class TestWhatChanged:
    def test_change_item_is_produced_from_deltas(self, engine, priority_engine):
        report = DeltaReport(
            deltas=(
                StateDelta(
                    change="user_route_became_impacted",
                    before=False,
                    after=True,
                    magnitude=0.9,
                    confidence=0.8,
                    causes=("road_closure_581",),
                ),
            ),
            magnitude=0.9,
            is_material=True,
        )
        payload = build(
            engine, priority_engine, impacts=[make_impact()], report=report
        )
        changes = payload.of_type(PresentationType.WHAT_CHANGED)
        assert len(changes) == 1
        assert changes[0].payload["change"] == "user_route_became_impacted"
        assert changes[0].payload["before"] is False
        assert changes[0].payload["after"] is True
        assert "road_closure_581" in changes[0].payload["causes"]

    def test_no_deltas_means_no_change_item(self, engine, priority_engine):
        payload = build(
            engine, priority_engine, impacts=[make_impact()], report=DeltaReport()
        )
        assert payload.of_type(PresentationType.WHAT_CHANGED) == []


class TestPayload:
    def test_matches_the_documented_section_37_shape(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        body = payload.as_dict()
        assert set(body) == {"event_id", "presentation_items"}
        for item in body["presentation_items"]:
            assert {"type", "priority", "headline", "confidence"} <= set(item)

    def test_of_type_filters(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        assert all(
            i.type is PresentationType.EVIDENCE
            for i in payload.of_type(PresentationType.EVIDENCE)
        )

    def test_interrupt_floor_is_respected_by_content_items(self, engine, priority_engine):
        payload = build(engine, priority_engine, impacts=[make_impact()])
        content = [i for i in payload.items if i.type is not PresentationType.QUIET]
        interrupting = [i for i in content if i.priority >= INTERRUPT_FLOOR]
        # Only low-priority context (evidence) may sit below the floor.
        for item in content:
            if item.priority < INTERRUPT_FLOOR:
                assert item.type is PresentationType.EVIDENCE
        assert interrupting or all(i.type is PresentationType.EVIDENCE for i in content)
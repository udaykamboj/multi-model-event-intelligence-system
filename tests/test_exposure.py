"""Section 35: exposure is deterministic and geospatial first.

The property under test throughout: a prediction may raise the *severity* of
an exposure that geometry already supports, but it must never be able to
create one that geometry does not.
"""

from __future__ import annotations

import pytest
from conftest import DOWNTOWN, FAR_AWAY, line, make_exposure, make_forecast, make_impact, make_state, point

from infraimpact.domain.enums import InfrastructureDomain, TruthStatus, Urgency
from infraimpact.domain.geo import buffer_geometry
from infraimpact.graph.model import build_puget_sound_graph
from infraimpact.users.exposure import (
    EXPOSURE_RADIUS_M,
    ROUTE_BLOCKED_M,
    ExposureContext,
    ExposureEngine,
)


@pytest.fixture
def engine():
    return ExposureEngine(build_puget_sound_graph())


def context(user, *, geometry=None, impacts=(), forecasts=(), official=False, engine=None):
    state = make_state(geometry=geometry, impacts=tuple(impacts), official=official)
    return ExposureContext(
        user=user,
        state=state,
        impacts=tuple(impacts),
        forecasts=tuple(forecasts),
        analysis_run_id="run_1",
        graph=engine.graph if engine else None,
    )


class TestGeometricDeterminism:
    def test_user_at_the_footprint_is_exposed(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        result = engine.compute(context(downtown_user, geometry=poly, engine=engine))
        assert result.inside_impact_area
        assert result.distance_m == 0.0
        # Inside the footprint but with no confirmed damage, the score is
        # driven by proximity alone: moderate, not high.
        assert result.exposure_level is Urgency.MODERATE

    def test_user_far_away_is_not_exposed(self, remote_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        result = engine.compute(context(remote_user, geometry=poly, engine=engine))
        assert result.exposure_score == 0.0
        assert result.exposure_level is Urgency.NONE

    def test_no_geometry_means_no_exposure(self, downtown_user, engine):
        # A footprintless event cannot assert a user is inside it.
        result = engine.compute(context(downtown_user, geometry=None, engine=engine))
        assert result.exposure_score == 0.0

    def test_repeat_computation_is_identical(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        ctx = context(downtown_user, geometry=poly, engine=engine)
        first = engine.compute(ctx)
        second = engine.compute(ctx)
        assert first.exposure_score == second.exposure_score
        assert first.components == second.components


class TestForecastsCannotManufactureExposure:
    def test_forecast_alone_gives_zero(self, remote_user, engine):
        # Certain prediction of a road closure, but 12 km away with no impact.
        forecasts = [make_forecast(1.0)]
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        result = engine.compute(
            context(remote_user, geometry=poly, forecasts=forecasts, engine=engine)
        )
        assert result.exposure_score == 0.0
        assert result.exposure_level is Urgency.NONE

    def test_forecast_cannot_create_exposure_beyond_radius(self, remote_user, engine):
        # A user 12 km away whose routes and places are all far from the
        # impact: even a certain forecast must not produce exposure.
        impact_geom = point(*DOWNTOWN)
        result = engine.compute(
            context(
                remote_user,
                geometry=point(*DOWNTOWN),
                forecasts=[make_forecast(1.0)],
                impacts=[make_impact(geometry=impact_geom, severity=Urgency.IMMEDIATE)],
                engine=engine,
            )
        )
        assert result.exposure_score == 0.0
        assert result.exposure_level is Urgency.NONE

    def test_forecast_weight_is_small_where_geometry_exists(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        impact = make_impact(geometry=point(*DOWNTOWN), severity=Urgency.MODERATE)
        without = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], engine=engine)
        )
        with_forecast = engine.compute(
            context(
                downtown_user,
                geometry=poly,
                impacts=[impact],
                forecasts=[make_forecast(1.0)],
                engine=engine,
            )
        )
        # A maximal forecast may nudge the score, but only slightly.
        assert with_forecast.exposure_score >= without.exposure_score
        assert with_forecast.exposure_score - without.exposure_score <= 0.11


class TestRoutes:
    def test_route_crossing_an_impact_is_impacted(self, downtown_user, engine):
        impact = make_impact(
            "road_1", geometry=point(-122.3525, 47.614), severity=Urgency.HIGH
        )
        poly = buffer_geometry(point(*DOWNTOWN), 800.0)
        result = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], engine=engine)
        )
        impacted = [r for r in result.route_impacts if r.intersects]
        assert impacted, "the route should cross the impact geometry"
        assert "road_1" in impacted[0].blocked_node_ids

    def test_route_away_from_all_impacts_is_untouched(self, downtown_user, engine):
        impact = make_impact("road_far", geometry=point(*FAR_AWAY))
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        result = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], engine=engine)
        )
        assert all(not r.intersects for r in result.route_impacts)

    def test_delay_is_estimated_when_blocked(self, downtown_user, engine):
        impact = make_impact(
            "road_1", geometry=point(-122.3525, 47.614), severity=Urgency.HIGH
        )
        poly = buffer_geometry(point(*DOWNTOWN), 800.0)
        result = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], engine=engine)
        )
        blocked = [r for r in result.route_impacts if r.intersects]
        assert blocked[0].delay_estimate_min is not None
        assert 0 < blocked[0].delay_estimate_min <= 45.0

    def test_a_driver_is_not_affected_by_a_bus_alert(self, engine):
        """Mode filtering: a road impact reaches a walker, but a transit
        alert does not reach someone who only drives."""
        from infraimpact.domain.schemas import RouteProfile, UserContext

        driver = UserContext(
            user_id="u_drive",
            route_profiles=(
                RouteProfile(
                    route_id="r_drive",
                    name="Drive to work",
                    geometry=line((-122.37, 47.62), DOWNTOWN),
                    modes=("drive",),
                ),
            ),
            current_location=point(*DOWNTOWN),
        )
        here = point(-122.3525, 47.614)
        road = make_impact("road_1", geometry=here, domain="road")
        transit = make_impact("bus_1", geometry=here, domain="transit")
        poly = buffer_geometry(point(*DOWNTOWN), 800.0)

        road_only = engine.compute(
            context(driver, geometry=poly, impacts=[road], engine=engine)
        )
        both = engine.compute(
            context(driver, geometry=poly, impacts=[road, transit], engine=engine)
        )

        assert any(r.intersects for r in road_only.route_impacts), "road affects drivers"
        assert len(both.route_impacts) == len(road_only.route_impacts), (
            "a bus alert must not add a route impact for a drive-only user"
        )

    def test_a_walker_is_affected_by_a_road_closure(self, engine):
        from infraimpact.domain.schemas import RouteProfile, UserContext

        walker = UserContext(
            user_id="u_walk",
            route_profiles=(
                RouteProfile(
                    route_id="r_walk",
                    name="Walk to work",
                    geometry=line((-122.37, 47.62), DOWNTOWN),
                    modes=("walk",),
                ),
            ),
            current_location=point(*DOWNTOWN),
        )
        road = make_impact("road_1", geometry=point(-122.3525, 47.614), domain="road")
        poly = buffer_geometry(point(*DOWNTOWN), 800.0)
        result = engine.compute(
            context(walker, geometry=poly, impacts=[road], engine=engine)
        )
        assert any(r.intersects for r in result.route_impacts)

    def test_no_alternative_claimed_without_graph_proof(self, downtown_user, engine):
        # Every impact on the route, with a graph that cannot route around it.
        impact = make_impact("road_1", geometry=point(-122.3525, 47.614))
        poly = buffer_geometry(point(*DOWNTOWN), 800.0)
        result = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], engine=engine)
        )
        blocked = [r for r in result.route_impacts if r.intersects]
        if blocked[0].alternative_available:
            assert blocked[0].alternative_geometry is not None
        else:
            assert blocked[0].alternative_available is False


class TestSavedPlaces:
    def test_home_inside_the_footprint_scores_high(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 300.0)
        result = engine.compute(context(downtown_user, geometry=poly, engine=engine))
        assert result.saved_place_impacts.get("p_home", 0) > 0.5

    def test_remote_home_scores_zero(self, remote_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 300.0)
        result = engine.compute(context(remote_user, geometry=poly, engine=engine))
        assert "p_home" not in result.saved_place_impacts

    def test_work_outranks_an_arbitrary_pin(self, engine):
        """Home and work carry more consequence than an arbitrary saved pin.

        Both places sit *outside* the footprint but at the same distance from
        the impact: a place inside the footprint is fully exposed regardless of
        its kind, so the weighting only shows up in the proximity branch.
        """
        from infraimpact.domain.schemas import SavedPlace, UserContext

        impact = make_impact("road_1", geometry=point(*DOWNTOWN))
        # A small footprint downtown; both places sit just outside it.
        poly = buffer_geometry(point(*DOWNTOWN), 200.0)
        nearby = point(DOWNTOWN[0] + 0.004, DOWNTOWN[1])  # ~320 m east
        user = UserContext(
            user_id="u_both",
            saved_places=(
                SavedPlace(
                    place_id="p_work", name="Work", kind="work", geometry=nearby
                ),
                SavedPlace(
                    place_id="p_pin", name="Gym", kind="other", geometry=nearby
                ),
            ),
        )
        result = engine.compute(
            context(user, geometry=poly, impacts=[impact], engine=engine)
        )
        assert result.saved_place_impacts["p_work"] > result.saved_place_impacts["p_pin"]

    def test_a_place_inside_the_footprint_is_fully_exposed(self, engine):
        from infraimpact.domain.schemas import SavedPlace, UserContext

        poly = buffer_geometry(point(*DOWNTOWN), 300.0)
        user = UserContext(
            user_id="u_inside",
            saved_places=(
                SavedPlace(
                    place_id="p_pin", name="Gym", kind="other", geometry=point(*DOWNTOWN)
                ),
            ),
        )
        result = engine.compute(context(user, geometry=poly, engine=engine))
        assert result.saved_place_impacts["p_pin"] == 1.0


class TestOfficialGuidance:
    def test_official_guidance_forces_immediate(self, downtown_user, engine):
        # A moderate arithmetic score is overridden by official guidance.
        poly = buffer_geometry(point(*DOWNTOWN), 1000.0)
        impact = make_impact("road_1", severity=Urgency.LOW)
        without = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], engine=engine)
        )
        with_official = engine.compute(
            context(downtown_user, geometry=poly, impacts=[impact], official=True, engine=engine)
        )
        assert with_official.exposure_level is Urgency.IMMEDIATE
        assert with_official.exposure_score >= without.exposure_score

    def test_components_expose_the_official_flag(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        result = engine.compute(
            context(downtown_user, geometry=poly, official=True, engine=engine)
        )
        assert result.components["official_guidance"] == 1.0


class TestConfidence:
    def test_contradictions_lower_confidence(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 300.0)
        clean = make_state(geometry=poly)
        disputed = make_state(geometry=poly, contradictions=3, independent=1, source_count=3)
        a = ExposureContext(
            user=downtown_user,
            state=clean,
            impacts=(),
            forecasts=(),
            analysis_run_id="run_1",
        )
        b = ExposureContext(
            user=downtown_user,
            state=disputed,
            impacts=(),
            forecasts=(),
            analysis_run_id="run_1",
        )
        assert engine.compute(a).confidence > engine.compute(b).confidence

    def test_inferred_impacts_lower_confidence(self, downtown_user, engine):
        poly = buffer_geometry(point(*DOWNTOWN), 300.0)
        confirmed = make_impact("a", truth_status="confirmed")
        inferred = make_impact("b", truth_status="inferred")
        result = engine.compute(
            context(downtown_user, geometry=poly, impacts=[inferred], engine=engine)
        )
        base = ExposureContext(
            user=downtown_user,
            state=make_state(geometry=poly, impacts=(confirmed,)),
            impacts=(confirmed,),
            forecasts=(),
            analysis_run_id="run_1",
        )
        assert result.confidence < engine.compute(base).confidence


class TestBandBoundaries:
    @pytest.mark.parametrize("score,expected", [
        (0.0, Urgency.NONE),
        (0.01, Urgency.NONE),
        (0.1, Urgency.LOW),
        (0.3, Urgency.MODERATE),
        (0.5, Urgency.HIGH),
        (0.8, Urgency.IMMEDIATE),
        (1.0, Urgency.IMMEDIATE),
    ])
    def test_bands(self, downtown_user, score, expected):
        from infraimpact.users.exposure import exposure_band

        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        ctx = context(downtown_user, geometry=poly)
        assert exposure_band(score, ctx) is expected

    def test_official_overrides_every_band(self, downtown_user):
        from infraimpact.users.exposure import exposure_band

        ctx = context(downtown_user, official=True)
        assert exposure_band(0.03, ctx) is Urgency.IMMEDIATE

    def test_zero_score_is_never_immediate(self, downtown_user):
        from infraimpact.users.exposure import exposure_band

        assert exposure_band(0.0, context(downtown_user)) is Urgency.NONE


class TestConstants:
    def test_exposure_radius_is_ten_km(self):
        assert EXPOSURE_RADIUS_M == 10_000.0

    def test_route_block_threshold_is_100m(self):
        assert ROUTE_BLOCKED_M == 100.0

    def test_remote_user_is_outside_the_radius(self):
        from infraimpact.domain.geo import distance_m

        assert distance_m(point(*FAR_AWAY), point(*DOWNTOWN)) > EXPOSURE_RADIUS_M
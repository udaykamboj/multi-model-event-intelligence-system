"""Geometry is the semantic contract PostGIS ST_* must satisfy (domain/geo.py).

These tests pin the pure-Python reference behaviour. Any PostGIS driver is
required to agree with them.
"""

from __future__ import annotations

import pytest
from conftest import DOWNTOWN, FAR_AWAY, line, point

from infraimpact.domain.geo import (
    buffer_geometry,
    centroid_of,
    contains,
    distance_m,
    geometry_area_m2,
    haversine_m,
    intersects,
)


class TestPointGeometry:
    def test_point_to_point_distance(self):
        # Two points must measure as the great-circle distance between them
        # rather than falling through to infinity on an empty segment set.
        assert distance_m(point(*DOWNTOWN), point(*FAR_AWAY)) == pytest.approx(
            12049.3, rel=0.01
        )

    def test_haversine_is_the_underlying_measure(self):
        assert haversine_m(DOWNTOWN, FAR_AWAY) == pytest.approx(12049.3, rel=0.01)

    def test_point_to_itself_is_zero(self):
        assert distance_m(point(*DOWNTOWN), point(*DOWNTOWN)) == 0.0

    def test_distance_is_symmetric(self):
        a, b = point(*DOWNTOWN), point(*FAR_AWAY)
        assert distance_m(a, b) == pytest.approx(distance_m(b, a), rel=1e-9)

    def test_missing_geometry_is_infinite_not_zero(self):
        # A missing geometry must never masquerade as "you are affected".
        assert distance_m(point(*DOWNTOWN), None) == float("inf")
        assert distance_m(None, point(*DOWNTOWN)) == float("inf")


class TestPolygon:
    @pytest.fixture
    def poly(self):
        return buffer_geometry(point(*DOWNTOWN), 500.0)

    def test_centre_is_inside(self, poly):
        assert contains(poly, DOWNTOWN)

    def test_far_point_is_outside(self, poly):
        assert not contains(poly, FAR_AWAY)

    def test_centre_distance_is_zero(self, poly):
        assert distance_m(point(*DOWNTOWN), poly) == 0.0

    def test_outside_distance_exceeds_radius(self, poly):
        assert distance_m(poly, point(*FAR_AWAY)) > 500.0

    def test_area_is_positive_and_plausible(self, poly):
        # A 500 m buffer is roughly 785,000 m2.
        assert 500_000 < geometry_area_m2(poly) < 1_100_000

    def test_centroid_is_near_the_source(self, poly):
        cx, cy = centroid_of(poly)
        assert abs(cx - DOWNTOWN[0]) < 1e-3
        assert abs(cy - DOWNTOWN[1]) < 1e-3


class TestLineString:
    def test_endpoint_is_on_the_line(self):
        geom = line((-122.37, 47.62), DOWNTOWN)
        assert contains(geom, DOWNTOWN)

    def test_interior_point_is_on_the_line(self):
        geom = line((-122.37, 47.62), DOWNTOWN)
        assert contains(geom, (-122.3525, 47.614))

    def test_distant_point_is_not_on_the_line(self):
        geom = line((-122.37, 47.62), DOWNTOWN)
        assert not contains(geom, FAR_AWAY)

    def test_line_to_far_point(self):
        geom = line((-122.37, 47.62), DOWNTOWN)
        assert distance_m(geom, point(*FAR_AWAY)) > 1000.0

    def test_line_intersects_its_own_buffer(self):
        geom = line((-122.37, 47.62), DOWNTOWN)
        assert intersects(geom, buffer_geometry(point(*DOWNTOWN), 500.0))

    def test_far_line_does_not_intersect(self):
        geom = line((-122.37, 47.62), DOWNTOWN)
        assert not intersects(geom, buffer_geometry(point(*FAR_AWAY), 500.0))


class TestIntersects:
    def test_point_inside_polygon(self):
        assert intersects(point(*DOWNTOWN), buffer_geometry(point(*DOWNTOWN), 500.0))

    def test_point_outside_polygon(self):
        assert not intersects(point(*DOWNTOWN), buffer_geometry(point(*FAR_AWAY), 500.0))

    def test_crossing_lines_intersect(self):
        a = line((-122.4, 47.608), (-122.3, 47.608))
        b = line((-122.335, 47.50), (-122.335, 47.70))
        assert intersects(a, b)

    def test_parallel_lines_do_not(self):
        a = line((-122.4, 47.608), (-122.3, 47.608))
        b = line((-122.4, 47.700), (-122.3, 47.700))
        assert not intersects(a, b)

    def test_empty_inputs_are_false_not_errors(self):
        assert not intersects(None, point(*DOWNTOWN))
        assert not intersects(point(*DOWNTOWN), None)


class TestRecursionSafety:
    def test_line_containment_terminates(self):
        # contains() on a LineString must not call back into itself through
        # distance_to_geometry_m. Regression guard for a real infinite recursion.
        geom = line((-122.37, 47.62), DOWNTOWN, (-122.30, 47.60))
        assert contains(geom, DOWNTOWN) in (True, False)

    def test_repeated_distance_calls_are_stable(self):
        poly = buffer_geometry(point(*DOWNTOWN), 500.0)
        first = distance_m(poly, point(*FAR_AWAY))
        for _ in range(50):
            assert distance_m(poly, point(*FAR_AWAY)) == first
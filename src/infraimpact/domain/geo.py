"""Pure-Python geometry.

Deliberately dependency-free so the platform runs anywhere. Production scales
this out to PostGIS (``ST_Intersects``, ``ST_DWithin``, ``ST_Buffer``,
``ST_Union`` per section 28); the semantics implemented here are the contract
those functions must satisfy.

Geometry representation is GeoJSON-shaped, not a bespoke class:
    Point      -> {"type": "Point", "coordinates": [lon, lat]}
    LineString -> {"type": "LineString", "coordinates": [[lon, lat], ...]}
    Polygon    -> {"type": "Polygon", "coordinates": [[[lon, lat], ...], ...]}
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8

Geometry = dict[str, Any]
LonLat = tuple[float, float]


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


def haversine_m(a: LonLat, b: LonLat) -> float:
    """Great-circle distance in metres."""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bbox_of(geometry: Geometry | None) -> tuple[float, float, float, float] | None:
    """Return (min_lon, min_lat, max_lon, max_lat) or None if empty."""
    if not geometry:
        return None
    pts = list(_iter_positions(geometry))
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons), min(lats), max(lons), max(lats))


def union_bbox(geometries: Sequence[Geometry | None]) -> tuple[float, float, float, float] | None:
    """Bounding box covering every input geometry."""
    boxes = [b for b in (bbox_of(g) for g in geometries) if b is not None]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def centroid_of(geometry: Geometry | None) -> LonLat | None:
    if not geometry:
        return None
    pts = list(_iter_positions(geometry))
    if not pts:
        return None
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def geometry_area_m2(geometry: Geometry | None) -> float:
    """Planar approximation via local equirectangular projection.

    Adequate for city-scale footprints; not for cadastral work.
    """
    if not geometry:
        return 0.0
    ring = _outer_ring(geometry)
    if ring is None or len(ring) < 3:
        return 0.0
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    kx = EARTH_RADIUS_M * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_RADIUS_M * math.pi / 180.0
    pts = [(p[0] * kx, p[1] * ky) for p in ring]
    area = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def distance_to_geometry_m(point: LonLat, geometry: Geometry | None) -> float:
    """0 if inside, else metres to the nearest boundary vertex or segment."""
    if not geometry:
        return float("inf")
    if contains(geometry, point):
        return 0.0
    best = float("inf")
    for line in _iter_lines(geometry):
        if len(line) == 1:
            # Degenerate line (a Point): measure straight to the vertex.
            best = min(best, haversine_m(point, line[0]))
            continue
        for seg in zip(line, line[1:]):
            d = _point_segment_distance_m(point, seg[0], seg[1])
            if d < best:
                best = d
    return best


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------


def contains(geometry: Geometry | None, point: LonLat) -> bool:
    if not geometry:
        return False
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point":
        return haversine_m(point, (coords[0], coords[1])) <= 1.0
    if gtype in ("Polygon", "MultiPolygon"):
        polys = [coords] if gtype == "Polygon" else coords
        for poly in polys:
            if not poly:
                continue
            if _point_in_ring(point, poly[0]):
                holes = poly[1:]
                if not any(_point_in_ring(point, h) for h in holes if h):
                    return True
        return False
    if gtype == "LineString":
        # A point within 1 m of a line is "on" it. Measured directly from the
        # segments rather than via distance_to_geometry_m, which would call
        # back into contains() and recurse forever.
        for line in _iter_lines(geometry):
            for seg in zip(line, line[1:]):
                if _point_segment_distance_m(point, seg[0], seg[1]) <= 1.0:
                    return True
        return False
    return False


def intersects(a: Geometry | None, b: Geometry | None) -> bool:
    """True when a and b share space. Conservative for point/line cases."""
    if not a or not b:
        return False
    if contains(a, _first_position(b)) or contains(b, _first_position(a)):
        return True
    for line_a in _iter_lines(a):
        for line_b in _iter_lines(b):
            for p in line_a:
                if contains(b, p):
                    return True
            for p in line_b:
                if contains(a, p):
                    return True
            for seg_a in zip(line_a, line_a[1:]):
                for seg_b in zip(line_b, line_b[1:]):
                    if _segments_intersect(seg_a[0], seg_a[1], seg_b[0], seg_b[1]):
                        return True
    return False


def distance_m(a: Geometry | None, b: Geometry | None) -> float:
    if not a or not b:
        return float("inf")
    if intersects(a, b):
        return 0.0
    best = float("inf")
    for pa in _iter_positions(a):
        d = distance_to_geometry_m(pa, b)
        if d < best:
            best = d
    for pb in _iter_positions(b):
        d = distance_to_geometry_m(pb, a)
        if d < best:
            best = d
    return best


def buffer_geometry(geometry: Geometry | None, radius_m: float) -> Geometry | None:
    """Approximate buffer: a convex-ish polygon around the input vertices.

    This is a screening primitive for "is the user's location anywhere near
    this?", not a cartographic buffer. In production PostGIS ``ST_Buffer`` is
    authoritative; this only needs to be conservative (never under-estimate).
    """
    if not geometry or radius_m <= 0:
        return geometry
    centre = centroid_of(geometry)
    if centre is None:
        return None
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(centre[1])) or 1e-9))
    ring: list[list[float]] = []
    steps = 24
    for i in range(steps):
        theta = 2 * math.pi * i / steps
        ring.append([centre[0] + dlon * math.cos(theta), centre[1] + dlat * math.sin(theta)])
    return {"type": "Polygon", "coordinates": [ring]}


# --------------------------------------------------------------------------
# constructors
# --------------------------------------------------------------------------


def point(lon: float, lat: float) -> Geometry:
    return {"type": "Point", "coordinates": [float(lon), float(lat)]}


def line_string(coords: Sequence[Sequence[float]]) -> Geometry:
    return {"type": "LineString", "coordinates": [[float(c[0]), float(c[1])] for c in coords]}


def polygon(ring: Sequence[Sequence[float]]) -> Geometry:
    return {"type": "Polygon", "coordinates": [[[float(c[0]), float(c[1])] for c in ring]]}


def bbox_polygon(bounds: tuple[float, float, float, float]) -> Geometry:
    min_lon, min_lat, max_lon, max_lat = bounds
    return polygon(
        [
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]
    )


def empty_geometry() -> Geometry:
    return {"type": "Point", "coordinates": [0.0, 0.0]}


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _iter_positions(geometry: Geometry) -> Iterable[LonLat]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point":
        yield (float(coords[0]), float(coords[1]))
    elif gtype == "LineString":
        for c in coords:
            yield (float(c[0]), float(c[1]))
    elif gtype == "Polygon":
        for ring in coords:
            for c in ring:
                yield (float(c[0]), float(c[1]))
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    yield (float(c[0]), float(c[1]))
    elif gtype == "MultiPoint":
        for c in coords:
            yield (float(c[0]), float(c[1]))


def _iter_lines(geometry: Geometry) -> list[list[LonLat]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    out: list[list[LonLat]] = []
    if gtype == "Point":
        # A point is a zero-length line. Yielding it keeps point-to-point
        # distance from falling through to infinity.
        out.append([(float(coords[0]), float(coords[1]))])
    elif gtype == "MultiPoint":
        out.extend([(float(c[0]), float(c[1]))] for c in coords)
    elif gtype == "LineString":
        out.append([(float(c[0]), float(c[1])) for c in coords])
    elif gtype == "Polygon":
        for ring in coords:
            out.append([(float(c[0]), float(c[1])) for c in ring])
    elif gtype == "MultiLineString":
        for line in coords:
            out.append([(float(c[0]), float(c[1])) for c in line])
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                out.append([(float(c[0]), float(c[1])) for c in ring])
    return out


def _outer_ring(geometry: Geometry) -> list[LonLat] | None:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and coords:
        return [(float(c[0]), float(c[1])) for c in coords[0]]
    if gtype == "MultiPolygon" and coords:
        return [(float(c[0]), float(c[1])) for c in coords[0][0]]
    if gtype == "LineString":
        return [(float(c[0]), float(c[1])) for c in coords]
    return None


def _first_position(geometry: Geometry) -> LonLat:
    for p in _iter_positions(geometry):
        return p
    return (0.0, 0.0)


def _point_in_ring(point: LonLat, ring: Sequence[Sequence[float]]) -> bool:
    """Ray casting. Boundary counts as inside."""
    x, y = point
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        # boundary check
        if _point_on_segment(point, (x1, y1), (x2, y2)):
            return True
        if (y1 > y) != (y2 > y):
            x_at = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_at:
                inside = not inside
    return inside


def _point_on_segment(p: LonLat, a: LonLat, b: LonLat, tol_m: float = 1.0) -> bool:
    if haversine_m(p, a) <= tol_m or haversine_m(p, b) <= tol_m:
        return True
    return _point_segment_distance_m(p, a, b) <= tol_m


def _point_segment_distance_m(p: LonLat, a: LonLat, b: LonLat) -> float:
    """Local equirectangular projection; accurate at city scale."""
    lat0 = math.radians((p[1] + a[1] + b[1]) / 3.0)
    kx = EARTH_RADIUS_M * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_RADIUS_M * math.pi / 180.0
    px, py = p[0] * kx, p[1] * ky
    ax, ay = a[0] * kx, a[1] * ky
    bx, by = b[0] * kx, b[1] * ky
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a: LonLat, b: LonLat, c: LonLat) -> float:
    lat0 = math.radians(a[1])
    kx = EARTH_RADIUS_M * math.cos(lat0) * math.pi / 180.0
    ky = EARTH_RADIUS_M * math.pi / 180.0
    ax, ay = a[0] * kx, a[1] * ky
    bx, by = b[0] * kx, b[1] * ky
    cx, cy = c[0] * kx, c[1] * ky
    return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)


def _segments_intersect(a1: LonLat, a2: LonLat, b1: LonLat, b2: LonLat) -> bool:
    d1 = _orient(a1, a2, b1)
    d2 = _orient(a1, a2, b2)
    d3 = _orient(b1, b2, a1)
    d4 = _orient(b1, b2, a2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False
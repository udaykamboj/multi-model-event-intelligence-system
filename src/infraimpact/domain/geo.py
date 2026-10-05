"""Geometry (shapely / GEOS backed).

Predicates, distances, areas and buffers are computed by shapely (the GEOS
engine PostGIS itself uses) in a local metric projection, instead of the
hand-written ray-casting/segment code this module used to carry. Production can
still scale out to PostGIS (``ST_Intersects``, ``ST_DWithin``, ``ST_Buffer``,
``ST_Union`` per section 28); semantics here are the same.

Geometry stays GeoJSON-shaped dicts so storage and the API are unchanged.

Geometry representation is GeoJSON-shaped, not a bespoke class:
    Point      -> {"type": "Point", "coordinates": [lon, lat]}
    LineString -> {"type": "LineString", "coordinates": [[lon, lat], ...]}
    Polygon    -> {"type": "Polygon", "coordinates": [[[lon, lat], ...], ...]}
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

from shapely.geometry import Point as _SPoint, mapping as _mapping, shape as _shape
from shapely.ops import transform as _transform

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
    """Area in square metres (GEOS, local equirectangular projection)."""
    g = _project(geometry)
    return float(g.area) if g is not None else 0.0


def distance_to_geometry_m(point: LonLat, geometry: Geometry | None) -> float:
    """0 if inside, else metres to the nearest part of ``geometry``."""
    if not geometry:
        return float("inf")
    lat0 = _lat0(geometry, {"type": "Point", "coordinates": list(point)})
    g = _project(geometry, lat0)
    if g is None:
        return float("inf")
    return float(g.distance(_project({"type": "Point", "coordinates": list(point)}, lat0)))


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------

#: Features within this distance count as touching (GPS/geometry tolerance).
TOUCH_TOLERANCE_M = 1.0


def contains(geometry: Geometry | None, point: LonLat) -> bool:
    if not geometry:
        return False
    return distance_to_geometry_m(point, geometry) <= TOUCH_TOLERANCE_M


def intersects(a: Geometry | None, b: Geometry | None) -> bool:
    """True when a and b share space (within the touch tolerance)."""
    return distance_m(a, b) <= TOUCH_TOLERANCE_M


def distance_m(a: Geometry | None, b: Geometry | None) -> float:
    if not a or not b:
        return float("inf")
    lat0 = _lat0(a, b)
    ga, gb = _project(a, lat0), _project(b, lat0)
    if ga is None or gb is None:
        return float("inf")
    return float(ga.distance(gb))


def buffer_geometry(geometry: Geometry | None, radius_m: float) -> Geometry | None:
    """True GEOS buffer of the geometry (not a circle around its centroid)."""
    if not geometry or radius_m <= 0:
        return geometry
    lat0 = _lat0(geometry)
    g = _project(geometry, lat0)
    if g is None:
        return None
    return _unproject(g.buffer(radius_m, quad_segs=8), lat0)


# --------------------------------------------------------------------------
# constructors
# --------------------------------------------------------------------------


def point(lon: float, lat: float) -> Geometry:
    return {"type": "Point", "coordinates": [float(lon), float(lat)]}


def line_string(coords: Sequence[Sequence[float]]) -> Geometry:
    return {"type": "LineString", "coordinates": [[float(c[0]), float(c[1])] for c in coords]}


def line(*coords: Sequence[float]) -> Geometry:
    return line_string(coords)


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


def _first_position(geometry: Geometry) -> LonLat:
    for p in _iter_positions(geometry):
        return p
    return (0.0, 0.0)


def _lat0(*geometries: Geometry | None) -> float:
    lats = [p[1] for g in geometries if g for p in _iter_positions(g)]
    return sum(lats) / len(lats) if lats else 0.0


def _factors(lat0: float) -> tuple[float, float]:
    ky = EARTH_RADIUS_M * math.pi / 180.0
    return ky * math.cos(math.radians(lat0)), ky


def _project(geometry: Geometry | None, lat0: float | None = None):
    """GeoJSON dict -> shapely geometry in local metres, or None if unusable."""
    if not geometry:
        return None
    try:
        g = _shape(geometry)
    except Exception:  # noqa: BLE001 - malformed geometry is "no geometry"
        return None
    if g.is_empty:
        return None
    kx, ky = _factors(_lat0(geometry) if lat0 is None else lat0)
    return _transform(lambda x, y, z=None: (x * kx, y * ky), g)


def _unproject(g, lat0: float) -> Geometry:
    kx, ky = _factors(lat0)
    back = _transform(lambda x, y, z=None: (x / kx, y / ky), g)
    return _lists(_mapping(back))


def _lists(obj: Any) -> Any:
    if isinstance(obj, (tuple, list)):
        return [_lists(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _lists(v) for k, v in obj.items()}
    return obj

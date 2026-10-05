"""Magnitude, rate and duration: how big, how fast, how long.

An event that the platform can locate but not size is only half-reconstructed.
A crowd that has grown from 300 to 3,000, a wind that has risen from 25 to 60
mph, a burn area that has gone from 40 to 900 acres - each of these is a change
in the world that matters, and none of them is visible in a footprint polygon or
a count of observations.

So the world-state engine measures three properties of every event:

    magnitude   how big is it, with what uncertainty, according to whom
    rate        how fast is information about it arriving, and is that changing
    duration    how long has it been going, and how long since anything was said

None of the three is event-type specific, and that is the point. There is no
crowd extractor and no wind extractor and no burn-area extractor; there is one
extractor that reads whatever numeric quantities a source chose to publish, and
a declarative table that says which payload keys mean what physical quantity.
Wildfire acreage, wind speed and earthquake magnitude are three rows in that
table. A new source that reports containment percentage is one more row. Neither
requires a new code path, and neither creates a ``wildfire_flow()``.

Two decisions shape the rest of the module.

First, quantities are read *quantitatively*. Two sources reporting 300 and 700
do not get averaged into a confident 500. The disagreement is measured, widened
into the interval, and reported. A mid-estimate with a wide interval is the
honest answer, and it is a genuinely different object from a point estimate,
which is why :class:`~infraimpact.domain.schemas.QuantityEstimate` carries
``lower``/``upper``/``disagreement`` rather than just a value.

Second, an unmeasured quantity is *absent*, never zero. An event whose sources
report no size gets ``primary=None``. Writing ``0.0`` would assert that nothing
is happening there, which is a claim about the world that no observation
supports, and it would propagate into every downstream score.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..domain.enums import Authority
from ..domain.ids import ensure_utc
from ..domain.schemas import (
    EventDuration,
    EventRate,
    EventScale,
    Observation,
    QuantityEstimate,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Unit handling
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitDefinition:
    """One unit within a physical dimension."""

    symbol: str
    #: Multiplier converting a value in this unit to the dimension's canonical
    #: unit. ``None`` means the dimension has no canonical unit and values are
    #: carried through untouched.
    to_canonical: float | None = None
    #: For temperature, an affine conversion cannot be a multiplier.
    offset: float = 0.0
    aliases: tuple[str, ...] = ()


#: Canonical units per dimension. Chosen to be the unit each dimension is most
#: commonly published in, so conversion is usually a no-op.
DIMENSIONS: dict[str, UnitDefinition] = {
    "count": UnitDefinition("count"),
    "length": UnitDefinition("m", aliases=("km", "kilometer", "kilometers")),
    "speed": UnitDefinition("mph", aliases=("km/h", "kph", "kmh", "knot", "kt", "m/s", "ms")),
    "temperature": UnitDefinition("F", aliases=("C", "celsius", "centigrade", "fahrenheit")),
    "time": UnitDefinition("min", aliases=("minutes", "mins", "second", "seconds", "s", "sec", "hour", "hours", "h")),
    "ratio": UnitDefinition("percent", aliases=("pct", "%", "fraction")),
    "area": UnitDefinition("acres", aliases=("acre", "hectares", "ha", "sq_mi", "sqmi")),
    "pressure": UnitDefinition("mb", aliases=("hpa", "mbar", "millibar", "inhg", "in_hg")),
    "energy": UnitDefinition("mj", aliases=("kwh", "mwh", "j", "joules")),
    "depth": UnitDefinition("ft", aliases=("feet", "foot", "m", "meters", "metres")),
}

#: Explicit unit conversions. Keyed by lowercased unit token. Anything absent
#: is treated as already being in the dimension's canonical unit, which is the
#: right default for feeds that publish bare numbers.
UNIT_CONVERSIONS: dict[str, tuple[str, float, float]] = {
    # length -> m
    "km": ("length", 1000.0, 0.0),
    "kilometer": ("length", 1000.0, 0.0),
    "kilometers": ("length", 1000.0, 0.0),
    "m": ("length", 1.0, 0.0),
    "meters": ("length", 1.0, 0.0),
    "metres": ("length", 1.0, 0.0),
    "ft": ("length", 0.3048, 0.0),
    "feet": ("length", 0.3048, 0.0),
    "foot": ("length", 0.3048, 0.0),
    "mi": ("length", 1609.344, 0.0),
    "miles": ("length", 1609.344, 0.0),
    # speed -> mph
    "km/h": ("speed", 0.621371, 0.0),
    "kph": ("speed", 0.621371, 0.0),
    "kmh": ("speed", 0.621371, 0.0),
    "kts": ("speed", 1.15078, 0.0),
    "knot": ("speed", 1.15078, 0.0),
    "kt": ("speed", 1.15078, 0.0),
    "knots": ("speed", 1.15078, 0.0),
    "m/s": ("speed", 2.23694, 0.0),
    "ms": ("speed", 2.23694, 0.0),
    # time -> minutes
    "s": ("time", 1 / 60.0, 0.0),
    "sec": ("time", 1 / 60.0, 0.0),
    "second": ("time", 1 / 60.0, 0.0),
    "seconds": ("time", 1 / 60.0, 0.0),
    "h": ("time", 60.0, 0.0),
    "hour": ("time", 60.0, 0.0),
    "hours": ("time", 60.0, 0.0),
    "minutes": ("time", 1.0, 0.0),
    "mins": ("time", 1.0, 0.0),
    # ratio -> percent
    "pct": ("ratio", 100.0, 0.0),
    "%": ("ratio", 100.0, 0.0),
    "fraction": ("ratio", 100.0, 0.0),
    # area -> acres
    "hectares": ("area", 2.47105, 0.0),
    "ha": ("area", 2.47105, 0.0),
    "sq_mi": ("area", 640.0, 0.0),
    "sqmi": ("area", 640.0, 0.0),
    # pressure -> mb
    "hpa": ("pressure", 1.0, 0.0),
    "mbar": ("pressure", 1.0, 0.0),
    "millibar": ("pressure", 1.0, 0.0),
    "inhg": ("pressure", 33.8639, 0.0),
    "in_hg": ("pressure", 33.8639, 0.0),
    # energy -> mj
    "kwh": ("energy", 0.0036, 0.0),
    "mwh": ("energy", 3.6, 0.0),
    "j": ("energy", 1e-6, 0.0),
    "joules": ("energy", 1e-6, 0.0),
    # depth -> ft
    "meters": ("depth", 3.28084, 0.0),
    "metres": ("depth", 3.28084, 0.0),
}

#: Temperature is affine, so it gets its own conversion.
_TEMPERATURE_CONVERSIONS = {
    "c": (9 / 5.0, 32.0),
    "celsius": (9 / 5.0, 32.0),
    "centigrade": (9 / 5.0, 32.0),
    "f": (1.0, 0.0),
    "fahrenheit": (1.0, 0.0),
}


def canonical_unit(dimension: str, unit: str | None) -> tuple[str, float, float]:
    """Resolve a published unit to ``(canonical_unit, multiplier, offset)``."""

    canonical = DIMENSIONS.get(dimension, UnitDefinition("")).symbol
    if not unit:
        return canonical, 1.0, 0.0
    token = str(unit).strip().lower()

    if dimension == "temperature":
        if token in _TEMPERATURE_CONVERSIONS:
            multiplier, offset = _TEMPERATURE_CONVERSIONS[token]
            return "F", multiplier, offset
        return "F", 1.0, 0.0

    conversion = UNIT_CONVERSIONS.get(token)
    if conversion is None:
        return canonical, 1.0, 0.0
    target_dimension, multiplier, offset = conversion
    target_canonical = DIMENSIONS.get(target_dimension, UnitDefinition("")).symbol
    return target_canonical or canonical, multiplier, offset


# --------------------------------------------------------------------------
# Quantity specifications
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QuantitySpec:
    """Maps payload keys to a physical quantity.

    ``field_patterns`` are matched case-insensitively as substrings of the
    payload key, so ``estimated_people``, ``people_estimate`` and
    ``crowdSize`` all resolve without enumerating spelling variants. The
    ordering of ``field_patterns`` does not matter; the ordering of *specs* does,
    because ``priority`` decides which quantity becomes an event's ``primary``.

    ``priority`` is not about importance to any user. It is about how much the
    quantity says about the event itself: a headline count of people present
    characterises the event, a reported delay characterises its consequences.
    """

    quantity: str
    dimension: str
    field_patterns: tuple[str, ...]
    unit_aliases: tuple[str, ...] = ()
    priority: int = 50
    #: When True the value is a magnitude of the event itself; when False it is
    #: a property of its consequences. Used only for reporting clarity.
    intrinsic: bool = True


#: The quantity universe. Every row is a physical quantity that a real feed can
#: report about any kind of event; none of them names an event type.
QUANTITY_SPECS: tuple[QuantitySpec, ...] = (
    # -- how many people / things are involved ----------------------------
    QuantitySpec(
        quantity="people",
        dimension="count",
        field_patterns=(
            "people", "person", "crowd", "participant", "attendee",
            "protester", "demonstrator", "occupancy", "headcount", "size_estimate",
        ),
        unit_aliases=("count", "people", "persons", "individuals"),
        priority=10,
    ),
    # -- physical severity magnitudes -------------------------------------
    QuantitySpec(
        quantity="magnitude",
        dimension="ratio",
        field_patterns=("magnitude", "mag", "richter", "prefmag", "intensity_value"),
        priority=8,
    ),
    QuantitySpec(
        quantity="fema_intensity",
        dimension="ratio",
        field_patterns=("cdom", "fema_intensity", "fema_cdom"),
        priority=12,
    ),
    QuantitySpec(
        quantity="wind_speed",
        dimension="speed",
        field_patterns=("wind_speed", "windspeed", "wind_gust", "gust_speed", "wind"),
        unit_aliases=("mph", "km/h", "kts", "kt", "knots", "m/s"),
        priority=15,
    ),
    QuantitySpec(
        quantity="burned_area",
        dimension="area",
        field_patterns=(
            "acres_burned", "acresburned", "burned_acres", "acres",
            "hectares_burned", "burned_hectares",
        ),
        unit_aliases=("acres", "acre", "ha", "hectares"),
        priority=14,
    ),
    QuantitySpec(
        quantity="water_depth",
        dimension="depth",
        field_patterns=("water_depth", "flood_depth", "depth_of_water", "stage", "water_level"),
        unit_aliases=("ft", "feet", "m"),
        priority=16,
    ),
    QuantitySpec(
        quantity="precipitation",
        dimension="depth",
        field_patterns=("precipitation", "rainfall", "rain_amount", "snowfall", "snow_total"),
        unit_aliases=("in", "inches", "mm", "cm"),
        priority=18,
    ),
    QuantitySpec(
        quantity="temperature",
        dimension="temperature",
        field_patterns=("temperature", "temp_value", "temp_c", "temp_f"),
        unit_aliases=("F", "C"),
        priority=20,
    ),
    QuantitySpec(
        quantity="pressure",
        dimension="pressure",
        field_patterns=("pressure", "barometric", "sea_level_pressure"),
        unit_aliases=("mb", "hpa", "inHg"),
        priority=22,
    ),
    QuantitySpec(
        quantity="energy_release",
        dimension="energy",
        field_patterns=("energy_release", "energy_mj", "energy_j"),
        priority=11,
    ),
    # -- consequences ------------------------------------------------------
    QuantitySpec(
        quantity="customers_affected",
        dimension="count",
        field_patterns=(
            "customers_affected", "customers", "homes_without_power",
            "accounts_affected", "people_affected", "properties_affected",
        ),
        priority=30,
        intrinsic=False,
    ),
    QuantitySpec(
        quantity="capacity",
        dimension="count",
        field_patterns=("capacity", "seats", "ridership", "vehicles_affected", "trips_affected"),
        priority=32,
        intrinsic=False,
    ),
    QuantitySpec(
        quantity="delay_minutes",
        dimension="time",
        field_patterns=("delay_minutes", "delay_min", "estimated_delay", "delay"),
        unit_aliases=("min", "minutes", "mins", "s", "sec", "seconds", "h", "hours"),
        priority=34,
        intrinsic=False,
    ),
    QuantitySpec(
        quantity="travel_speed",
        dimension="speed",
        field_patterns=(
            "current_speed", "speed_mph", "average_speed", "observed_speed", "speed",
        ),
        unit_aliases=("mph", "km/h", "kts", "m/s"),
        priority=36,
        intrinsic=False,
    ),
    QuantitySpec(
        quantity="closure_length",
        dimension="length",
        field_patterns=("closure_length", "closure_miles", "length_miles", "extent_miles", "length_km"),
        priority=38,
        intrinsic=False,
    ),
    QuantitySpec(
        quantity="distance",
        dimension="length",
        field_patterns=("distance", "distance_m", "distance_km", "radius_m", "radius_km"),
        priority=40,
        intrinsic=False,
    ),
    QuantitySpec(
        quantity="containment",
        dimension="ratio",
        field_patterns=("containment", "percent_contained", "containment_pct", "controlled_pct"),
        priority=42,
        intrinsic=False,
    ),
)

#: Authority weight per source tier, mirroring the truth hierarchy.
#: Recency half-life used when an event has too few readings to have an
#: established rhythm of its own. An hour is a middle estimate for civic
#: reporting; it only matters until the second reading arrives.
_DEFAULT_HALF_LIFE_S = 3600.0

#: Floor on the derived half-life, so a burst of near-simultaneous readings does
#: not produce a half-life of milliseconds and then decay every earlier reading
#: to nothing.
_MIN_HALF_LIFE_S = 300.0

#: Relative tolerance for treating a running weight sum as an exact tie to half
#: the total. See :func:`_weighted_median`.
_MEDIAN_TIE_TOLERANCE = 1e-9

_AUTHORITY_WEIGHT: dict[Authority, float] = {
    Authority.OFFICIAL: 1.0,
    Authority.SEMI_OFFICIAL: 0.85,
    Authority.ESTABLISHED_MEDIA: 0.65,
    Authority.INTERNAL: 0.55,
    Authority.COMMUNITY: 0.35,
    Authority.UNVERIFIED: 0.15,
}


@dataclass(frozen=True)
class RawQuantity:
    """One number as one source published it, before any aggregation."""

    quantity: str
    value: float
    unit: str | None
    observation_id: str
    source_id: str
    authority: Authority
    event_time: datetime
    precision: float
    field: str


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class QuantityExtractor:
    """Reads quantities out of whatever a source chose to publish.

    Recurses into nested payloads because real feeds nest: a wildfire record has
    ``response.containment.percent``, a transit alert has
    ``routes[0].delay.seconds``. Depth is bounded so a pathological or
    self-referential payload cannot make this walk forever.
    """

    def __init__(
        self,
        specs: Sequence[QuantitySpec] = QUANTITY_SPECS,
        *,
        max_depth: int = 4,
    ) -> None:
        self.specs = tuple(specs)
        self.max_depth = max_depth
        # Longest patterns first so ``wind_speed`` beats the generic ``wind``.
        self._compiled: tuple[tuple[re.Pattern[str], QuantitySpec], ...] = tuple(
            (re.compile(re.escape(p), re.IGNORECASE), spec)
            for spec in self.specs
            for p in spec.field_patterns
        )

    def extract(self, observation: Observation) -> list[RawQuantity]:
        payload = observation.structured_payload or {}
        found: list[RawQuantity] = []
        self._walk(payload, (), found, observation, depth=0)
        return found

    def _walk(
        self,
        node: Any,
        path: tuple[str, ...],
        out: list[RawQuantity],
        observation: Observation,
        *,
        depth: int,
    ) -> None:
        if depth > self.max_depth:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                next_path = (*path, key_text)
                spec = self._match(key_text)
                number = _as_number(value)
                if spec is not None and number is not None:
                    out.append(self._build(spec, key_text, number, observation, next_path))
                    # A scalar leaf: also check whether its own key carries a
                    # unit sibling, then stop descending into non-containers.
                    if not isinstance(value, (dict, list)):
                        continue
                self._walk(value, next_path, out, observation, depth=depth + 1)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                self._walk(item, (*path, str(index)), out, observation, depth=depth + 1)

    def _match(self, key: str) -> QuantitySpec | None:
        best: QuantitySpec | None = None
        best_length = 0
        for pattern, spec in self._compiled:
            if pattern.search(key) and len(pattern.pattern) > best_length:
                best = spec
                best_length = len(pattern.pattern)
        return best

    def _build(
        self,
        spec: QuantitySpec,
        field: str,
        number: tuple[float, str | None],
        observation: Observation,
        path: tuple[str, ...],
    ) -> RawQuantity:
        value, declared_unit = number
        unit = declared_unit
        if unit is None:
            unit = _unit_from_path(path, spec)
        _, multiplier, offset = canonical_unit(spec.dimension, unit)
        return RawQuantity(
            quantity=spec.quantity,
            value=value * multiplier + offset,
            unit=canonical_unit(spec.dimension, unit)[0],
            observation_id=observation.observation_id,
            source_id=observation.source_id,
            authority=observation.provenance.authority,
            event_time=ensure_utc(observation.event_time),
            precision=observation.quality.spatial_precision
            if spec.quantity in {"distance", "wind_speed"}
            else observation.quality.source_reliability,
            field=field,
        )


def _as_number(value: Any) -> tuple[float, str | None] | None:
    """Coerce a payload value to ``(number, declared_unit)``.

    Accepts ints, floats, numeric strings (feeds publish ``"45 mph"`` and
    ``"12,000"``), and one level of ``{"value": 45, "unit": "mph"}`` wrappers.
    Rejects booleans outright, because ``True`` is a number in Python and
    ``{"severity": true}`` is emphatically not a magnitude.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        return _number_from_string(value)
    if isinstance(value, dict):
        inner = value.get("value", value.get("val", value.get("amount")))
        if inner is not None and not isinstance(inner, bool):
            parsed = _as_number(inner)
            if parsed is not None:
                unit = value.get("unit") or value.get("units") or value.get("uom")
                return parsed[0], str(unit) if unit else None
    return None


def _number_from_string(text: str) -> tuple[float, str | None] | None:
    stripped = text.strip().replace(",", "")
    if not stripped:
        return None
    match = re.match(
        r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z/%°][A-Za-z/%°\s]{0,12})?$", stripped
    )
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = (match.group(2) or "").strip() or None
    return value, unit


def _unit_from_path(path: tuple[str, ...], spec: QuantitySpec) -> str | None:
    """Infer a unit from a wrapping key, e.g. ``wind_speed_mph`` or ``rainfall_in``.

    A feed that names the unit in the key rather than the value is extremely
    common, and silently dropping the unit would mix metres with feet inside one
    quantity's interval - which is the specific mistake an interval is supposed
    to prevent.
    """

    if spec.unit_aliases:
        for part in reversed(path):
            lowered = part.lower()
            for alias in spec.unit_aliases:
                if alias.lower() == lowered or lowered.endswith(f"_{alias.lower()}"):
                    return alias
    for part in reversed(path[:-1] if len(path) > 1 else path):
        lowered = part.lower()
        if lowered in {"unit", "units", "uom"}:
            return None
    return None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


class ScaleEstimator:
    """Turns raw quantities, timestamps and geometry into ``EventScale``."""

    def __init__(self, extractor: QuantityExtractor | None = None) -> None:
        self.extractor = extractor or QuantityExtractor()

    def estimate(
        self,
        observations: Sequence[Observation],
        *,
        now: datetime | None = None,
        claims: Sequence[Any] = (),
    ) -> EventScale:
        now = now or utcnow()
        quantities: list[RawQuantity] = []
        for observation in observations:
            quantities.extend(self.extractor.extract(observation))

        magnitudes = self._aggregate(quantities, now)
        rate = self._rate(observations, now)
        duration = self._duration(observations, now)
        return EventScale(
            magnitudes=magnitudes,
            primary=self._primary(magnitudes),
            rate=rate,
            duration=duration,
        )

    # -- magnitudes -------------------------------------------------------

    def _aggregate(
        self, quantities: Sequence[RawQuantity], now: datetime
    ) -> tuple[QuantityEstimate, ...]:
        """Combine same-quantity readings into one estimate with an interval.

        Three properties, each fixing a specific way a median is wrong here.

        **It is a weighted median, not a mean.** Crowd estimates are the
        standard case: they cluster with a long upper tail, because one
        bystander who guessed high pulls a mean far above what anyone else
        reported, and then every downstream number inherits that error. A median
        is unmoved by a single outlier while still using every value.

        **It is recency-weighted, because the question is "how big is this
        *now*".** An unweighted median over a growing event reports where it was
        most of its life, not where it is: three reports of 300, 900 and 2500
        people put the estimate at 900, while the only figure that describes the
        present moment is the most recent one. Weighting each reading by
        ``0.5 ** (age / half-life)``, with the half-life set to the event's own
        median reporting gap, makes the estimate follow the situation without
        needing to know anything about what kind of event it is - a fast-moving
        and a slow-moving event each get a half-life appropriate to their own
        rhythm.

        **No single reading may outweigh all the others.** Recency weighting
        alone reintroduces exactly the outlier sensitivity the median was chosen
        to avoid: one recent, badly wrong report would be worth more than every
        earlier report combined. Capping each weight at the sum of the rest
        bounds that, so a bad reading can move the estimate to the next-best
        value but cannot make it the answer.

        ``disagreement`` is the relative spread across sources, which is what
        keeps "one source said 300" distinguishable from "five sources said
        250-350".
        """

        by_quantity: dict[str, list[RawQuantity]] = {}
        for quantity in quantities:
            by_quantity.setdefault(quantity.quantity, []).append(quantity)

        half_life = self._half_life(quantities)

        estimates: list[QuantityEstimate] = []
        for name, group in sorted(by_quantity.items()):
            if not group:
                continue
            # In group order, deliberately: ``_weighted_median`` sorts value and
            # weight together. Sorting the values first and then zipping them
            # against group-order weights pairs each reading's weight with
            # whichever value happened to sort into its slot - which silently
            # hands the newest reading the oldest reading's weight, and makes a
            # shrinking event look exactly like a growing one.
            values = [q.value for q in group]
            weights = self._recency_weights(group, now, half_life)
            point = _weighted_median(values, weights)

            low, high = min(values), max(values)
            spread = 0.0 if high <= low else (high - low) / max(abs(high), 1e-9)
            disagreement = round(min(1.0, spread), 4)

            # A single source gets an interval derived from its own declared
            # uncertainty rather than from other sources disagreeing, because
            # "no disagreement observed" must not read as "certain".
            if len(values) == 1:
                margin = 0.35 * max(0.2, 1.0 - group[0].precision)
                lower = point * (1.0 - margin)
                upper = point * (1.0 + margin)
            else:
                pad = (high - low) * 0.1
                lower = low - pad
                upper = high + pad

            spec = self._spec_for(name)
            confidence = self._confidence(group, disagreement, weights)
            estimates.append(
                QuantityEstimate(
                    quantity=name,
                    unit=group[0].unit or (DIMENSIONS.get(spec.dimension, UnitDefinition("")).symbol or None if spec else None),
                    value=round(point, 4),
                    lower=round(lower, 4),
                    upper=round(upper, 4),
                    confidence=confidence,
                    method="aggregate" if len(values) > 1 else "source_structured",
                    observation_ids=tuple(dict.fromkeys(q.observation_id for q in group)),
                    source_count=len({q.source_id for q in group}),
                    disagreement=disagreement,
                )
            )
        return tuple(estimates)

    @staticmethod
    def _half_life(quantities: Sequence[RawQuantity]) -> float:
        """This event's own reporting rhythm, in seconds.

        The median gap between readings. Used as the recency half-life so the
        decay rate is set by how often the event actually reports rather than by
        a constant tuned to one kind of event - a 15-minute half-life is right
        for a road closure being re-reported and wrong for a wildfire whose
        containment percentage is updated twice a day.
        """

        if len(quantities) < 2:
            return _DEFAULT_HALF_LIFE_S
        times = sorted(ensure_utc(q.event_time) for q in quantities)
        gaps = [
            (b - a).total_seconds() for a, b in zip(times, times[1:], strict=False) if b > a
        ]
        if not gaps:
            return _DEFAULT_HALF_LIFE_S
        gaps.sort()
        return max(_MIN_HALF_LIFE_S, gaps[len(gaps) // 2])

    @staticmethod
    def _recency_weights(
        group: Sequence[RawQuantity], now: datetime, half_life: float
    ) -> list[float]:
        """Authority x recency, with no reading allowed to dominate.

        Recency is relative to ``now`` clamped at zero, so a reading from the
        future - a clock skew between a feed and the platform - is treated as
        current rather than as infinitely authoritative.
        """

        raw: list[float] = []
        for quantity in group:
            age = max(0.0, (ensure_utc(now) - ensure_utc(quantity.event_time)).total_seconds())
            decay = 0.5 ** (age / max(1.0, half_life))
            raw.append(_AUTHORITY_WEIGHT.get(quantity.authority, 0.2) * decay)

        total = sum(raw)
        return [
            # Cap at the combined weight of everything else, so one reading can
            # shift the median to the next-best value but not past it.
            min(weight, max(1e-9, total - weight))
            for weight in raw
        ]

    def _spec_for(self, quantity: str) -> QuantitySpec | None:
        for spec in QUANTITY_SPECS:
            if spec.quantity == quantity:
                return spec
        return None

    @staticmethod
    def _confidence(
        group: Sequence[RawQuantity], disagreement: float, weights: Sequence[float] | None = None
    ) -> float:
        """Confidence falls with source count, authority and disagreement.

        Deliberately multiplicative rather than additive. A value from three
        anonymous sources is not more trustworthy than a value from one official
        one just because there are more of them - syndication is not
        corroboration, the same rule the evidence layer applies to sources.

        ``weights`` are the recency weights, and precision is averaged with them
        rather than uniformly: a precise reading from three hours ago should not
        lend its precision to an estimate being reported now.
        """

        authorities = max((_AUTHORITY_WEIGHT.get(q.authority, 0.2) for q in group), default=0.2)
        distinct_sources = len({q.source_id for q in group})
        # Saturating source bonus: the second independent source matters, the
        # ninth barely does.
        source_bonus = min(1.0, 0.6 + 0.1 * distinct_sources)
        if weights:
            total = sum(weights) or 1.0
            precision = sum(
                w * q.precision for w, q in zip(weights, group, strict=False)
            ) / total
        else:
            precision = sum(q.precision for q in group) / len(group) if group else 0.2
        base = 0.5 * authorities + 0.2 * source_bonus + 0.3 * precision
        return round(max(0.0, min(1.0, base * (1.0 - 0.5 * disagreement))), 4)

    @staticmethod
    def _primary(magnitudes: Sequence[QuantityEstimate]) -> QuantityEstimate | None:
        """Pick the quantity that best characterises the event.

        Ties on spec priority are broken by confidence, so between an official
        magnitude and an unofficial temperature reading of equal intrinsic rank,
        the official one is reported as primary and the other is still present.
        """

        if not magnitudes:
            return None
        ranked = []
        for estimate in magnitudes:
            spec = next(
                (s for s in QUANTITY_SPECS if s.quantity == estimate.quantity), None
            )
            ranked.append((spec.priority if spec else 99, -estimate.confidence, estimate))
        ranked.sort(key=lambda t: (t[0], t[1]))
        return ranked[0][2]

    # -- rate -------------------------------------------------------------

    def _rate(self, observations: Sequence[Observation], now: datetime) -> EventRate:
        if len(observations) < 2:
            return EventRate()

        ordered = sorted(observations, key=lambda o: ensure_utc(o.observed_at))
        first_seen = ensure_utc(ordered[0].observed_at)
        last_seen = ensure_utc(ordered[-1].observed_at)
        span_hours = (last_seen - first_seen).total_seconds() / 3600.0

        if span_hours <= 0:
            # Everything arrived in one burst. The honest answer is a rate the
            # platform cannot establish, not a divide-by-near-zero spike.
            return EventRate(
                observation_rate_per_hour=0.0,
                recent_rate_per_hour=0.0,
                trend="unknown",
                trend_strength=0.0,
            )

        overall = (len(ordered) - 1) / span_hours
        midpoint = first_seen + (last_seen - first_seen) / 2
        earlier = sum(1 for o in ordered if ensure_utc(o.observed_at) < midpoint)
        later = len(ordered) - earlier
        early_rate = earlier / max(1e-6, span_hours / 2)
        late_rate = later / max(1e-6, span_hours / 2)

        # A trailing window measured against "now" is the honest recent rate,
        # including the silence that has happened since.
        window_s = 3600.0
        recent = sum(
            1 for o in ordered if (now - ensure_utc(o.observed_at)).total_seconds() <= window_s
        )
        recent_rate = recent / (window_s / 3600.0)

        ratio = late_rate / early_rate if early_rate > 0 else (2.0 if late_rate > 0 else 1.0)
        if ratio >= 1.5:
            trend, strength = "rising", min(1.0, (ratio - 1.0) / 2.0)
        elif ratio <= 0.67:
            trend, strength = "falling", min(1.0, (1.0 / max(ratio, 1e-6) - 1.0) / 2.0)
        else:
            trend, strength = "steady", min(1.0, abs(ratio - 1.0))

        return EventRate(
            observation_rate_per_hour=round(overall, 4),
            recent_rate_per_hour=round(recent_rate, 4),
            trend=trend,  # type: ignore[arg-type]
            trend_strength=round(strength, 4),
        )

    # -- duration ---------------------------------------------------------

    def _duration(self, observations: Sequence[Observation], now: datetime) -> EventDuration:
        if not observations:
            return EventDuration()
        event_times = [ensure_utc(o.event_time) for o in observations]
        seen_times = [ensure_utc(o.observed_at) for o in observations]
        first_event = min(event_times)
        last_event = max(event_times)
        last_seen = max(seen_times)
        return EventDuration(
            elapsed_seconds=max(0.0, (now - first_event).total_seconds()),
            active_span_seconds=max(0.0, (last_event - first_event).total_seconds()),
            since_first_observed=first_event,
            last_observation_at=last_seen,
            silence_seconds=max(0.0, (now - last_seen).total_seconds()),
        )


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    """Median honouring weights, computed over a value the inputs actually had.

    Weighted medians interpolate, and an interpolated "500" implies a precision
    that no source ever claimed. The weighted midpoint of the two central values
    is used instead, so the reported estimate is always one somebody published.

    The midpoint case is reached on a running-sum comparison, and the running sum
    is the accumulated product of decaying weights. Comparing it to half the
    total for *equality* is therefore the one comparison here that depends on
    floating-point accident rather than on the data. When the total is 0.02,
    ``0.002 + 0.008`` is not reliably ``0.010``, and which side it lands on
    decides whether the estimate is the middle reading or the one after it -
    which is the difference between a growing event and a shrinking one
    reporting the same number. Hence the relative tolerance: any equality within
    a part in ``1e9`` is treated as a tie and interpolates, deterministically.
    """

    if not values:
        return 0.0
    pairs = sorted(zip(values, weights, strict=False), key=lambda t: t[0])
    total = sum(w for _, w in pairs) or 1.0
    half = total / 2.0
    tolerance = max(abs(half), 1.0) * _MEDIAN_TIE_TOLERANCE
    cumulative = 0.0
    for index, (value, weight) in enumerate(pairs):
        cumulative += weight
        if cumulative >= half - tolerance:
            if index + 1 < len(pairs) and abs(cumulative - half) <= tolerance:
                return (value + pairs[index + 1][0]) / 2
            return value
    return pairs[-1][0]


def summarise_scale(scale: EventScale) -> dict[str, Any]:
    """Compact dict form, for derived features and the world projection."""

    primary = scale.primary
    return {
        "primary_quantity": primary.quantity if primary else "",
        "primary_value": float(primary.value) if primary and primary.value is not None else 0.0,
        "primary_unit": (primary.unit or "") if primary else "",
        "primary_confidence": primary.confidence if primary else 0.0,
        "primary_disagreement": primary.disagreement if primary else 0.0,
        "magnitude_count": float(len(scale.magnitudes)),
        "rate_per_hour": scale.rate.observation_rate_per_hour,
        "rate_trend": {"rising": 1.0, "falling": -1.0, "steady": 0.0}.get(scale.rate.trend, 0.0),
        "duration_seconds": scale.duration.elapsed_seconds,
        "silence_seconds": scale.duration.silence_seconds,
    }


def magnitude_deltas(
    previous: EventScale, current: EventScale
) -> list[tuple[str, float, float, float]]:
    """Material magnitude changes as ``(quantity, before, after, relative)``.

    Relative change is scaled by an interval floor so a quantity that jitters
    between 300 and 305 is not reported as having changed, while one that moves
    from 300 to 3000 is.
    """

    before_map = {m.quantity: m for m in previous.magnitudes}
    after_map = {m.quantity: m for m in current.magnitudes}
    out: list[tuple[str, float, float, float]] = []
    for name, after in after_map.items():
        after_value = after.value
        if after_value is None:
            continue
        before = before_map.get(name)
        before_value = before.value if before is not None else None
        if before_value is None:
            continue
        scale = max(abs(before_value), abs(after_value), 1e-9)
        relative = (after_value - before_value) / scale
        floor = 0.1 + 0.2 * max(after.disagreement, before.disagreement if before else 0.0)
        if abs(relative) > floor:
            out.append((name, before_value, after_value, relative))
    return out


__all__ = [
    "DIMENSIONS",
    "QUANTITY_SPECS",
    "UNIT_CONVERSIONS",
    "EventDuration",
    "EventRate",
    "EventScale",
    "LifecycleAssessment",
    "QuantityEstimate",
    "QuantityEstimator",
    "QuantitySpec",
    "RawQuantity",
    "ScaleEstimator",
    "canonical_unit",
    "magnitude_deltas",
    "summarise_scale",
]

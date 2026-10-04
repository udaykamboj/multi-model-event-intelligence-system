"""Historical and research corpora adapters (brief sections 34, 45).

These files are not feeds. They are the evidence base the predictive and
statistical layers learn from, and reading them correctly is most of what makes
those layers trustworthy. This module parses all of them.

Why they are quarantined
------------------------
Every signal here is registered ``usage="historical_only"``. That gate is
enforced in :meth:`infraimpact.sources.registry.SourceRegistry.realtime` and in
the relevance engine, so a 2017 protest in Thurston County can inform "what
happens after a march of this size" while being structurally incapable of
notifying anyone about 2026. A corpus that trains the model must never be
allowed to page a user.

Traps this module exists to survive
-----------------------------------
These are all verified against the files, not assumed:

1. **Three dialects and a missing header.** ``*.tab`` (CCC, MIDA, NAVCO),
   ``*.csv`` (CCC phase 3, mobility, crime) and ``*.CSV`` which is
   tab-delimited *and* headerless (GDELT). A naive reader turns the whole GDELT
   file into one column named after its first data row.
2. **Four different ways of saying "missing".** ``NA``, ``.``, ``-`` and the
   empty string all appear. ``.`` is the Affinity missing marker, so ``float()
   coercion turns an absent spending figure into ``0.0`` - a fabricated
   collapse in spending on exactly the days the data is thinnest.
3. **Three different coordinate column names.** ``lat``/``lon``,
   ``latitude``/``longitude``, and ``lat``/``lon`` with different precision.
4. **A 40 MB workbook that is not an event list.** ``acled_hdx`` contains only
   pre-aggregated count cubes plus a licensing notice. Reading it as events
   would manufacture a million demonstrations.
5. **Aggregate-versus-record.** Google Mobility, Affinity, Employment and
   Womply are indexed by ``cityid`` or ``countyfips``, not coordinates. Only
   ``cityid`` can be resolved to a point, through ``GeoIDs_City.csv``.
6. **Alias pairs.** ``Google_Mobility_County_Daily.csv`` has a ``.csv.gz``
   twin and every GDELT ``.CSV`` has a ``.CSV.zip`` twin. Reading both counts
   the same dataset twice.
"""

from __future__ import annotations

import csv
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from xml.etree import ElementTree

from ..domain.enums import Authority, ObservationType, SourceType
from ..domain.geo import bbox_of
from ..domain.schemas import Observation
from .adapter import RawRecord
from .snapshot import ADMISSION_KEY, SnapshotAdapter, _inside, feature_geometry, point_from_latlon

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# missing-value handling
# --------------------------------------------------------------------------

#: Tokens that mean "no value" across this corpus. ``.`` is the Affinity
#: marker and is the dangerous one: it parses as nothing, so a float coercion
#: yields 0.0 and the absence of a measurement becomes a measured collapse.
MISSING_TOKENS: frozenset[str] = frozenset(
    {"", "na", "n/a", "null", "none", ".", "-", "--", "nan", "unknown", "not_available"}
)


def clean(value: Any, limit: int = 400) -> str | None:
    """Normalise a cell to trimmed text, or ``None`` when it is missing."""

    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in MISSING_TOKENS:
        return None
    return text[:limit]


def num(value: Any) -> float | None:
    """Parse a number, returning ``None`` for every missing-value dialect."""

    text = clean(value, limit=64)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def integer(value: Any) -> int | None:
    parsed = num(value)
    return int(parsed) if parsed is not None else None


def flag(value: Any) -> bool | None:
    """Parse a boolean cell. ``NA`` stays ``None`` rather than becoming False."""

    text = clean(value, limit=16)
    if text is None:
        return None
    if text.lower() in {"1", "true", "yes", "y", "t"}:
        return True
    if text.lower() in {"0", "false", "no", "n", "f"}:
        return False
    return None


# --------------------------------------------------------------------------
# shared lookups
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# study areas
# --------------------------------------------------------------------------

#: Washington State, generously padded. Used by the ACLED Washington corpus,
#: which is statewide: a march in Yakima is a legitimate comparator for a march
#: in Seattle, and a march in Ohio is not.
WASHINGTON = (-124.85, 45.30, -116.85, 49.10)

#: Contiguous United States. Used by the national corpora whose rows carry
#: coordinates but no country column.
CONTIGUOUS_US = (-125.00, 24.00, -66.50, 49.50)

#: North America, for GDELT, whose export has no reliable country column at
#: all - the geography is all that can be trusted, so the study area has to be
#: a geographic one too.
NORTH_AMERICA = (-170.00, 12.00, -52.00, 72.00)

#: Country names and numeric codes the corpora use for the United States. Every
#: corpus spells it differently and one of them stores it as a float, so the
#: match is deliberately broad rather than exact.
#:
#: ``750`` is missing from this set in every published description of the
#: NAVCO schema that exists, because ``loc_cow`` is Correlates of War rather
#: than ISO. Its absence means a US-located campaign would be dropped as
#: out-of-scope while the adapter claimed to be reading the country column.
_US_NAMES = frozenset(
    {
        "united states",
        "united states of america",
        "usa",
        "us",
        "u.s.",
        "u.s.a.",
        "united states of north america",
    }
)
_US_ISO_CODES = frozenset({"840", "750"})
_US_FIPS_PREFIX = "53"
#: State FIPS for Washington.
WA_STATE_FIPS = "53"
WA_STATE_ABBREV = "WA"


def _norm(value: Any) -> str:
    """Lowercase, trimmed text, with a trailing ``.0`` stripped.

    NAVCO writes ``840.0`` for the United States because its code columns went
    through a spreadsheet. Comparing the raw string against ``"840"`` silently
    drops every American campaign.
    """

    text = (clean(value, 80) or "").strip().lower()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def is_us_country(*values: Any) -> bool:
    """True when any of the given cells identifies the United States."""

    return any(_norm(value) in _US_NAMES or _norm(value) in _US_ISO_CODES for value in values)


def _data_root(root: Path) -> Path:
    return root


class CityIdLookup:
    """``cityid`` -> coordinates, from ``economic_opportunity_insights/GeoIDs_City.csv``.

    Six of the mobility and spending files are keyed by ``cityid`` with no
    coordinates of their own. This is the only table in the corpus that
    connects those ids to places, so an Affinity row is unusable without it -
    which is why the adapters hold one instead of dropping the geometry.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._rows: dict[str, dict[str, Any]] = {}
        if path and path.is_file():
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                for row in csv.DictReader(handle):
                    ident = clean(row.get("cityid"))
                    if ident:
                        self._rows[ident] = row

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, cityid: Any) -> dict[str, Any] | None:
        ident = clean(cityid, 32)
        return self._rows.get(ident) if ident else None


def load_city_ids(root: Path) -> CityIdLookup:
    return CityIdLookup(
        root / "data/research/economic_opportunity_insights/GeoIDs_City.csv"
    )


# --------------------------------------------------------------------------
# base class
# --------------------------------------------------------------------------


class HistoricalAdapter(SnapshotAdapter):
    """Base for corpora that inform analysis but never raise a notification.

    Three jobs beyond parsing:

    **Scope.** Every corpus here is at least regional and most are national or
    global, so a plain "inside the Puget Sound box" test would discard
    essentially all of them and leave the platform with no baseline at all. Each
    subclass therefore declares a *study area* - a wider, documented geography -
    and records admitted from it are labelled ``comparator`` rather than being
    passed off as local events. A Washington State protest is admitted by the
    ACLED adapter as a comparator for a Seattle one; a Sudanese conflict event
    is not admitted at all.

    **Identity.** Several of these corpora have no primary key (ACLED and the
    Crowd Counting Consortium are the two), and an observation without a stable
    id cannot be deduplicated, which would quietly double-count every event on
    the next poll. Each subclass declares ``id_fields`` or composes one.

    **Missing values.** ``NA``, ``.``, ``-`` and ``""`` all occur, and ``.`` is
    the dangerous one: ``float(".")`` raises, so every naive coercion either
    crashes or - worse - treats it as ``0.0`` and reads a missing figure as a
    measured zero.
    """

    usage = "historical_only"
    #: Corpora are research products, not live agencies. None of them is
    #: machine-readable *official* output except the municipal police files.
    source_type: SourceType = SourceType.THIRD_PARTY_DATABASE
    authority: Authority = Authority.COMMUNITY

    #: ``False`` for GDELT, whose export carries no header row at all.
    has_header: bool = True

    #: Records emitted per file. The largest corpus here is 256 MB, so an
    #: unbounded read would exhaust memory. The cap applies to *emitted* records,
    #: after relevance filtering, so a file is never truncated in favour of rows
    #: that were about to be discarded.
    max_records: int | None = 20000

    #: Wider ``(min_lon, min_lat, max_lon, max_lat)`` box, or ``None`` to admit
    #: nothing outside the operating region. Subclasses set it.
    study_bounds: tuple[float, float, float, float] | None = None

    #: Human-readable description of the study area, stored on every record.
    scope_label: str = "study area"

    #: Keep rows that carry no geometry at all. Right for area- and
    #: country-level aggregates, wrong for point events: a news story with no
    #: place cannot be compared to anything.
    keep_unlocated: bool = False

    #: Precision assumed for a record admitted from outside the operating region.
    comparator_precision_m: float = 200_000.0

    def __init__(self, spec: Any = None, **kwargs: Any) -> None:
        super().__init__(spec, **kwargs)
        self._city_ids: CityIdLookup | None = None

    @property
    def city_ids(self) -> CityIdLookup:
        if self._city_ids is None:
            self._city_ids = load_city_ids(self.root)
        return self._city_ids

    # -- scope -------------------------------------------------------------

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """Corpus-specific relevance, applied before the region test.

        Overridden where a geography or country column is a better filter than
        coordinates - UCDP is sorted by country, MIDA and NAVCO have no
        coordinates at all.
        """

        return True

    def admit(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> str | None:
        if not self.relevant(item, geometry):
            self.dropped_out_of_scope += 1
            return None
        # Geometry first, not the region test. ``_in_region(None)`` is ``True``
        # by design - a news headline legitimately has no coordinates - so
        # testing the region before the geometry stamps a country-level
        # aggregate or a FIPS-keyed weekly figure as ``within_operating_region:
        # True``. That is precisely the claim the labels exist to prevent: a
        # Sudan-wide protest count presented as a Seattle one, at 200 km
        # nominal precision.
        if geometry is None:
            if self.keep_unlocated:
                return "unlocated"
            self.dropped_out_of_region += 1
            return None
        if self._in_region(geometry):
            return "region"
        if self.study_bounds is not None:
            box = bbox_of(geometry)
            if box is not None and _inside(box, self.study_bounds):
                return "comparator"
        self.dropped_out_of_region += 1
        return None

    def _scope_telemetry(self) -> str | None:
        parts = [f"scope={self.scope_label!r}"]
        if self.dropped_out_of_scope:
            parts.append(f"dropped_out_of_scope={self.dropped_out_of_scope}")
        return " ".join(parts)

    # -- identity and time -------------------------------------------------

    id_fields: tuple[str, ...] = ("id",)
    date_fields: tuple[str, ...] = ("date",)

    #: Columns composed into a stable id when the corpus has no primary key.
    #: A row with none of these present is skipped rather than given a
    #: non-deterministic id, because an unstable id defeats deduplication.
    identity_fields: tuple[str, ...] = ()

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in self.id_fields:
            value = clean(item.get(key))
            if value:
                return f"{self.source_id}:{value}"
        if self.identity_fields:
            parts = [clean(item.get(key), 120) or "" for key in self.identity_fields]
            if any(parts):
                return f"{self.source_id}:" + "|".join(parts)
        return ""

    def _event_time(self, item: dict[str, Any]) -> datetime | None:
        for key in self.date_fields:
            value = parse_historic_date(item.get(key))
            if value is not None:
                return value
        return None

    def _observed_at(self, item: dict[str, Any]) -> datetime | None:
        return self._event_time(item)

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        found = feature_geometry(item)
        if found:
            return found
        return point_from_latlon(
            item.get("latitude") or item.get("lat"),
            item.get("longitude") or item.get("lon") or item.get("long"),
        )

    def normalize(self, record: RawRecord) -> Observation | None:  # noqa: D102
        raise NotImplementedError

    # -- shared payload builder -------------------------------------------

    def gather_observation(
        self,
        record: RawRecord,
        *,
        headline: str,
        observation_type: ObservationType,
        payload: dict[str, Any],
        precision_m: float | None = None,
        authority: Authority | None = None,
        source_type: SourceType | None = None,
        drop_empty: bool = True,
    ) -> Observation:
        """Build an observation from a cleaned payload dict.

        ``drop_empty`` removes keys whose value is ``None`` so the ledger does
        not carry a dense field of nulls for every corpus whose schema differs.

        The admission label from :meth:`admit` is consumed here: a comparator or
        unlocated record is stamped with its scope and given a precision that
        admits how coarse it really is, so nothing downstream can mistake a
        Washington State crowd count for a Seattle one.
        """

        admission = record.payload.get(ADMISSION_KEY, "region")
        payload = dict(payload)
        payload["study_area_scope"] = self.scope_label
        payload["within_operating_region"] = admission == "region"
        if admission == "comparator":
            payload["is_historical_comparator"] = True
            precision_m = self.comparator_precision_m
        elif admission == "unlocated":
            payload["is_unlocated_area_aggregate"] = True
            precision_m = self.comparator_precision_m
        cleaned = {k: v for k, v in payload.items() if not (drop_empty and v is None)}
        item = {k: v for k, v in record.payload.items() if k != ADMISSION_KEY}
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=observation_type,
            headline=headline,
            structured_payload=cleaned,
            precision_m=precision_m,
            authority=authority or self.authority,
            source_type=source_type or self.source_type,
            keep_raw=False,
        )


_DATE_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%b %d %Y",
)


def parse_historic_date(value: Any) -> datetime | None:
    """Parse a date from any of the dialects the corpora use.

    ``NA`` and the other missing tokens return ``None`` rather than raising, and
    a single bad date never discards the row - a corpus with one unparseable
    timestamp still contains a usable event.
    """

    text = clean(value, 64)
    if text is None:
        return None
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# A. ACLED regional - the closest analogue corpus to the product's domain
# --------------------------------------------------------------------------


class AcledRegionalAdapter(HistoricalAdapter):
    """ACLED protest events for Washington State and DC.

    ``washington_state_protest_events.csv`` (10,462 events, 114 columns) and
    ``washington_dc_protest_events.csv`` share a schema. This is the most
    valuable file in the corpus for this product: real demonstrations in the
    target state, with an *estimated crowd size band* and the outcome measures
    - arrests, crowd and police injuries, property damage, chemical-agent use.

    Three honesty rules the normalisation obeys:

    * ``size_mean`` is a **crowd-size estimate derived from media reports**,
      not a headcount. It is stored as ``estimated_size_mean`` with its low/high
      band, and the band is always carried alongside the mean so no downstream
      consumer can treat 5,500 as measured to the person.
    * ``NA`` in ``arrests``/``injuries_*``/``property_damage`` means *not
      reported*, which is not the same as zero. It stays ``None``; the separate
      ``*_any`` boolean columns are the binary facts.
    * Claims and issues are participant-characterised content. They are stored
      verbatim as ``claims_text`` / ``issues_text`` and are the reason the LLM
      layer exists for narrative extraction - they are never used as features
      for any risk score about a group.
    """

    source_type = SourceType.THIRD_PARTY_DATABASE
    authority = Authority.COMMUNITY
    id_fields = ("id", "event_id")
    #: Neither release carries a primary key, so the id is composed from what
    #: uniquely identifies a gathering: when it happened, where, and what it was.
    identity_fields = ("date", "locality", "location_detail", "event_type", "type")
    date_fields = ("date",)
    max_records = 60000
    study_bounds = WASHINGTON
    scope_label = "Washington State (comparator outside the Puget Sound box)"

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        size_mean = integer(item.get("size_mean"))
        size_low = integer(item.get("size_low"))
        size_high = integer(item.get("size_high"))
        kind = clean(item.get("type") or item.get("event_type"), 60) or "gathering"
        locality = clean(item.get("locality"), 80)
        detail = clean(item.get("location_detail"), 120)
        head = " - ".join(p for p in (kind.title(), locality, detail) if p)
        if size_mean:
            head += f" (est. {size_mean:,})"

        return self.gather_observation(
            record,
            headline=head[:250] or "historical gathering",
            observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
            payload={
                "event_date": clean(item.get("date"), 32),
                "event_type": kind,
                "location_detail": detail,
                "locality": locality,
                "state": clean(item.get("state"), 8),
                "county": clean(item.get("resolved_county"), 80),
                "fips_code": clean(item.get("fips_code"), 16),
                "online_only": flag(item.get("online")),
                "estimated_size_mean": size_mean,
                "estimated_size_low": size_low,
                "estimated_size_high": size_high,
                "size_category": clean(item.get("size_cat"), 8),
                "size_text": clean(item.get("size_text"), 60),
                "size_is_media_estimate": True,
                "arrests_reported": integer(item.get("arrests")),
                "arrests_any": flag(item.get("arrests_any")),
                "crowd_injuries_any": flag(item.get("participant_injuries") or item.get("injuries_crowd_any")),
                "police_injuries_any": flag(item.get("police_injuries") or item.get("injuries_police_any")),
                "property_damage_any": flag(item.get("property_damage_any")),
                "chemical_agents_any": flag(item.get("chemical_agents")),
                "claims_text": clean(item.get("claims"), 600),
                "issues_text": clean(item.get("issues"), 300),
                "organizations_text": clean(item.get("organizations"), 400),
                "macroevent": clean(item.get("macroevent"), 60),
                "valence": integer(item.get("valence")),
                "source_count": sum(
                    1 for k, v in item.items() if str(k).startswith("source_") and clean(v)
                ),
            },
            precision_m=1500.0,
        )


# --------------------------------------------------------------------------
# B. Crowd Counting Consortium - crowd-size basis
# --------------------------------------------------------------------------


class CrowdCountingAdapter(HistoricalAdapter):
    """Crowd Counting Consortium compiled events, 2017 to present.

    Three releases with two different schemas:

    ``ccc_compiled_20172020.tab`` / ``ccc_compiled_20212024.tab``
        62 columns, tab-delimited.
    ``ccc_phase3_public_2025_present.csv``
        74 columns, comma-delimited, with renamed fields: ``source_1`` became
        ``source1``, and ``participants``/``notables`` carry narrative.

    This is the corpus that makes crowd size estimable at all. ``size_mean`` is
    still an estimate, but it comes with a low/high band, which is the only
    honest way to carry it. The binary outcome fields (``arrests_any``,
    ``*_casualties_any``, ``property_damage_any``) are the empirical link from
    "a gathering of about this size happened here" to "this is what followed".

    Only ``size_cat`` is present for most rows; where ``size_mean`` is absent,
    the category is preserved rather than a mean being invented from it.
    """

    authority = Authority.COMMUNITY
    id_fields = ("id", "event_id")
    identity_fields = ("date", "locality", "location_detail", "event_type", "type")
    date_fields = ("date",)
    max_records = 200000
    study_bounds = CONTIGUOUS_US
    scope_label = "United States (contiguous), comparator outside the operating region"

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_rows(doc)

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        kind = clean(item.get("type") or item.get("event_type"), 60) or "gathering"
        locality = clean(item.get("locality"), 80)
        state = clean(item.get("state"), 8)
        detail = clean(item.get("location_detail") or item.get("location"), 120)
        size_mean = integer(item.get("size_mean"))
        size_cat = clean(item.get("size_cat"), 8)
        head = " - ".join(p for p in (kind.title(), detail or locality, state) if p)
        if size_mean:
            head += f" (est. {size_mean:,})"
        elif size_cat:
            head += f" (size category {size_cat})"

        return self.gather_observation(
            record,
            headline=head[:250] or "historical gathering",
            observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
            payload={
                "event_date": clean(item.get("date"), 32),
                "event_type": kind,
                "location_detail": detail,
                "locality": locality,
                "state": state,
                "county": clean(item.get("resolved_county"), 80),
                "fips_code": clean(item.get("fips_code"), 16),
                "online_only": flag(item.get("online")),
                "estimated_size_mean": size_mean,
                "estimated_size_low": integer(item.get("size_low")),
                "estimated_size_high": integer(item.get("size_high")),
                "size_category": size_cat,
                "size_text": clean(item.get("size_text"), 60),
                "size_is_media_estimate": True,
                "arrests_any": flag(item.get("arrests_any")),
                "participant_casualties_any": flag(item.get("participant_casualties_any")),
                "police_casualties_any": flag(item.get("police_casualties_any")),
                "property_damage_any": flag(item.get("property_damage_any")),
                "chemical_agents_any": flag(item.get("chemical_agents")),
                "claims_text": clean(item.get("claims_summary") or item.get("claims"), 600),
                "issues_text": clean(item.get("issues"), 300),
                "organizations_text": clean(item.get("organizations"), 400),
                "macroevent": clean(item.get("macroevent"), 60),
                "is_repeater": flag(item.get("repeater")),
                "valence": integer(item.get("valence")),
                "corpus": self.source_id,
            },
            precision_m=1500.0,
        )


def json_rows(doc: Any) -> list[dict[str, Any]]:  # pragma: no cover - tiny helper
    return [r for r in doc if isinstance(r, dict)] if isinstance(doc, list) else []


# --------------------------------------------------------------------------
# C. ACLED count cubes - aggregate only, never events
# --------------------------------------------------------------------------


class AcledCountCubeAdapter(HistoricalAdapter):
    """``acled_hdx/demonstration_events.xlsx`` - count cubes, not events.

    Sheet 1 is a licensing notice. Sheets 2-4 are pre-aggregated
    ``Country / ... / Month / Year / Events`` count cubes with 30,556,
    601,734 and 408,879 rows respectively, and the workbook's sheet XML expands
    to roughly 500 MB when decompressed.

    That makes this corpus a *frequency table*: "how many demonstrations were
    recorded in this country in this month". It is a defensible base rate for
    how common mass mobilisation is, which is exactly what a prior needs. It is
    **not** a list of demonstrations, and treating it as one would invent a
    million events with fabricated dates and places.

    So the adapter reads it with a streaming SAX pass over the sheet XML - no
    third-party spreadsheet library and no materialisation - and each cube row
    becomes one ``ACTIVITY_CONTEXT`` observation carrying the count. Country is
    resolved through a small built-in centroid table for the handful of
    countries the cube is keyed on, because a count by country with no location
    cannot be compared against anything regional.
    """

    authority = Authority.COMMUNITY
    max_records = 5000
    keep_unlocated = True
    scope_label = "United States count cubes (country-level aggregate, no coordinates)"

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """Keep the US cubes.

        The workbook counts demonstrations in 200+ countries. Their value here is
        a base rate - how common mass mobilisation is at all - and Washington's
        own rate is the only one that can inform anything about Washington.
        """

        return is_us_country(item.get("Country"), item.get("country"))

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        path = self.resolve()
        if path is None:
            self._message = f"no workbook at {self.root / self.subdir}"
            return []
        stat = path.stat()
        self._signature = f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}"
        if checkpoint == self._signature:
            return []

        out: list[RawRecord] = []
        sheets = 0
        skipped = 0
        # The budget is spent on *relevant* rows, so the sheet scan continues
        # past the cap rather than stopping at the first N rows of sheet 3.
        scan_cap = (self.max_records or 5000) * 400
        for name, rows in iter_workbook_rows(path, scan_cap):
            if not _looks_like_count_cube(rows):
                continue  # sheet 1: the TOU / licensing notice
            sheets += 1
            header = rows[0]
            for row in rows[1:]:
                cube = dict(zip(header, row))
                if len(out) >= (self.max_records or 0):
                    break
                if not self.relevant(cube, None):
                    skipped += 1
                    continue
                out.append(
                    RawRecord(
                        source_record_id=f"{self.source_id}:{name}:{'|'.join(row[:6])}",
                        payload={"sheet": name, **cube},
                        event_time=parse_historic_date(
                            f"{cube.get('Year', '')}-{cube.get('Month', '')}-01"
                        ),
                        observed_at=None,
                    )
                )
        self.dropped_out_of_scope = skipped
        self.record_success(len(out), 0.0)
        self._message = (
            f"{path.name} {len(out)} US count-cube rows from {sheets} sheet(s), "
            f"{skipped} rows for other countries skipped"
        )
        return out

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        country = clean(item.get("Country") or item.get("country"), 80) or "unknown"
        month = integer(item.get("Month"))
        year = integer(item.get("Year"))
        count = integer(item.get("Events") or item.get("events"))
        # Grouping columns vary per sheet; keep whatever the cube actually has.
        groups = {
            k: clean(v, 80)
            for k, v in item.items()
            if k not in {"Country", "country", "Month", "Year", "Events", "events", "sheet"}
            and clean(v, 80)
        }
        label = ", ".join(f"{k}={v}" for k, v in groups.items()) or "all"
        period = f"{year}-{month:02d}" if month is not None else str(year)
        headline = (
            f"{count:,} recorded demonstrations in {country} ({label}), {period}"
            if count is not None
            else f"demonstration count cube: {country} {period}"
        )
        return self.gather_observation(
            record,
            headline=headline,
            observation_type=ObservationType.ACTIVITY_CONTEXT,
            payload={
                "sheet": clean(item.get("sheet"), 40),
                "country": country,
                "year": year,
                "month": month,
                "recorded_events": count,
                "is_aggregate_count_not_events": True,
                "dimension_values": groups or None,
            },
            precision_m=None,
        )


def _looks_like_count_cube(rows: list[list[str]]) -> bool:
    """True when a sheet's header is a ``Country/.../Month/Year/Events`` cube."""

    if not rows:
        return False
    header = {c.strip().lower() for c in rows[0]}
    return "country" in header and "events" in header and "year" in header


_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def iter_workbook_rows(
    path: Path, limit: int
) -> Iterator[tuple[str, list[list[str]]]]:
    """Yield ``(sheet_name, rows)`` from an xlsx without a spreadsheet library.

    ``zipfile`` plus ``ElementTree.iterparse`` over each sheet's XML is enough to
    read a workbook this size incrementally, and it is the only way that does
    not require materialising ~500 MB of sheet XML to look at one number. The
    shared-string table is small here because a cube has a handful of distinct
    labels, so it is held in memory.

    Sheet name to sheet part is resolved positionally, which is how xlsx stores
    them (``sheet1.xml`` is the first ``<sheet>`` in ``workbook.xml``); resolving
    it through the relationship table would need a second pass for no gain here.
    """

    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        parts = sorted(
            (n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")),
            key=_sheet_part_order,
        )
        names = _sheet_names(archive)
        for index, part in enumerate(parts):
            rows = _read_sheet(archive, part, shared, limit)
            if rows:
                yield (names[index] if index < len(names) else part), rows


def _sheet_part_order(name: str) -> tuple[int, str]:
    digits = re.findall(r"(\d+)", name)
    return (int(digits[-1]) if digits else 0, name)


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    shared: list[str] = []
    if "xl/sharedStrings.xml" not in archive.namelist():
        return shared
    with archive.open("xl/sharedStrings.xml") as raw:
        for _event, element in ElementTree.iterparse(raw, events=("end",)):
            if element.tag == f"{_XL_NS}si":
                shared.append(_si_text(element))
                element.clear()
    return shared


def _si_text(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{_XL_NS}t"))


def _sheet_names(archive: zipfile.ZipFile) -> list[str]:
    if "xl/workbook.xml" not in archive.namelist():
        return []
    with archive.open("xl/workbook.xml") as raw:
        root = ElementTree.parse(raw).getroot()
    return [node.get("name", "") for node in root.iter(f"{_XL_NS}sheet")]


def _read_sheet(
    archive: zipfile.ZipFile, part: str, shared: list[str], limit: int
) -> list[list[str]]:
    rows: list[list[str]] = []
    with archive.open(part) as raw:
        for _event, element in ElementTree.iterparse(raw, events=("end",)):
            if element.tag != f"{_XL_NS}row":
                continue
            cells: list[str] = []
            for cell in element:
                if not cell.tag.endswith("}c"):
                    continue
                cells.append(_cell_value(cell, shared))
            if any(cell for cell in cells):
                rows.append(cells)
            element.clear()
            if len(rows) >= limit:
                break
    return rows


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    value = cell.find(f"{_XL_NS}v")
    if kind == "s" and value is not None:
        index = int(value.text or 0)
        return shared[index] if 0 <= index < len(shared) else ""
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_XL_NS}t"))
    return value.text if value is not None and value.text is not None else ""


# --------------------------------------------------------------------------
# D. MIDA - mass mobilisation, country level
# --------------------------------------------------------------------------


class MidaAdapter(HistoricalAdapter):
    """MIDA mass mobilisation events, 1990-2020 (``mmALL_073120_csv.tab``).

    31 columns, no coordinates. ``location`` is a coarse string - ``national``,
    ``capital``, or a region name - so these events have **no place**. They are
    emitted at country precision with ``is_national_only=True`` because a
    country-wide protest count cannot honestly be projected onto a city, and
    the country's centroid is used purely so the record is spatially filterable
    at all.

    ``participants`` is a band (``100s``, ``1000s``, ``10000s``, ``100000s``,
    ``1m``) and ``protesterviolence`` is a binary observation by the coder, both
    preserved as-is.

    **This corpus contains no United States events.** Verified against the file:
    17,145 rows across 166 countries and not one is the US; the only "United
    States" strings anywhere in it are inside the free-text ``notes`` and
    ``sources`` columns of Canadian rows ("the north american free trade
    agreement with the united states"). Filtering on the country column,
    which is what the scope originally did, therefore admits nothing and drops
    everything - 17,133 rows discarded for a filter that could never match.

    Rather than leave a dead adapter or pretend the corpus speaks to Seattle,
    the scope is widened to what the file can actually support: North American
    protest base rates, which is a real comparator for a Seattle march even
    though no row in it is one. The scope label says so.
    """

    authority = Authority.COMMUNITY
    id_fields = ("id",)
    max_records = 100000
    keep_unlocated = True
    study_bounds = NORTH_AMERICA
    scope_label = (
        "North America (comparator; this corpus contains no United States "
        "events - verified across 17,145 rows / 166 countries)"
    )

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """North America, plus the United States if a later release adds any.

        MIDA carries its own ``region`` column, so the filter uses it rather
        than maintaining a country-code list that would drift between releases.
        A US check is kept alongside it purely so that a future file version
        containing US rows is admitted instead of silently discarded.
        """

        if _norm(item.get("region")) in {"north america", "central america", "caribbean"}:
            return True
        return is_us_country(item.get("country"), item.get("ccode"))

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        country = clean(item.get("country"), 80) or "unknown"
        year = integer(item.get("year"))
        location = clean(item.get("location"), 60) or "unspecified"
        kind = clean(item.get("protesteridentity"), 80)
        head = f"MIDA gathering {country} {year}: {kind}" if kind else f"MIDA gathering {country} {year}"
        return self.gather_observation(
            record,
            headline=head[:250],
            observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
            payload={
                "event_date": f"{year}-{_pad(item.get('startmonth'))}-{_pad(item.get('startday'))}",
                "year": year,
                "end_date": f"{integer(item.get('endyear'))}-{_pad(item.get('endmonth'))}-{_pad(item.get('endday'))}",
                "country": country,
                "country_code": clean(item.get("ccode"), 8),
                "region": clean(item.get("region"), 60),
                "location_text": location,
                "is_national_only": True,
                "no_coordinates_in_corpus": True,
                "participants_text": clean(item.get("participants"), 40),
                "participants_category": clean(item.get("participants_category"), 40),
                "protester_identity": kind,
                "protester_violence": flag(item.get("protesterviolence")),
                "demands": [
                    clean(item.get(f"protesterdemand{i}"), 160) for i in range(1, 5)
                    if clean(item.get(f"protesterdemand{i}"), 160)
                ]
                or None,
                "state_responses": [
                    clean(item.get(f"stateresponse{i}"), 120) for i in range(1, 8)
                    if clean(item.get(f"stateresponse{i}"), 120)
                ]
                or None,
            },
            precision_m=200000.0,
        )


def _pad(value: Any) -> str:
    parsed = integer(value)
    return f"{parsed:02d}" if parsed is not None else "??"


# --------------------------------------------------------------------------
# E. NAVCO - mobilisation campaigns
# --------------------------------------------------------------------------


class NavcoAdapter(HistoricalAdapter):
    """NAVCO 2.1 campaigns (``NAVCO2-1_ForPublication.tab``, 102 columns).

    One row per mobilisation campaign with the fields that matter for impact
    forecasting: ``camp_goals``, ``camp_duration``, ``camp_size`` bands,
    ``prim_meth``/``resis_meth`` (primary and resistance tactics), and the
    outcome columns (``fatalities_high``/``fatalities_low``, ``repression``,
    ``camp_backlash``).

    Country-level only - there is no coordinate anywhere in the file - so these
    are emitted at country precision and exist to inform *how mobilisation
    campaigns escalate*, never *where*.

    ``navco3-0full.xlsx`` sits beside this file and is **not** NAVCO data: it is
    GDELT/CAMEO codebook material. It is deliberately not wired as a signal,
    because attributing it to NAVCO would put a false provenance on every row.
    """

    authority = Authority.COMMUNITY
    id_fields = ("id", "campyearid")
    date_fields = ("start_date", "start_year")
    max_records = 100000
    keep_unlocated = True
    scope_label = (
        "Worldwide mobilisation-campaign outcomes (comparator; this corpus "
        "contains no United States-located campaigns - verified across 2,717 "
        "rows / 146 locations, `loc_iso` has no 840)"
    )

    #: ``loc_iso`` is ISO numeric, ``loc_cow`` is Correlates of War. Both are
    #: stored as floats by whichever spreadsheet NAVCO was exported from
    #: (``840.0`` / ``750.0``), which :func:`_norm` strips before comparison.
    #: Checked anyway, because this is the column that would prove a US row if a
    #: future release contained one.
    us_location_fields = ("loc_iso", "loc_cow", "loc_cow_name", "location")

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """Every campaign is admitted; there is no in-region subset to find.

        NAVCO has no coordinates, so a US-only filter is the only thing that
        could ever exclude a row - and it excludes all of them. The dataset's
        value here is not "what happened in Seattle" but "how mobilisation
        campaigns end": ``repression``, ``camp_backlash``, ``success``,
        ``progress`` and ``camp_duration`` are the outcome variables a
        predictive layer needs in order to say anything calibrated about what a
        march produces. That is a worldwide question with a worldwide answer,
        and every record it produces is stamped ``within_operating_region=False``
        and can never reach a notification.
        """

        return True

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        # ``location`` is the place name ("Madagascar"); ``loc_iso``/``loc_cow``
        # are the codes. Reading the name out of ``loc_cow`` produced a payload
        # whose country was the number 356.
        place = clean(item.get("location"), 80) or "unknown"
        year = integer(item.get("year"))
        goals = clean(item.get("camp_goals"), 200)
        head = f"NAVCO campaign {place} {year}: {goals}" if goals else f"NAVCO campaign {place} {year}"

        return self.gather_observation(
            record,
            headline=head[:250],
            observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
            payload={
                "year": year,
                "start_date": clean(item.get("start_date"), 32),
                "start_year": integer(item.get("start_year")),
                "end_year": integer(item.get("end_year")),
                "duration": clean(item.get("camp_duration"), 40),
                "country_name": place,
                "country_iso": clean(item.get("loc_iso"), 8),
                "country_code_cow": clean(item.get("loc_cow"), 8),
                "region": clean(item.get("loc_vdem"), 60),
                "no_coordinates_in_corpus": True,
                "targets": clean(item.get("target"), 200),
                "goals": goals,
                "goals_changed": flag(item.get("goalschange")),
                "organizations": clean(item.get("camp_orgs_list"), 400),
                "primary_methods": clean(item.get("prim_meth"), 200),
                "resistance_methods": clean(item.get("resis_meth"), 200),
                "size_text": clean(item.get("camp_size"), 40),
                "size_category": clean(item.get("camp_size_cat"), 40),
                "size_n2": integer(item.get("camp_size_n2")),
                "conflict_intensity": integer(item.get("camp_confl_intensity")),
                "fatalities_low": integer(item.get("fatalities_low")),
                "fatalities_high": integer(item.get("fatalities_high")),
                "repression": clean(item.get("repression"), 200),
                "backlash": clean(item.get("camp_backlash"), 200),
                "audience_backlash": clean(item.get("audience_backlash"), 120),
                "success": clean(item.get("success"), 120),
                "progress": clean(item.get("progress"), 120),
                "in_media": flag(item.get("in_media")),
                "regime_support": clean(item.get("regime_support"), 120),
            },
            precision_m=200000.0,
        )


# --------------------------------------------------------------------------
# F. UCDP/PRIO GED - conflict events with fatalities
# --------------------------------------------------------------------------


class UcdpAdapter(HistoricalAdapter):
    """UCDP/PRIO Global Event Dataset, v24.1 (``GEDEvent_v24_1.csv``, 256 MB).

    ~260k georeferenced conflict events with fatalities split by party. Two
    reasons this is in the corpus:

    * It is the reference standard for *what an observed violent event with a
      fatality count looks like as a record*, which the presentation layer needs
      in order to describe one without exaggeration.
    * It is a worldwide dataset, so the region filter keeps very few rows. That
      is the correct outcome: a conflict event in Sudan is not a Seattle event,
      and letting it through would be the single largest source of fabricated
      regional alarm in the system.

    ``date_start`` is used rather than ``year``, because ``year`` alone is
    ambiguous across a multi-year conflict while ``date_start`` is a real
    timestamp. ``date_prec`` is preserved: a "1" means the day is known, a "7"
    means only the year is - and the ledger should be able to tell the
    difference.

    ``ged241-csv.zip`` is the same data compressed and is not read again.
    """

    source_type = SourceType.THIRD_PARTY_DATABASE
    authority = Authority.SEMI_OFFICIAL
    id_fields = ("id",)
    date_fields = ("date_start", "date_end")
    max_records = 40000
    study_bounds = CONTIGUOUS_US
    scope_label = "United States (contiguous)"

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """Keep US events only.

        The GED is 350,000 events and this region contains essentially none of
        them, which is the correct answer: a conflict event in Sudan is not a
        Seattle event. Filtering on the ``country`` column rather than
        coordinates matters because the file is sorted by country and Afghanistan
        alone accounts for 40,000 rows - a coordinate filter would still have to
        read all of them.
        """

        return is_us_country(item.get("country"))

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        best = integer(item.get("best"))
        country = clean(item.get("country"), 80) or "unknown"
        conflict = clean(item.get("conflict_name"), 120)
        side_a = clean(item.get("side_a"), 80)
        side_b = clean(item.get("side_b"), 80)
        head = " | ".join(p for p in (conflict, f"{side_a} v {side_a and side_b}") if p)
        if best:
            head += f" - {best} fatalities"
        return self.gather_observation(
            record,
            headline=(head or f"conflict event in {country}")[:250],
            observation_type=ObservationType.VIOLENCE_EVENT,
            payload={
                "ged_id": clean(item.get("id"), 24),
                "year": integer(item.get("year")),
                "type_of_violence": integer(item.get("type_of_violence")),
                "conflict_name": conflict,
                "dyad_name": clean(item.get("dyad_name"), 120),
                "side_a": side_a,
                "side_b": side_b,
                "country": country,
                "adm_1": clean(item.get("adm_1"), 120),
                "adm_2": clean(item.get("adm_2"), 120),
                "where_coordinates": clean(item.get("where_coordinates"), 120),
                "where_precision": integer(item.get("where_prec")),
                "date_precision": integer(item.get("date_prec")),
                "deaths_best": best,
                "deaths_high": integer(item.get("high")),
                "deaths_low": integer(item.get("low")),
                "deaths_side_a": integer(item.get("deaths_a")),
                "deaths_side_b": integer(item.get("deaths_b")),
                "deaths_civilians": integer(item.get("deaths_civilians")),
                "deaths_unknown": integer(item.get("deaths_unknown")),
                "source_count": integer(item.get("number_of_sources")),
                "source_headline": clean(item.get("source_headline"), 200),
                "observed_event_not_a_prediction": True,
            },
            precision_m=5000.0,
        )


# --------------------------------------------------------------------------
# G. GDELT - headerless, tab-delimited, no published header
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GdeltLayout:
    """Column indices for a GDELT export, verified against the file.

    The export carries **no header row** and its 58-column layout is not
    self-describing. Rather than assume a schema version and silently
    mislabel fields, the indices below are *discovered* from the data and the
    evidence is kept.

    That mattered. The published GDELT 2.0 Events layout puts
    ``ActionGeo_Lat``/``ActionGeo_Long`` at 35/36; in these files the
    coordinates are at **39/40**, with the place name at 36. A reader trusting
    the published schema reads CAMEO codes as latitude, gets plausible-looking
    numbers in range, and drops all 143,447 rows as "out of region" without
    raising anything. Discovery found the truth; the published schema would
    have been confidently wrong.

    The discriminator that separates the two is *geography*, not parseability.
    Both candidate pairs parse as floats and both sit inside valid ranges, so
    range checks cannot choose between them. What cannot be coincidence is one
    of them placing a large share of a world newswire inside the study area.
    """

    record_id: int
    event_date: int
    source_url: int
    #: ``(latitude, longitude, place_name)`` triples, best-scoring first. The
    #: first is treated as the primary action location.
    geo_blocks: tuple[tuple[int, int, int], ...] = ()
    columns: int = 0
    #: Pairs read by :func:`discover_gdelt_layout`, kept so a payload can be
    #: audited without re-reading the file.
    evidence: dict[str, Any] | None = None

    @property
    def verified(self) -> bool:
        return bool(self.record_id >= 0 and self.event_date >= 0 and self.geo_blocks)


_GDELT_LAYOUT_CACHE: dict[tuple[str, int], GdeltLayout] = {}

#: Enough parsed coordinate pairs to judge a candidate. A parse-rate threshold
#: is the wrong test here: GDELT only geocodes roughly half of the stories it
#: ingests, so the *true* pair is legitimately sparse and a high-rate gate
#: rejects it while accepting a pair of small integers that always parse.
_MIN_GEO_PAIRS = 30


def discover_gdelt_layout(
    path: Path,
    sample_rows: int = 4000,
    bounds: tuple[float, float, float, float] = NORTH_AMERICA,
) -> GdeltLayout:
    """Find the record id, date, URL and coordinate columns of a GDELT export.

    Discovery rather than assumption, because the file has no header and its
    layout varies between GDELT releases. The rules are all checkable
    invariants of the data itself:

    * the id column is the one whose values are unique integers;
    * the date column's values are all ``YYYYMMDD``;
    * the URL column's values all begin with ``http``;
    * a coordinate column pair is *adjacent*, yields at least
      :data:`_MIN_GEO_PAIRS` parsed pairs, and sits within its valid range -
      with the latitude preceding the longitude, which the range check
      distinguishes because latitudes never exceed 90 degrees;
    * a pair is scored by **how much of a world newswire it places inside
      ``bounds``**, which is what actually distinguishes coordinates from the
      numeric columns that surround them;
    * the place name is the nearest preceding column that looks like prose.

    A layout with no in-bounds evidence is returned unverified, and the adapter
    emits no geometry rather than a guessed one.
    """

    key = (str(path), path.stat().st_size)
    cached = _GDELT_LAYOUT_CACHE.get(key)
    if cached is not None:
        return cached

    width = 0
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            width = max(width, len(row))
            if len(rows) < sample_rows:
                rows.append(row)
            else:
                break
    if not rows or not width:
        return GdeltLayout(record_id=-1, event_date=-1, source_url=-1)

    columns = list(zip(*rows))
    record_id = _first_index(columns, lambda col: _unique_integers(col))
    event_date = _first_index(columns, lambda col: _all_match(col, r"\d{8}"))
    source_url = _first_index(
        columns, lambda col: _all_match(col, r"https?://\S+", ignore_empty=True, full=False)
    )

    scored: list[tuple[int, int, int, int]] = []
    for index in _coord_candidates(columns, width):
        first = _column(columns, index)
        second = _column(columns, index + 1)
        pairs = [
            (num(a), num(b))
            for a, b in zip(first, second)
            if num(a) is not None and num(b) is not None
        ]
        forward = _hits_in_bounds(pairs, True, bounds)
        reverse = _hits_in_bounds(pairs, False, bounds)
        if forward >= reverse:
            lat, lon, hits = index, index + 1, forward
        else:
            lat, lon, hits = index + 1, index, reverse
        scored.append((hits, len(pairs), lat, lon))

    scored.sort(reverse=True)
    evidence: dict[str, Any] = {
        "bounds": list(bounds),
        "sampled_rows": len(rows),
        "candidates": [
            {"lat": lat, "lon": lon, "in_bounds": hits, "parsed_pairs": pairs}
            for hits, pairs, lat, lon in scored[:6]
        ],
    }

    best_hits = scored[0][0] if scored else 0
    if best_hits <= 0:
        # Nothing places a newswire inside the study area. That is a real
        # finding about the file, and guessing anyway is how 143,447 rows get
        # discarded as "out of region" while the feed looks healthy.
        evidence["verdict"] = "no coordinate pair placed any sampled row inside bounds"
        layout = GdeltLayout(
            record_id=record_id,
            event_date=event_date,
            source_url=source_url,
            columns=width,
            evidence=evidence,
        )
        _GDELT_LAYOUT_CACHE[key] = layout
        return layout

    # Blocks that place a comparable *share* of their parsable pairs in bounds
    # are all plausible locations. The comparison has to be on the share, not
    # the absolute count, because the blocks geocode at different rates: on
    # 2020-05-30 ActionGeo has 2,153 in-bounds rows from 3,128 parsed and
    # SourceGeo 2,593 from 3,780 - the same 69% of their own data, different
    # totals - and ranking on totals silently promotes the wrong one.
    #
    # Within the band, *column order* decides which is primary: in a GDELT
    # export the action location precedes the actor and source locations, and
    # "where the action happened" is the question a Seattle impact platform is
    # asking. Ranking purely by hits answered a different one.
    best_share = max(h / p for h, p, _lat, _lon in scored if p)
    band = [
        s
        for s in scored
        if s[1] and (s[0] / s[1]) >= best_share * 0.85 and s[0] >= 50
    ]
    band.sort(key=lambda s: s[2])
    geo: list[tuple[int, int, int]] = []
    for _hits, _pairs, lat, lon in band:
        if any(lat == g[0] for g in geo):
            continue  # both orientations of one column pair, not two locations
        geo.append((lat, lon, _place_name_column(columns, lat)))
        if len(geo) >= 4:
            break

    primary_hits, primary_pairs = next(
        (h, p) for h, p, lat, _lon in band if lat == geo[0][0]
    )
    evidence["verdict"] = (
        f"primary geo pair {geo[0][0]}/{geo[0][1]} placed {primary_hits} of "
        f"{primary_pairs} parsed pairs inside bounds; "
        f"{len(geo)} location block(s) retained in column order"
    )
    layout = GdeltLayout(
        record_id=record_id,
        event_date=event_date,
        source_url=source_url,
        geo_blocks=tuple(geo),
        columns=width,
        evidence=evidence,
    )
    _GDELT_LAYOUT_CACHE[key] = layout
    return layout


def _place_name_column(columns: list[tuple[str, ...]], lat_index: int) -> int:
    """Nearest column before ``lat_index`` that reads like a place name.

    Scans back up to five columns. In these exports the full name sits three
    columns ahead of the latitude (``36`` for a pair at ``39``) with a short
    camel-case code in between, so a fixed offset picks the wrong one.
    """

    for back in range(1, 6):
        index = lat_index - back
        values = [v for v in _column(columns, index) if v]
        if len(values) < 20:
            continue
        if all(num(v) is not None for v in values[:200]):
            continue  # numeric, not a name
        if sum("," in v for v in values[:200]) >= len(values[:200]) * 0.2:
            return index
    return -1


def _column(columns: list[tuple[str, ...]], index: int) -> list[str]:
    return list(columns[index]) if 0 <= index < len(columns) else []


def _first_index(columns: list[tuple[str, ...]], predicate: Any) -> int:
    for index, column in enumerate(columns):
        try:
            if predicate(column):
                return index
        except (TypeError, ValueError):
            continue
    return -1


def _unique_integers(column: Sequence[str]) -> bool:
    values = [v for v in column if v]
    if len(values) < len(column) * 0.9 or len(set(values)) != len(values):
        return False
    return all(re.fullmatch(r"-?\d+", v) for v in values)


def _all_match(
    column: Sequence[str], pattern: str, ignore_empty: bool = False, full: bool = True
) -> bool:
    """Does every (non-empty) value in the column match ``pattern``?

    ``full=False`` is required for anything with a prefix - ``re.fullmatch``
    against ``https?://`` can never succeed, because a URL does not end with
    its scheme. Getting that wrong is invisible: the URL column simply reports
    as not found and every observation is written without the link that would
    let anyone audit it against the original article.
    """

    regex = re.compile(pattern)
    values = [v for v in column if v or not ignore_empty]
    if not values:
        return False
    check = regex.fullmatch if full else regex.match
    return all(bool(check(v)) for v in values[:2000])


def _coord_candidates(columns: list[tuple[str, ...]], width: int) -> list[int]:
    """Left-hand column index of every adjacent pair that could be coordinates.

    The gate is *evidence volume*, not parse rate. GDELT geocodes only about
    85% of the stories in one of these files and considerably less in others, so
    a percentage threshold rejects the genuine pair while happily accepting a
    pair of small integers that happen to parse every time.

    Both columns must fit in [-180, 180], and at least one must fit in [-90,
    90] - which is what makes the pair a coordinate pair rather than two
    unrelated integers. Which of the two is the latitude is decided later, by
    which orientation places more of a world newswire inside the study area;
    it is not decided here, because "the longitude must lie within +/- 90
    degrees" is not a rule. It is false for most of the planet: Melbourne is at
    144.97 E and Seattle at 122.3 W, so the test rejects every real coordinate
    pair on Earth.
    """

    out: list[int] = []
    for index in range(width - 1):
        first = _column(columns, index)
        second = _column(columns, index + 1)
        if not first or not second:
            continue
        pairs = [
            (num(a), num(b))
            for a, b in zip(first, second)
            if num(a) is not None and num(b) is not None
        ]
        if len(pairs) < _MIN_GEO_PAIRS:
            continue
        left = [a for a, _ in pairs]
        right = [b for _, b in pairs]
        if not all(-180.0 <= v <= 180.0 for v in left):
            continue
        if not all(-180.0 <= v <= 180.0 for v in right):
            continue
        if not (all(-90.0 <= v <= 90.0 for v in left) or all(-90.0 <= v <= 90.0 for v in right)):
            continue
        out.append(index)
    return out


def _hits_in_bounds(pairs: list[tuple[float, float]], lat_first: bool, bounds: tuple[float, float, float, float]) -> int:
    min_lon, min_lat, max_lon, max_lat = bounds
    total = 0
    for a, b in pairs:
        lat, lon = (a, b) if lat_first else (b, a)
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            total += 1
    return total


class GdeltAdapter(HistoricalAdapter):
    """GDELT news event exports, ``2020-05-30``..``2020-06-01``.

    Used as one signal: **how much news coverage a place generated on a given
    day**, which during the George Floyd protests is a usable proxy for how much
    was happening there. Not a source of facts about events - only about
    coverage of them, and the payload says so.

    Three properties of the file that make a naive reader produce wrong data
    rather than an error:

    1. ``.CSV`` extension, tab-delimited.
    2. **No header row.** The first record is data.
    3. The column layout is not self-describing, so
       :func:`discover_gdelt_layout` verifies the indices from the data and the
       resulting evidence is stored on every record.

    Each file is a single day, and the three ZIP siblings are byte-equivalent
    compressions of the three plain files, so only the plain ones are read.
    """

    authority = Authority.COMMUNITY
    source_type = SourceType.ESTABLISHED_NEWS
    has_header = False
    csv_delimiter = "\t"
    max_records = 20000
    default_precision_m = 5000.0
    study_bounds = NORTH_AMERICA
    scope_label = "North America (comparator outside the operating region)"
    #: A news record with no place cannot be compared to anything, so unlike the
    #: area-aggregate corpora this one drops unlocated rows.
    keep_unlocated = False

    def _layout(self) -> GdeltLayout:
        # The signal spans three daily exports, so the layout is resolved per
        # file being read rather than once against the first.
        path = self._current_path or self.resolve()
        if path is None:
            return GdeltLayout(record_id=-1, event_date=-1, source_url=-1)
        return discover_gdelt_layout(path, bounds=self.study_bounds)

    def _record_id(self, item: dict[str, Any]) -> str:
        layout = self._layout()
        if layout.record_id < 0:
            return ""
        return f"{self.source_id}:{item.get(f'col_{layout.record_id}')}"

    def _event_time(self, item: dict[str, Any]) -> datetime | None:
        layout = self._layout()
        if layout.event_date < 0:
            return None
        return parse_historic_date(item.get(f"col_{layout.event_date}"))

    def _source_url(self, item: dict[str, Any]) -> str | None:
        layout = self._layout()
        if layout.source_url < 0:
            return None
        return clean(item.get(f"col_{layout.source_url}"), 400)

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        layout = self._layout()
        for lat, lon, name_index in layout.geo_blocks:
            geometry = point_from_latlon(item.get(f"col_{lat}"), item.get(f"col_{lon}"))
            if geometry:
                return geometry
        return None

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        layout = self._layout()
        date = clean(item.get(f"col_{layout.event_date}"), 16) if layout.event_date >= 0 else None
        # The place name belongs to whichever geo block actually placed this
        # row, so it is read from that block rather than from the first block
        # in the layout, whose coordinates are frequently empty for this row.
        place = None
        for lat, lon, name_index in layout.geo_blocks:
            if not point_from_latlon(item.get(f"col_{lat}"), item.get(f"col_{lon}")):
                continue
            place = clean(item.get(f"col_{name_index}"), 120) if name_index >= 0 else None
            if place:
                break
        return self.gather_observation(
            record,
            headline=f"GDELT news record {date} {place or ''}".strip()[:250],
            observation_type=ObservationType.NEWS_ARTICLE,
            payload={
                "gdelt_record_id": clean(item.get(f"col_{layout.record_id}"), 24)
                if layout.record_id >= 0
                else None,
                "publish_date": date,
                "place": place,
                "signals_news_coverage_not_events": True,
                "schema_layout": {
                    "record_id": layout.record_id,
                    "date": layout.event_date,
                    "source_url": layout.source_url,
                    "geo_blocks": [list(b) for b in layout.geo_blocks],
                    "columns": layout.columns,
                    "verified_against_file": layout.verified,
                    "evidence": layout.evidence,
                },
                "raw_columns": [item.get(f"col_{i}", "") for i in range(layout.columns)],
            },
            precision_m=5000.0,
        )


# --------------------------------------------------------------------------
# H. mobility and economic activity
# --------------------------------------------------------------------------


class GoogleMobilityAdapter(HistoricalAdapter):
    """Google Community Mobility, daily change from the pre-pandemic baseline.

    Three files at different geographies:

    ``Google_Mobility_City_Daily.csv``    keyed by ``cityid``
    ``Google_Mobility_County_Daily.csv``  keyed by ``countyfips``
    ``Google_Mobility_State_Daily.csv``   keyed by ``statefips``

    Only the city file can be placed, through ``GeoIDs_City.csv``. The county
    and state files keep their FIPS code as an identifier and are emitted
    *unlocated* rather than being pinned to a county centroid - a county is an
    area of up to 6,000 km2 and projecting one value onto one point would
    invent a spatial resolution the measurement does not have.

    This is the strongest available proxy for real movement in a place, which
    makes it a genuine predictor of whether a road closure or transit
    disruption will actually be felt. Values are fractional change
    (``-.00286`` means -0.286%), so they are kept as decimals, not rescaled.

    ``Google_Mobility_County_Daily.csv.gz`` is byte-equivalent to the plain
    county file and is not read twice.
    """

    authority = Authority.SEMI_OFFICIAL
    max_records = 200000
    keep_unlocated = True
    identity_fields = ("cityid", "countyfips", "statefips", "year", "month", "day")
    date_fields = ("date", "year")
    scope_label = (
        "Washington State rows, plus the six US cities GeoIDs_City.csv can resolve"
    )

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """Washington, or a city ``GeoIDs_City.csv`` can actually place.

        The lookup table covers six cities - New York, Los Angeles, Chicago,
        Houston, Phoenix, Philadelphia - and **none of them is Seattle**. So for
        the city file the honest scope is those six as comparators; the only rows
        that say anything about the operating region are the Washington county
        and state rows, which stay because the state aggregate is still a real
        signal about Seattle, just at a coarser geography than the file suggests.
        """

        if _norm(item.get("statefips")) == WA_STATE_FIPS:
            return True
        if _norm(item.get("countyfips")).startswith(_US_FIPS_PREFIX):
            return True
        city = self.city_ids.get(item.get("cityid"))
        return city is not None

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        city = self.city_ids.get(item.get("cityid"))
        if city:
            return point_from_latlon(city.get("lat"), city.get("lon"))
        return None

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        year, month, day = (
            integer(item.get("year")),
            integer(item.get("month")),
            integer(item.get("day")),
        )
        city = self.city_ids.get(item.get("cityid"))
        cityid = clean(item.get("cityid"), 16)
        countyfips = clean(item.get("countyfips"), 16)
        statefips = clean(item.get("statefips"), 8)

        place = (
            clean(city.get("cityname"), 80)
            if city
            else (f"FIPS {countyfips}" if countyfips else (f"FIPS {statefips}" if statefips else ""))
        )
        # Verified column names: ``gps_retail_and_recreation`` and friends, no
        # ``_pct`` suffix. A spelled-differently list produces an empty
        # ``movements`` dict for every row and the adapter silently emits
        # nothing - 965 records read, zero observations, no error. The
        # legacy un-prefixed and ``_pct`` variants are kept because the same
        # corpus was published under three header conventions across releases.
        movements = {}
        for stem in (
            "retail_and_recreation",
            "grocery_and_pharmacy",
            "parks",
            "transit_stations",
            "workplaces",
            "residential",
            "away_from_home",
        ):
            for column in (f"gps_{stem}", stem, f"gps_{stem}_pct", f"{stem}_pct"):
                value = num(item.get(column))
                if value is not None:
                    movements[stem] = value
                    break
        if not movements:
            return None

        away = movements.get("away_from_home")
        transit = movements.get("transit_stations")
        period = (
            f"{year}-{month:02d}-{day:02d}" if None not in (year, month, day) else "undated"
        )
        return self.gather_observation(
            record,
            headline=f"mobility {place} {period}"
            + (f": away-from-home {away:+.1%}" if away is not None else ""),
            observation_type=ObservationType.ACTIVITY_CONTEXT,
            payload={
                "date": None if None in (year, month, day) else period,
                "city_id": cityid,
                "city_name": clean(city.get("cityname"), 80) if city else None,
                "city_state": clean(city.get("stateabbrev"), 8) if city else None,
                "county_fips": countyfips,
                "state_fips": statefips,
                "geography": "city" if city else ("county" if countyfips else "state"),
                "is_area_aggregate_not_a_point": not bool(city),
                "fractional_change_vs_prepandemic_baseline": movements,
                "away_from_home_change": away,
                "transit_stations_change": transit,
                "units": "fraction of baseline, e.g. -0.05 is 5% below baseline",
                "spend_mobility_is_context_never_event": True,
            },
            precision_m=20000.0 if city else None,
        )


class AffinityAdapter(HistoricalAdapter):
    """Affinity card-spending change by city (``Affinity_City_Daily.csv``).

    24 columns of spending index changes by category, plus ``freq`` (spending
    frequency) and a ``provisional`` flag. Placed through ``GeoIDs_City.csv``,
    which is the only reason these rows have coordinates at all.

    The missing-value trap is the reason this adapter is written the way it is:
    absent figures are a literal ``.``, which ``float()`` refuses and every
    naive coercion turns into ``0.0``. That would register a total spending
    collapse on exactly the days the card data is thinnest - the kind of error
    that looks like a real signal. Here ``.`` becomes ``None`` and is dropped,
    so a sparse row is honestly sparse.

    ``provisional`` is carried through: preliminary figures are not the same
    kind of evidence as final ones.
    """

    authority = Authority.SEMI_OFFICIAL
    max_records = 200000
    keep_unlocated = True
    identity_fields = ("cityid", "year", "month", "day")
    date_fields = ("date", "year")
    scope_label = (
        "the six US cities GeoIDs_City.csv can resolve (none of them in the "
        "operating region)"
    )

    #: Category column -> short name. Everything else in the file is a
    #: ``spend_*`` column; the map keeps the payload readable.
    SPEND_CATEGORIES = {
        "spend_all": "total",
        "spend_aap": "apparel",
        "spend_acf": "auto_catch_fire",
        "spend_aer": "aerial",
        "spend_apg": "appliance",
        "spend_durables": "durables",
        "spend_nondurables": "nondurables",
        "spend_grf": "groceries",
        "spend_gen": "general_merch",
        "spend_hic": "healthcare",
        "spend_hcs": "home_services",
        "spend_inperson": "in_person",
        "spend_inpersonmisc": "in_person_misc",
        "spend_remoteservices": "remote_services",
        "spend_sgh": "service_general",
        "spend_tws": "travel_serv",
        "spend_retail_w_grocery": "retail_with_groceries",
        "spend_retail_no_grocery": "retail_without_groceries",
    }

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """Every placeable row is one of the six lookup cities.

        Affinity only ships a city file, and the ``cityid`` lookup resolves six
        US cities, none of them in Washington. There is no Washington spending
        figure anywhere in this corpus, which is worth stating plainly rather
        than working around by pinning a national figure to Seattle.
        """

        return self.city_ids.get(item.get("cityid")) is not None

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        city = self.city_ids.get(item.get("cityid"))
        if city:
            return point_from_latlon(city.get("lat"), city.get("lon"))
        return None

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        city = self.city_ids.get(item.get("cityid"))
        cityid = clean(item.get("cityid"), 16)
        year, month, day = (
            integer(item.get("year")),
            integer(item.get("month")),
            integer(item.get("day")),
        )
        spending = {
            short: num(item.get(column))
            for column, short in self.SPEND_CATEGORIES.items()
            if num(item.get(column)) is not None
        }
        if not spending:
            return None
        name = clean(city.get("cityname"), 80) if city else f"cityid {cityid}"
        total = spending.get("total")
        period = (
            f"{year}-{month:02d}-{day:02d}" if None not in (year, month, day) else "undated"
        )
        return self.gather_observation(
            record,
            headline=f"spending {name} {period}"
            + (f": total {total:+.1%}" if total is not None else ""),
            observation_type=ObservationType.ACTIVITY_CONTEXT,
            payload={
                "date": None if None in (year, month, day) else period,
                "city_id": cityid,
                "city_name": clean(city.get("cityname"), 80) if city else None,
                "city_state": clean(city.get("stateabbrev"), 8) if city else None,
                "spending_change_by_category": spending or None,
                "frequency_change": num(item.get("freq")),
                "provisional": flag(item.get("provisional")),
                "missing_marker_in_source": ".",
                "units": "fraction of baseline",
            },
            precision_m=20000.0 if city else None,
        )


class FipsAdapter(HistoricalAdapter):
    """County-level weekly activity, keyed by ``countyfips``.

    Shared base for the Employment and Womply corpora, which differ only in
    what they measure. Neither has coordinates and neither gets any: a county is
    an area of up to 6,000 km2, so the FIPS code is preserved as an identifier
    and the observation is emitted unlocated. Anything that wants to use these
    must join on FIPS itself.

    The two corpora together cover all 3,143 US counties for all of 2020, so the
    scope filter keeps only Washington's 39 counties - King, Pierce, Snohomish
    and the rest. Without that, the per-poll record budget would be spent on
    Alabama and the Washington rows would never be reached.

    ``countyfips`` is stored without its leading zero (``"1001"``, not
    ``"01001"``), which does not affect Washington because its state code is 53
    and needs no padding.
    """

    max_records = 200000
    keep_unlocated = True
    id_fields = ()
    identity_fields = ("countyfips", "year", "month", "day_endofweek")
    date_fields = ("date", "year")
    scope_label = "Washington State counties (county-level aggregate, no coordinates)"
    #: Subclasses name the columns they care about; everything else is dropped.
    value_fields: tuple[str, ...] = ()
    label: str = "county activity"

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        return _norm(item.get("countyfips")).startswith(_US_FIPS_PREFIX)

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        year = integer(item.get("year"))
        month = integer(item.get("month"))
        week = integer(item.get("day_endofweek"))
        fips = clean(item.get("countyfips"), 16)
        values = {
            field: num(item.get(field))
            for field in self.value_fields
            if num(item.get(field)) is not None
        }
        if not values:
            return None
        primary = next(iter(values.items()))
        return self.gather_observation(
            record,
            headline=f"{self.label} county {fips} {year}-{month:02d}w{week}: "
            f"{primary[0]}={primary[1]:+.3f}",
            observation_type=ObservationType.ACTIVITY_CONTEXT,
            payload={
                "year": year,
                "month": month,
                "week_of_month": week,
                "county_fips": fips,
                "geography": "county",
                "no_coordinates_in_corpus": True,
                "is_area_aggregate_not_a_point": True,
                "changes": values,
                "units": "fraction change vs baseline",
            },
            precision_m=None,
        )


class EmploymentAdapter(FipsAdapter):
    """Weekly county employment change (``Employment_County_Weekly_2020.csv``).

    22 columns: total employment plus size-bucket splits (``emp_ss40`` etc.,
    firms under 40 employees and up) and quarterly wage splits. The size-bucket
    splits matter for impact work because small-firm employment is the part of
    an economy that a street closure disrupts first.
    """

    label = "employment"
    authority = Authority.SEMI_OFFICIAL
    value_fields = (
        "emp",
        "emp_ss40",
        "emp_ss60",
        "emp_ss65",
        "emp_ss70",
        "emp_wage_q1",
        "emp_wage_q2",
        "emp_wage_q3",
        "emp_wage_q4",
    )


class WomplyAdapter(FipsAdapter):
    """Weekly small-business activity by county (``Womply_County_Weekly.csv``).

    Two columns - ``merchants_all`` and ``revenue_all`` - and that is the whole
    point: a count of small businesses transacting is a far more sensitive
    early-warning signal of local economic disruption than official employment
    statistics, which lag by weeks.
    """

    label = "small-business activity"
    authority = Authority.SEMI_OFFICIAL
    value_fields = ("merchants_all", "revenue_all")


# --------------------------------------------------------------------------
# I. municipal crime and 311 damage
# --------------------------------------------------------------------------


class MunicipalCrimeAdapter(HistoricalAdapter):
    """Municipal 2020 unrest crime and 311 service requests, six cities.

    Six files and **six different schemas**. They are not two dialects to be
    reconciled with ``or`` fallbacks - each city's open-data export names the
    same concepts differently, so :data:`DIALECTS` declares the verified column
    for each role per file. Guessing with fallbacks is what silently drops a
    whole city's rows: a missing ``latitude`` does not error, it produces an
    unlocated observation, and the dataset quietly halves.

    Two findings that change how these files may be used:

    * ``chicago_311_infrastructure_damage_2020.csv`` is **4,770 rows of
      "Graffiti Removal Request" and nothing else**. Despite the filename it
      contains no broken signals, blocked intersections or downed lights, so it
      is evidence about vandalism reporting, not about infrastructure damage.
      The payload records the request type so this cannot be misread later.
    * ``los_angeles_civil_unrest_crimes_2020.csv`` is dominated by burglary and
      vandalism - which *is* the damage evidence, since vandalism is property
      damage by definition.

    What these files are genuinely good for is answering "what actually follows a
    large gathering in a US city" with each city's own records rather than a
    guess. They are comparators for Seattle, never Seattle itself, and every
    observation says so.

    ``status``/``closed_date`` are carried because "311 report filed" and "damage
    repaired" are different facts, and LAPD's ``part_1_2`` distinguishes serious
    crimes from non-criminal reportable offences.
    """

    source_type = SourceType.OFFICIAL_MACHINE_READABLE
    authority = Authority.OFFICIAL
    max_records = 100000
    study_bounds = CONTIGUOUS_US
    scope_label = (
        "Seattle (in region) plus US comparator cities: Chicago, New York, "
        "Los Angeles, San Francisco, Washington DC"
    )
    id_fields = ()
    identity_fields = ("__dialect_id",)

    #: Role -> column name, per file. Roles are fixed; only the column names
    #: vary. Every entry below was read off the file it names.
    DIALECTS: dict[str, dict[str, str]] = {
        "chicago_311_infrastructure_damage_2020.csv": {
            "kind": "sr_type",
            "code": "sr_short_code",
            "id": "sr_number",
            "date": "created_date",
            "closed": "closed_date",
            "address": "street_address",
            "area": "community_area",
            "district": "police_district",
            "beat": "police_beat",
            "status": "status",
            "city": "city",
            "report_kind": "service_request",
            "extra": "created_department",
        },
        "nyc_311_infrastructure_damage_2020.csv": {
            "kind": "complaint_type",
            "code": "descriptor",
            "id": "unique_key",
            "date": "created_date",
            "closed": "closed_date",
            "address": "incident_address",
            "area": "community_board",
            "district": "council_district",
            "beat": "police_precinct",
            "status": "status",
            "city": "city",
            "report_kind": "service_request",
            "extra": "agency",
        },
        "chicago_civil_unrest_crimes_2020.csv": {
            "kind": "primary_type",
            "code": "iucr",
            "id": "case_number",
            "date": "date",
            "address": "block",
            "area": "community_area",
            "district": "district",
            "beat": "beat",
            "flag": "arrest",
            "city": "Chicago",
            "report_kind": "crime_report",
        },
        "los_angeles_civil_unrest_crimes_2020.csv": {
            "kind": "crm_cd_desc",
            "code": "crm_cd",
            "id": "dr_no",
            "date": "date_occ",
            "reported": "date_rptd",
            "address": "location",
            "area": "area_name",
            "district": "rpt_dist_no",
            "status": "status_desc",
            "category": "part_1_2",
            "premises": "premis_desc",
            "weapon": "weapon_desc",
            "city": "Los Angeles",
            "report_kind": "crime_report",
        },
        "nyc_civil_unrest_crimes_2020.csv": {
            "kind": "ofns_desc",
            "code": "ky_cd",
            "id": "cmplnt_num",
            "date": "cmplnt_fr_dt",
            "reported": "rpt_dt",
            "address": "loc_of_occur_desc",
            "area": "boro_nm",
            "beat": "patrol_boro",
            "category": "law_cat_cd",
            "premises": "prem_typ_desc",
            "city": "New York",
            "report_kind": "crime_report",
        },
        "san_francisco_civil_unrest_crimes_2020.csv": {
            "kind": "incident_description",
            "code": "incident_code",
            "id": "incident_number",
            "date": "incident_date",
            "address": "intersection",
            "area": "analysis_neighborhood",
            "district": "police_district",
            "resolution": "resolution",
            "city": "San Francisco",
            "report_kind": "crime_report",
        },
        # Seattle is the only one of these inside the operating region, so its
        # rows are admitted as ``region`` and everything else in this table as
        # comparators. It is also the only NIBRS export in the set: everyone
        # else uses its own local offence vocabulary, and ``offense_sub_category``
        # is a far finer grain than LAPD's ``crm_cd_desc``.
        "seattle_spd_crimes_2020_unrest.csv": {
            "kind": "nibrs_offense_code_description",
            "code": "nibrs_offense_code",
            "id": "report_number",
            "date": "offense_date",
            "reported": "report_date_time",
            "address": "block_address",
            "area": "neighborhood",
            "district": "precinct",
            "beat": "beat",
            "sector": "sector",
            "category": "offense_category",
            "against": "nibrs_crime_against_category",
            "group": "nibrs_group_a_b",
            "shooting": "shooting_type_group",
            "census_block": "census_block_2020",
            "city": "Seattle",
            "report_kind": "crime_report",
            "schema": "NIBRS",
        },
        # MPD's export. ``CCN`` is the case number, ``OFFENSE`` the offence
        # description, and the coordinates are in ``LATITUDE``/``LONGITUDE``.
        # ``-`` appears in ``METHOD``/``SHIFT`` for rows where the field was
        # never filled in, which ``clean`` maps to None rather than to a value.
        "dc_mpd_crimes_2020_unrest.csv": {
            "kind": "OFFENSE",
            "code": "METHOD",
            "id": "CCN",
            "date": "START_DATE",
            "reported": "REPORT_DAT",
            "address": "BLOCK",
            "area": "NEIGHBORHOOD_CLUSTER",
            "district": "DISTRICT",
            "extra": "SHIFT",
            "ward": "WARD",
            "city": "Washington",
            "report_kind": "crime_report",
            "schema": "MPD",
            "lat": "LATITUDE",
            "lon": "LONGITUDE",
        },
        "dc_mpd_crimes_2021_capitol.csv": {
            "kind": "OFFENSE",
            "code": "METHOD",
            "id": "CCN",
            "date": "START_DATE",
            "reported": "REPORT_DAT",
            "address": "BLOCK",
            "area": "NEIGHBORHOOD_CLUSTER",
            "district": "DISTRICT",
            "extra": "SHIFT",
            "ward": "WARD",
            "city": "Washington",
            "report_kind": "crime_report",
            "schema": "MPD",
            "lat": "LATITUDE",
            "lon": "LONGITUDE",
        },
    }

    def _current_file(self) -> Path | None:
        """The file this row actually came from.

        Not ``resolve()``. This signal is seven open-data exports with seven
        schemas, and keying the dialect off the *first* declared file means
        every other city's rows are read through Chicago's column names - which
        does not raise, it simply produces empty fields and an unlocated
        observation for all of them.
        """

        return self._current_path or self.resolve()

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Coordinates named by this file's dialect.

        The generic snapshot reader only knows ``latitude``/``lat`` and
        ``longitude``/``lon``. MPD's export spells them ``LATITUDE`` and
        ``LONGITUDE`` in upper case, so every one of its rows would arrive
        unlocated - which for a corpus whose entire value is place-specific
        damage evidence means discarding the file's reason to exist without a
        single error message.
        """

        dialect = self._dialect()
        lat, lon = dialect.get("lat"), dialect.get("lon")
        if lat and lon:
            geometry = point_from_latlon(item.get(lat), item.get(lon))
            if geometry:
                return geometry
        return super()._geometry_of(item)

    def _dialect(self) -> dict[str, str]:
        """Column map for the file this poll actually read.

        Keyed by filename because that is the only thing that distinguishes the
        schemas; an unmatched file falls back to the Chicago 311 map and
        reports ``dialect_recognised=False`` rather than pretending.
        """

        path = self._current_file()
        name = path.name if path else ""
        known = name in self.DIALECTS
        return dict(self.DIALECTS.get(name) or self.DIALECTS["chicago_311_infrastructure_damage_2020.csv"])

    def _record_id(self, item: dict[str, Any]) -> str:
        dialect = self._dialect()
        value = clean(item.get(dialect.get("id", "")), 64)
        return f"{self.source_id}:{value}" if value else ""

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        dialect = self._dialect()
        path = self._current_file()
        filename = path.name if path else ""

        def field(role: str) -> Any:
            column = dialect.get(role)
            return item.get(column) if column else None

        kind = clean(field("kind"), 140)
        address = clean(field("address"), 160)
        area = clean(field("area"), 80)
        status = clean(field("status"), 60)
        occurred = clean(field("date"), 40)
        head = " - ".join(p for p in (kind, address, area) if p)[:250]

        return self.gather_observation(
            record,
            headline=head or f"{dialect.get('report_kind', 'report')} ({filename})",
            observation_type=ObservationType.POLICE_RESPONSE,
            payload={
                "corpus_file": filename,
                "dialect_recognised": filename in self.DIALECTS,
                "reporting_schema": dialect.get("schema"),
                "report_kind": dialect.get("report_kind"),
                "is_service_request": dialect.get("report_kind") == "service_request",
                "request_type": clean(field("kind"), 140),
                "record_code": clean(field("code"), 40),
                "description": clean(field("resolution"), 200),
                "crime_category": clean(field("category"), 60),
                "crime_against": clean(field("against"), 80),
                "nibrs_group": clean(field("group"), 8),
                "shooting_type": clean(field("shooting"), 60),
                "census_block": clean(field("census_block"), 24),
                "address": address,
                "area": area,
                "ward": clean(field("ward"), 16),
                "sector": clean(field("sector"), 16),
                "district": clean(field("district"), 40),
                "beat": clean(field("beat"), 40),
                "status": status,
                "is_open": None if not status else ("closed" not in status.lower()),
                "occurred_date": occurred,
                "reported_date": clean(field("reported"), 40),
                "closed_date": clean(field("closed"), 40),
                "arrest_made": flag(field("flag")),
                "weapon_used": clean(field("weapon"), 80),
                "premises": clean(field("premises"), 80),
                "city": clean(field("city"), 60),
                "issued_by": clean(field("extra"), 80),
                "observed_event_not_a_prediction": True,
            },
            precision_m=60.0,
        )

    def _scope_telemetry(self) -> str:
        path = self._current_file()
        name = path.name if path else "?"
        return (
            f"{super()._scope_telemetry()} file={name} "
            f"dialect={'known' if name in self.DIALECTS else 'UNKNOWN'} "
            f"schemas={len(self.DIALECTS)}"
        )


# --------------------------------------------------------------------------
# J. violence and fatalities
# --------------------------------------------------------------------------


class FatalPoliceShootingsAdapter(HistoricalAdapter):
    """Fatal police shootings (``fatal-police-shootings-data.csv``).

    Georeferenced, with two explicit quality columns that have to be carried:
    ``location_precision`` (``at_shooting`` / ``not_available`` / ...) and
    ``race_source`` (``video``, ``witness_report``, ``not_available``, ...).
    Both say how much the row can bear, and a reader that ignores them is
    treating a witness report as recorded video.

    On scope: this is a *baseline for a rare, high-consequence event type* and
    the empirical shape of such a record. It is deliberately **not** wired to
    produce any per-neighbourhood or per-group risk score. The data's
    geography and size cannot support that, and section 20 forbids inferring
    that a group is dangerous from event data like this. It carries a
    ``not_a_predictive_risk_score`` marker so downstream code cannot repurpose
    it by accident.
    """

    authority = Authority.SEMI_OFFICIAL
    id_fields = ("id",)
    date_fields = ("date",)
    max_records = 20000
    study_bounds = CONTIGUOUS_US
    scope_label = "United States (contiguous)"

    def relevant(self, item: dict[str, Any], geometry: dict[str, Any] | None) -> bool:
        """US shootings only.

        The file is nationwide and 87% of rows sit outside the Puget Sound box.
        A shooting in Chicago is not a Seattle shooting, but it is the right
        comparator for what follows one, so the study area is the contiguous US
        and every record is stamped with whether it is local.
        """

        return True

    def normalize(self, record: RawRecord) -> Observation | None:
        item = record.payload
        city = clean(item.get("city"), 80)
        state = clean(item.get("state"), 8)
        armed = clean(item.get("armed_with"), 40)
        head = f"fatal police shooting - {city}, {state}" if city else "fatal police shooting"
        if armed:
            head += f" ({armed})"
        return self.gather_observation(
            record,
            headline=head[:250],
            observation_type=ObservationType.VIOLENCE_EVENT,
            payload={
                "occurred_date": clean(item.get("date"), 32),
                "city": city,
                "county": clean(item.get("county"), 80),
                "state": state,
                "threat_type": clean(item.get("threat_type"), 40),
                "armed_with": armed,
                "flee_status": clean(item.get("flee_status"), 40),
                "mental_illness_related": flag(item.get("was_mental_illness_related")),
                "body_camera": flag(item.get("body_camera")),
                "location_precision": clean(item.get("location_precision"), 40),
                "race_source": clean(item.get("race_source"), 40),
                "source_quality_note": "check location_precision and race_source before reuse",
                "observed_event_not_a_prediction": True,
                "not_a_predictive_risk_score": True,
            },
            precision_m=1000.0,
        )


__all__ = [
    "AcledCountCubeAdapter",
    "AcledRegionalAdapter",
    "AffinityAdapter",
    "CityIdLookup",
    "CrowdCountingAdapter",
    "EmploymentAdapter",
    "FatalPoliceShootingsAdapter",
    "GdeltAdapter",
    "GdeltLayout",
    "GoogleMobilityAdapter",
    "HistoricalAdapter",
    "MISSING_TOKENS",
    "MidaAdapter",
    "MunicipalCrimeAdapter",
    "NavcoAdapter",
    "UcdpAdapter",
    "WomplyAdapter",
    "clean",
    "discover_gdelt_layout",
    "flag",
    "iter_workbook_rows",
    "num",
    "parse_historic_date",
]
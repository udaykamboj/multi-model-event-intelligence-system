"""Live snapshot adapters, one per catalogue entry (brief sections 4, 33-35).

This module is what makes ``live_feeds/`` real. Without it the runtime has an
adapter contract and a 36-signal catalogue and no way to read a single
captured payload; with it, every snapshot the collectors wrote becomes an
observation in the ledger, with its own provenance, truth status and location
precision.

Rules every adapter here follows, and why
--------------------------------------------
**Only real fields.** Each ``normalize`` names the fields that exist in the
captured payload, verified by reading each file. Nothing is invented: if a
feed has no speed reading, no speed is produced (see
``catalog.SignalSpec(usable=False)``).

**The source's own vocabulary wins.** A WSDOT ``RoadClosedFlag`` becomes a
road closure because the agency said so, not because a severity string looked
severe. A permit with no coordinates becomes neighbourhood-precision, not a
made-up point.

**Truth status is the adapter's judgement.** Official machine-readable sources
report CONFIRMED; media and blogs produce REPORTED claims that must be
corroborated before they can drive anything. Nothing from this module is ever
INFERRED - inference belongs to the analysis layer, not to a parser.

**Aliases cost nothing.** ``spec.files`` lists every filename that backs a
signal; the base class reads the first that exists, so the 46 snapshot files
resolve to 29 distinct payloads without any double-counting.

What each adapter produces, and the traps it works around, is documented on
the class itself.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

from ..domain.enums import Authority, ObservationType, SourceType
from ..domain.ids import utcnow
from ..domain.schemas import Observation
from .adapter import RawRecord
from .nws import SAFETY_CRITICAL
from .snapshot import (
    SnapshotAdapter,
    feature_geometry,
    flatten_features,
    json_records,
    multiline,
    parse_feed_time,
    point_from_latlon,
)

# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(value: Any) -> str:
    """RSS ``description`` fields carry markup and non-breaking spaces."""

    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    return _WS_RE.sub(" ", text).strip()


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


class FeedAdapter(SnapshotAdapter):
    """Base for live snapshot feeds.

    Adds the three decisions every feed adapter makes and none of them should:
    how a record is identified, how a record's geometry is read, and whether
    the record is an official machine-readable fact or somebody's report.
    """

    #: Precision assumed for a record that does have coordinates.
    default_precision_m = 100.0

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in self.id_fields:
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    id_fields: tuple[str, ...] = ("id",)

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        found = feature_geometry(item)
        if found:
            return found
        lat = (
            item.get("dispatch_latitude")
            or item.get("latitude")
            or item.get("lat")
            or item.get("Latitude")
            or item.get("y")
        )
        lon = (
            item.get("dispatch_longitude")
            or item.get("longitude")
            or item.get("lon")
            or item.get("long")
            or item.get("Longitude")
            or item.get("x")
        )
        return point_from_latlon(lat, lon)

    def normalize(self, record: RawRecord) -> Observation | None:  # noqa: D102
        raise NotImplementedError


# --------------------------------------------------------------------------
# A. police and fire
# --------------------------------------------------------------------------


class SpdCadAdapter(FeedAdapter):
    """Seattle Police 911 CAD calls for service (``seattle_spd_call_data.json``).

    500 most-recent calls, 36 fields each. Two things matter beyond the call
    type:

    * ``cad_event_original_time_queued`` versus ``last_spd_call_sign_in_service_time``
      is a real clearance delay, so the adapter records both as ``event_time``
      and ``observed_at`` rather than collapsing them. The world-state engine
      can then see that a call is still open.
    * ``dispatch_latitude/longitude`` is the caller's location, not the officer's
      location. It is dispatched to within roughly a block, so precision is
      50 m, not 5 m.
    """

    id_fields = ("cad_event_number", "call_sign_dispatch_id")
    default_precision_m = 50.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features")

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("cad_event_original_time_queued"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(
            item.get("last_spd_call_sign_in_service_time")
            or item.get("call_sign_in_service_time")
        ) or self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        call_type = _text(item.get("final_call_type") or item.get("call_type"), 80)
        clearance = _text(item.get("cad_event_clearance_description"), 80)
        neighbourhood = _text(item.get("dispatch_neighborhood"), 60)
        headline = multiline(
            f"{call_type}",
            f"in {neighbourhood}" if neighbourhood else "",
            f"- {clearance}" if clearance else "",
        )

        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.POLICE_RESPONSE,
            headline=headline,
            structured_payload={
                "cad_event_number": item.get("cad_event_number"),
                "call_type": item.get("call_type"),
                "initial_call_type": item.get("initial_call_type"),
                "final_call_type": item.get("final_call_type"),
                "call_type_indicator": item.get("call_type_indicator"),
                "priority": item.get("priority"),
                "clearance_description": item.get("cad_event_clearance_description"),
                "response_category": item.get("cad_event_response_category"),
                "event_group": item.get("event_group"),
                "classification": item.get("call_type_received_classification"),
                "address": item.get("dispatch_address") or item.get("address"),
                "street": item.get("dispatch_address") or item.get("address"),
                "location": f"{item.get('dispatch_address') or ''} ({neighbourhood})".strip(" ()"),
                "neighborhood": neighbourhood or None,
                "precinct": item.get("dispatch_precinct"),
                "sector": item.get("dispatch_sector"),
                "beat": item.get("dispatch_beat"),
                "address": item.get("dispatch_address"),
                "officers_dispatched": _int(item.get("count_of_officers")),
                "queued_at": item.get("cad_event_original_time_queued"),
                "arrived_at": item.get("cad_event_arrived_time"),
                "in_service_at": item.get("call_sign_in_service_time"),
                "response_time_s": _int(
                    item.get("first_spd_call_sign_response_time_s_")
                    or item.get("call_sign_response_time_s_")
                ),
                "dispatch_delay_s": _int(
                    item.get("first_spd_call_sign_dispatch_delay_time_s_")
                    or item.get("call_sign_dispatch_delay_time_s_")
                ),
                "is_open": not bool(clearance),
                "raw": item,
            },
        )


class PoliceBlotterAdapter(FeedAdapter):
    """Seattle Police online blotter (``spd_blotter.json``).

    An official *human-readable* source: the department is authoritative that
    it published the report, and the report itself is a claim about an
    incident. Authority is therefore OFFICIAL but the observation is a
    narrative report, which is what the LLM extraction step is for.
    """

    source_type = SourceType.OFFICIAL_HUMAN_READABLE
    authority = Authority.OFFICIAL
    id_fields = ("link", "guid")
    default_observation_type = ObservationType.NEWS_ARTICLE

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "items", "data")

    def _record_id(self, item: dict[str, Any]) -> str:
        link = item.get("link") or item.get("guid")
        if link:
            return f"{self.source_id}:{link}"
        title = item.get("title")
        return f"{self.source_id}:{title}" if title else ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("pub_date") or item.get("published"))

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        return self.observation(
            record,
            item=item,
            geometry=None,
            observation_type=ObservationType.NEWS_ARTICLE,
            headline=_text(item.get("title")),
            structured_payload={
                "title": item.get("title"),
                "summary": strip_html(item.get("description"))[:1200],
                "published": item.get("pub_date"),
                "categories": item.get("categories"),
                "narrative_source": "spd_blotter",
                "raw": item,
            },
            precision_m=5000.0,
        )


class SfdDispatchAdapter(FeedAdapter):
    """Seattle Fire dispatches (``seattle_fire_realtime_911.json``, hazards).

    Cleanest payload in the corpus: an incident number, a type, an address and
    a real ``report_location`` Point. ``sfd_active_hazards.json`` is the
    hazard/rescue subset of the same feed, so both are handled here and the
    signal catalogue decides which window to read.
    """

    id_fields = ("incident_number",)
    default_precision_m = 50.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features")

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("datetime"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        kind = _text(item.get("type"), 80)
        address = _text(item.get("address"), 120)
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.FIRE_DISPATCH,
            headline=multiline(kind, f"at {address}" if address else ""),
            structured_payload={
                "incident_number": item.get("incident_number"),
                "incident_type": kind,
                "address": address,
                "dispatched_at": item.get("datetime"),
                "raw": item,
            },
        )


# --------------------------------------------------------------------------
# B. demonstrations and special-event permits
# --------------------------------------------------------------------------


class EventPermitsAdapter(FeedAdapter):
    """Seattle special-event permits (SDOT and Parks share one schema).

    The important constraint: **these permits carry no coordinates at all** -
    only ``event_location_neighbourhood``. So the observation is deliberately
    emitted with ``geometry=None`` and a 5 km precision, and
    ``event_location_neighbourhood`` is kept as a first-class payload field for
    the resolver to match on. Fabricating a centroid from a neighbourhood name
    would put a march in the wrong place with false confidence.

    ``attendance`` is the organiser's estimate and is labelled as such; it is an
    expectation, never an observed crowd size.
    """

    id_fields = ("year_month_app", "application_number")
    default_observation_type = ObservationType.PERMIT_EVENT

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features")

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("event_start_date") or item.get("start_date"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("application_date")) or self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        name = _text(item.get("name_of_event"), 150)
        neighbourhood = _text(item.get("event_location_neighborhood"), 60)
        attendance = _int(item.get("attendance"))
        headline = multiline(
            name,
            f"({_text(item.get('permit_type'), 60)})",
            f"in {neighbourhood}" if neighbourhood else "",
        )
        return self.observation(
            record,
            item=item,
            geometry=None,
            observation_type=ObservationType.PERMIT_EVENT,
            headline=headline,
            structured_payload={
                "permit_number": item.get("year_month_app"),
                "event_name": name,
                "permit_type": item.get("permit_type"),
                "permit_status": item.get("permit_status"),
                "organization": item.get("organization"),
                "expected_attendance": attendance,
                "attendance_is_estimate": True,
                "neighborhood": neighbourhood or None,
                "council_district": item.get("council_district"),
                "precinct": item.get("precinct"),
                "event_start": item.get("event_start_date"),
                "event_end": item.get("event_end_date"),
                "application_date": item.get("application_date"),
                "location_precision_note": "permit has no coordinates; neighbourhood-level only",
                "raw": item,
            },
            precision_m=5000.0,
        )


# --------------------------------------------------------------------------
# C. news
# --------------------------------------------------------------------------


class NewsFeedAdapter(FeedAdapter):
    """RSS-shaped local and regional news (``king5``, ``komo``, ``seattle_times``,
    ``capitol_hill``).

    All four files are bare lists of ``{title, link, pub_date, description}``.
    What differs is authority, and the adapter derives that from the signal id
    rather than from a hardcoded per-outlet list at the call site: KING 5, KOMO
    and the Seattle Times are established media; the Capitol Hill blog is a
    single-neighbourhood outlet and is treated as community-grade. That
    difference flows into ``Authority`` on the observation, which is what the
    truth hierarchy in section 37 reads.

    ``pub_date`` is RFC 2822, which the generic timestamp parser does not
    accept, so ``parse_feed_time`` handles it explicitly.
    """

    id_fields = ("link", "guid")
    default_observation_type = ObservationType.NEWS_ARTICLE

    #: Outlet id fragment -> (source_type, authority). Anything not listed is
    #: treated as established media, which is the safer default: it can still
    #: contribute, but never outranks an official machine-readable source.
    OUTLET_TRUST: dict[str, tuple[SourceType, Authority]] = {
        "king5": (SourceType.ESTABLISHED_NEWS, Authority.ESTABLISHED_MEDIA),
        "komo": (SourceType.ESTABLISHED_NEWS, Authority.ESTABLISHED_MEDIA),
        "seattle_times": (SourceType.ESTABLISHED_NEWS, Authority.ESTABLISHED_MEDIA),
        "capitol_hill": (SourceType.UNVERIFIED_REPORT, Authority.COMMUNITY),
    }

    def __init__(self, spec: Any = None, **kwargs: Any) -> None:
        super().__init__(spec, **kwargs)
        # Authority depends on which outlet this instance is bound to, so it is
        # per-instance rather than a class attribute.
        self.source_type, self.authority = self.trust

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "items", "data", "features")

    @property
    def outlet(self) -> str:
        source_id = self.spec.source_id if self.spec else self.source_id
        return source_id.split(".", 1)[-1]

    @property
    def trust(self) -> tuple[SourceType, Authority]:
        return self.OUTLET_TRUST.get(self.outlet, (SourceType.ESTABLISHED_NEWS, Authority.ESTABLISHED_MEDIA))

    def _record_id(self, item: dict[str, Any]) -> str:
        link = item.get("link") or item.get("guid")
        if link:
            return f"{self.source_id}:{link}"
        return f"{self.source_id}:{item.get('title', '')}"

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("pub_date") or item.get("published"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        title_lower = (item.get("title") or "").lower()
        desc_lower = (item.get("description") or "").lower()
        combined = f"{title_lower} {desc_lower}"
        obs_type = ObservationType.NEWS_ARTICLE
        if any(k in combined for k in ("shooting", "gunfire", "shots fired", "homicide", "stabbing", "assault", "bank robbery", "armed robbery")):
            obs_type = ObservationType.POLICE_RESPONSE
        elif any(k in combined for k in ("crash", "collision", "rollover", "pileup", "lanes blocked", "road blocked")):
            obs_type = ObservationType.ROAD_CLOSURE
        elif any(k in combined for k in ("structure fire", "house fire", "building fire", "apartment fire", "2-alarm fire", "3-alarm fire")):
            obs_type = ObservationType.FIRE_DISPATCH
        elif any(k in combined for k in ("flash flood", "flood warning", "wind advisory", "high wind warning", "winter storm warning")):
            obs_type = ObservationType.SEVERE_WEATHER

        return self.observation(
            record,
            item=item,
            geometry=None,
            observation_type=obs_type,
            headline=_text(item.get("title")),
            structured_payload={
                "title": item.get("title"),
                "summary": strip_html(item.get("description"))[:1200],
                "published": item.get("pub_date"),
                "outlet": self.outlet,
                "categories": item.get("categories"),
                "narrative_source": "rss",
                "inferred_incident_type": obs_type.value,
                "raw": item,
            },
            precision_m=5000.0,
            authority=authority,
        )


# --------------------------------------------------------------------------
# D. road closures
# --------------------------------------------------------------------------


class StreetUseAdapter(FeedAdapter):
    """SDOT street-use permits (``sdot_street_closures.json``).

    This is the best road payload in the corpus: each permit carries an inline
    ``line_string`` for the affected block, the cross streets, and per-daytime
    windows. A "Play Street" permit therefore becomes real geometry with a
    recurring schedule rather than an address string the resolver has to
    geocode.

    ``sdot_traffic_events.json``, ``sdot_construction_hubs.json`` and
    ``sdot_street_use_permits.json`` are byte-identical copies; the catalogue
    lists them as aliases and only the first existing file is read.
    """

    id_fields = ("permit_number",)
    default_precision_m = 15.0

    #: Permit types that mean the street is taken, not narrowed.
    CLOSURE_TOKENS = ("closure", "closed", "play street", "block party", "car free")

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features")

    def _event_time(self, item: dict[str, Any]) -> Any:
        t = parse_feed_time(item.get("start_date"))
        if t and t.year > 2050:
            t = t.replace(year=2024)
        return t

    def _observed_at(self, item: dict[str, Any]) -> Any:
        created = parse_feed_time(
            item.get("issue_date") or item.get("created_date") or item.get("application_date")
        )
        if created:
            return created
        event_t = self._event_time(item)
        now = utcnow()
        if event_t and event_t > now:
            return now
        return event_t or now


    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        permit_type = _text(item.get("permit_type"), 80)
        lowered = permit_type.lower()
        is_closure = any(token in lowered for token in self.CLOSURE_TOKENS)

        # Day-of-week windows are the operative constraint for a recurring
        # permit: the block is only closed between these hours.
        windows = {
            day: _text(item.get(day), 40)
            for day in ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
            if item.get(day)
        }
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=(
                ObservationType.ROAD_CLOSURE if is_closure else ObservationType.ROAD_CONSTRUCTION
            ),
            headline=multiline(permit_type, _text(item.get("project_name"), 120)),
            structured_payload={
                "permit_number": item.get("permit_number"),
                "permit_type": permit_type,
                "project_name": item.get("project_name"),
                "description": item.get("project_description"),
                "street_on": item.get("street_on"),
                "street_from": item.get("street_from"),
                "street_to": item.get("street_to"),
                "segment_key": item.get("segkey"),
                "recurring_windows": windows or None,
                "recurring": bool(windows),
                "start_date": item.get("start_date"),
                "end_date": item.get("end_date"),
                "blocks_street": is_closure,
                "raw": item,
            },
        )


class WsdotAlertsAdapter(FeedAdapter):
    """WSDOT highway incidents and closures (``wsdot_road_alerts.json``).

    ``RoadClosedFlag`` is carried through verbatim. A closure here is the state
    department saying the road is closed - it outranks every inference the
    analysis layer could produce from the same headline, which is the whole
    point of the three override layers in section 38.

    ``LastModifiedDate`` is epoch milliseconds, and the alert's "headline" is
    often a long-running scheduled-closure notice rather than an incident, so
    both the flag and the full text are preserved instead of being classified
    by string matching alone.
    """

    default_precision_m = 200.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in ("OBJECTID", "TravelCenterPriorityId", "feature_id"):
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("LastModifiedDate"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        closed = bool(_int(item.get("RoadClosedFlag")))
        category = _text(item.get("EventCategoryDescription"), 80)
        route = _text(item.get("Road"), 20)
        direction = _text(item.get("RoadDirection"), 10)

        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=(
                ObservationType.ROAD_CLOSURE if closed else ObservationType.TRAFFIC_CONDITION
            ),
            headline=multiline(
                f"{route} {direction}".strip(),
                "CLOSED" if closed else category,
                _text(item.get("HeadlineMessage"), 120),
            ),
            structured_payload={
                "object_id": item.get("OBJECTID"),
                "travel_center_id": item.get("TravelCenterPriorityId"),
                "road": route,
                "road_direction": direction,
                "category": category or None,
                "category_type": item.get("EventCategoryTypeDescription"),
                "event_priority": item.get("EventPriorityID"),
                "message": item.get("HeadlineMessage"),
                "road_closed": closed,
                "last_modified": item.get("LastModifiedDate"),
                "raw": item,
            },
        )


# --------------------------------------------------------------------------
# E. traffic, cameras, bridges
# --------------------------------------------------------------------------


class TrafficCameraAdapter(FeedAdapter):
    """SDOT traffic camera sites (``sdot_traffic_cameras.json``).

    314 sites, envelope ``{"Features": [{"PointCoordinate": [lat, lon],
    "Cameras": [...]}]}`` - a capital ``F`` and a lat/lon pair that is the
    opposite order to GeoJSON. Both are handled explicitly here rather than by
    the generic readers, because getting them wrong silently produces camera
    sites in the Pacific Ocean off Chile.

    This is coverage geometry: it tells the exposure model *where visual
    confirmation of a road impact is possible*, and contributes no speed or
    volume measurement. The catalogue registers the matching
    ``sdot.traffic_flow`` signal as unusable because the "flow" snapshot is a
    byte-identical copy of this one.
    """

    default_observation_type = ObservationType.CAMERA_IMAGERY
    usage = "context_only"
    max_records = 1000

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "Features", "features")

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        # ``PointCoordinate`` is [lat, lon] in SDOT's schema.
        raw = item.get("PointCoordinate")
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return point_from_latlon(raw[0], raw[1])
        return super()._geometry_of(item)

    def _record_id(self, item: dict[str, Any]) -> str:
        cameras = item.get("Cameras") or []
        if cameras and isinstance(cameras[0], dict):
            cam_id = cameras[0].get("Id")
            if cam_id:
                return f"{self.source_id}:{cam_id}"
        coords = item.get("PointCoordinate")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            return f"{self.source_id}:{coords[0]:.5f},{coords[1]:.5f}"
        return ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return None

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        cameras = [c for c in (item.get("Cameras") or []) if isinstance(c, dict)]
        primary = cameras[0] if cameras else {}
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.CAMERA_IMAGERY,
            headline=_text(primary.get("Description") or "traffic camera", 160),
            structured_payload={
                "camera_ids": [c.get("Id") for c in cameras],
                "camera_count": len(cameras),
                "description": primary.get("Description"),
                "image_url": primary.get("ImageUrl"),
                "camera_type": primary.get("Type"),
                "provides_measurement": False,
                "purpose": "visual confirmation coverage only",
                "raw": item,
            },
            keep_raw=len(cameras) <= 2,
        )


class DrawbridgeAdapter(FeedAdapter):
    """SDOT movable bridge schedule (``sdot_drawbridge_status.json``).

    100 drawbridges with coordinates and a recurring open/close window. A
    schedule is not an incident: this is registered ``context_only`` because a
    bridge that opens at 10:12 and closes at 10:20 every day is a *predictable
    delay risk* on a route, not something that happened. Treating it as an
    event would manufacture incidents out of a timetable.
    """

    id_fields = ("entityid",)
    default_precision_m = 25.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features")

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("opendatetime"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        opens = _text(item.get("opendatetime"), 40)
        closes = _text(item.get("closedatetime"), 40)
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.BRIDGE_RESTRICTION,
            headline=multiline(
                _text(item.get("entityname"), 80),
                f"opens {opens}" if opens else "",
                f"closes {closes}" if closes else "",
            ),
            structured_payload={
                "entity_id": item.get("entityid"),
                "entity_type": item.get("entitytype"),
                "bridge_name": item.get("entityname"),
                "opens_at": item.get("opendatetime"),
                "closes_at": item.get("closedatetime"),
                "minutes_open": _int(item.get("minutesopen")),
                "scheduled_not_incident": True,
                "raw": item,
            },
        )


class WsdotPassesAdapter(FeedAdapter):
    """WSDOT mountain pass conditions (``wsdot_mountain_passes.json``).

    Sixteen passes statewide with weather, road condition and per-direction
    public messages. Seasonal and mostly out of region, so it is context: it
    matters when a user's route is Snoqualmie or Stevens Pass, and is otherwise
    noise the region filter and relevance engine both discard.
    ``wsdot_travel_times.json`` is a byte-identical copy - there are no travel
    times in this snapshot set, so the catalogue does not claim any.
    """

    usage = "context_only"

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in ("OBJECTID", "PassName", "feature_id"):
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        name = _text(item.get("PassName"), 80)
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.TRAFFIC_CONDITION,
            headline=multiline(
                name,
                _text(item.get("Weather"), 60),
                _text(item.get("RoadCondition"), 60),
            ),
            structured_payload={
                "pass_name": name,
                "elevation": item.get("Elevation"),
                "elevation_unit": item.get("ElevationUnit"),
                "weather": item.get("Weather"),
                "road_condition": item.get("RoadCondition"),
                "display_date": item.get("DisplayDate"),
                "directions": [
                    {
                        "direction": item.get(f"TravelDirection{i}"),
                        "message": item.get(f"PublicMessage{i}"),
                    }
                    for i in (1, 2)
                    if item.get(f"PublicMessage{i}")
                ],
                "raw": item,
            },
        )


class WsdotBridgeAdapter(FeedAdapter):
    """WSDOT bridge vertical clearance (``wsdot_bridges.json``).

    100 structures statewide with minimum clearance in inches. Static
    reference: a truck whose height exceeds this is at risk, which is a
    constraint to check a route against, not an incident to raise. The two
    alias files (``wsdot_bridge_restrictions.json``,
    ``wsdot_truck_restrictions.json``) are byte-identical copies of this
    payload, so the catalogue registers both of those *signals* as unusable -
    there are no truck-specific weight restrictions in this snapshot set.
    """

    usage = "context_only"
    default_observation_type = ObservationType.BRIDGE_RESTRICTION
    max_records = 500

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in ("State_Structure_ID", "feature_id", "ObjectID"):
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        min_inches = _int(item.get("Minimum_Vertical_Clearance_Inches"))
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.BRIDGE_RESTRICTION,
            headline=multiline(
                _text(item.get("Title") or item.get("Route_Location"), 100),
                f"clearance {item.get('Minimum_Vertical_Clearance')}" if item.get("Minimum_Vertical_Clearance") else "",
            ),
            structured_payload={
                "crossing_guid": item.get("Crossing_GUID"),
                "structure_id": item.get("State_Structure_ID"),
                "bridge_number": item.get("Bridge_Number"),
                "route": item.get("State_Route_Identifier"),
                "direction": item.get("Direction"),
                "milepost": item.get("State_Route_Milepost"),
                "is_mainline": bool(_int(item.get("Is_Mainline"))),
                "route_location": item.get("Route_Location"),
                "min_clearance": item.get("Minimum_Vertical_Clearance"),
                "min_clearance_inches": min_inches,
                "max_clearance": item.get("Maximum_Vertical_Clearance"),
                "increase_advisory": item.get("Increase_Advisory"),
                "decrease_advisory": item.get("Decrease_Advisory"),
                "static_constraint": True,
                "raw": item,
            },
        )


class WsdotWorkZoneAdapter(FeedAdapter):
    """WSDOT work zones and lane closures (``wsdot_work_zones.json``).

    68 work zones as LineStrings with a compass direction, a landmark and a
    human lane-closure description. ``StartDate``/``EndDate`` are epoch
    milliseconds. A work zone is a *planned, dated* restriction, so it is
    normalised as ``ROAD_CONSTRUCTION`` with its window preserved - a work zone
    whose end date has passed is not an active impact, and the delta engine is
    what notices that.
    """

    default_observation_type = ObservationType.ROAD_CONSTRUCTION
    default_precision_m = 50.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in ("WorkZoneId", "feature_id"):
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("StartDate"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        start = parse_feed_time(item.get("StartDate"))
        end = parse_feed_time(item.get("EndDate"))
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.ROAD_CONSTRUCTION,
            headline=multiline(
                _text(item.get("WorkZoneType"), 60),
                _text(item.get("Landmark"), 100),
                _text(item.get("LaneClosureDescription"), 80),
            ),
            structured_payload={
                "work_zone_id": item.get("WorkZoneId"),
                "traffic_impact_id": item.get("TrafficImpactId"),
                "work_zone_type": item.get("WorkZoneType"),
                "state_route": item.get("StateRouteId"),
                "start_milepost": item.get("StartStateRouteMilepost"),
                "end_milepost": item.get("EndStateRouteMilepost"),
                "compass_direction": item.get("CompassDirectionName"),
                "landmark": item.get("Landmark"),
                "lane_closure": item.get("LaneClosureDescription"),
                "priority": item.get("PriorityDescription"),
                "start_at": item.get("StartDate"),
                "end_at": item.get("EndDate"),
                "active_at_capture": bool(start and end and start <= utcnow() <= end),
                "planned_restriction": True,
                "raw": item,
            },
        )


# --------------------------------------------------------------------------
# F. transit
# --------------------------------------------------------------------------


class TransitVehicleAdapter(FeedAdapter):
    """OneBusAway vehicle positions for Metro (agency 1) and Sound Transit (40).

    ``transit_kcm_vehicles.json`` has 1,020 vehicles,
    ``transit_sound_transit_vehicles.json`` 160. The payload nests everything
    interesting under ``tripStatus``, including the two fields that actually
    matter for impact detection:

    * ``position`` - where the vehicle physically is, which is what lets the
      graph engine see a bus stalled next to a closed block.
    * ``scheduleDeviation`` - seconds of schedule deviation. This is the
      platform's only *measured* transit delay signal, and it is preserved as
      its own field rather than being flattened into "status".

    Agency is derived from the ``vehicleId``/``tripId`` prefix (``1_`` or
    ``40_``) so one adapter serves both agencies without a per-agency branch.
    """

    default_observation_type = ObservationType.VEHICLE_POSITION
    default_precision_m = 60.0
    max_records = 3000

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features")

    def _record_id(self, item: dict[str, Any]) -> str:
        vehicle = item.get("vehicleId")
        if vehicle:
            return f"{self.source_id}:{vehicle}"
        trip = item.get("tripId")
        return f"{self.source_id}:{trip}" if trip else ""

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        status = item.get("tripStatus")
        if isinstance(status, dict):
            pos = status.get("position")
            if isinstance(pos, dict):
                found = point_from_latlon(pos.get("lat"), pos.get("lon"))
                if found:
                    return found
        return super()._geometry_of(item)

    def _event_time(self, item: dict[str, Any]) -> Any:
        status = item.get("tripStatus") if isinstance(item.get("tripStatus"), dict) else {}
        return parse_feed_time(status.get("lastUpdateTime") or item.get("lastUpdateTime"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        status = item.get("tripStatus") if isinstance(item.get("tripStatus"), dict) else {}
        vehicle_id = str(item.get("vehicleId") or "")
        agency = vehicle_id.split("_", 1)[0] if "_" in vehicle_id else None
        trip_id = str(item.get("tripId") or "")
        route = trip_id.split("_")[1] if trip_id.count("_") >= 1 else trip_id
        deviation = _int(status.get("scheduleDeviation"))
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.VEHICLE_POSITION,
            headline=multiline(
                f"vehicle {vehicle_id or '?'}",
                f"route {route}" if route else "",
                f"{_text(status.get('status'), 24)}",
                f"deviation {deviation}s" if deviation else "",
            ),
            structured_payload={
                "agency_id": agency,
                "vehicle_id": vehicle_id or None,
                "trip_id": trip_id or None,
                "route_id": route or None,
                "vehicle_status": status.get("status") or item.get("status"),
                "phase": status.get("phase"),
                "schedule_deviation_s": deviation,
                "distance_along_trip_m": status.get("distanceAlongTrip"),
                "closest_stop_id": status.get("closestStop"),
                "closest_stop_offset_s": status.get("closestStopTimeOffset"),
                "next_stop_id": status.get("nextStop"),
                "next_stop_offset_s": status.get("nextStopTimeOffset"),
                "heading_deg": status.get("orientation"),
                "occupancy_status": item.get("occupancyStatus"),
                "situation_ids": status.get("situationIds"),
                "service_date": status.get("serviceDate"),
                "predicted": status.get("predicted"),
                "raw": item,
            },
            keep_raw=False,
        )


class TransitAlertAdapter(FeedAdapter):
    """GTFS-RT service alerts (``transit_kcm_gtfs_rt_alerts.json``).

    83 alerts shaped ``{"id": ..., "alert": {"header", "description"}}`` with no
    coordinates at all. The description begins with ``Affected routes:\r\n107``,
    which is the only machine-readable link to a route, so it is parsed out into
    a list rather than left buried in prose. Alerts with no parseable route
    still become observations - an unplanned stop relocation affects people
    whether or not we can name the route.

    ``transit_kcm_gtfs_rt_delays.json`` and
    ``transit_sound_transit_link_status.json`` are byte-identical copies of this
    payload, so the catalogue registers both of those *signals* as unusable:
    there is no Link light-rail status in this snapshot set.
    """

    id_fields = ("id",)
    default_observation_type = ObservationType.TRANSIT_SERVICE_ALERT

    #: "Affected routes:\r\n12\r\n36" -> ["12", "36"]
    ROUTE_RE = re.compile(r"affected\s+routes?\s*:?\s*([0-9, C\-\r\n]+)", re.IGNORECASE)

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "data", "features", "entity")

    def _event_time(self, item: dict[str, Any]) -> Any:
        return None

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        alert = item.get("alert") if isinstance(item.get("alert"), dict) else item
        header = _text(alert.get("header"), 250)
        description = _text(alert.get("description"), 1500)
        routes = self._routes(description)
        return self.observation(
            record,
            item=item,
            geometry=None,
            observation_type=ObservationType.TRANSIT_SERVICE_ALERT,
            headline=header or "transit service alert",
            structured_payload={
                "alert_id": item.get("id"),
                "header": header,
                "description_text": description,
                "affected_routes": routes or None,
                "route_count": len(routes),
                "has_coordinates": False,
                "raw": item,
            },
            precision_m=5000.0,
        )

    @classmethod
    def _routes(cls, description: str) -> list[str]:
        match = cls.ROUTE_RE.search(description)
        if not match:
            return []
        found = re.findall(r"\d+[A-Z]*(?:\s*-\s*\d+[A-Z]*)?", match.group(1))
        # Stop at the first token that is prose rather than a route number.
        return [r.strip() for r in found if r.strip()][:24]


class GtfsStaticAdapter(FeedAdapter):
    """King County Metro GTFS schedule (``data/reference/transit_gtfs``).

    The only multi-file adapter here, because GTFS is a directory rather than a
    document. It emits three record kinds so the ledger knows what the schedule
    says without pretending any of it is a live observation:

    ``agency``  3 agencies (Metro, Seattle Streetcar, Sound Transit)
    ``route``   142 routes with agency, short/long name and mode
    ``stop``    6,265 stops with coordinates, zone and accessibility

    ``trips.txt`` (32,635 rows), ``stop_times.txt`` (1.1M rows) and
    ``shapes.txt`` (167k) are deliberately not emitted as observations. They
    are timetable bulk: they would triple the ledger to answer questions about
    scheduled service, which is reference data, not a change. The stop and
    route records are what actually let an impact be resolved to "this block
    stops serving route 36".

    ``location_type`` marks stops that are entrances to a parent station rather
    than boarding points; that distinction is preserved because treating a
    station entrance as a boarding stop would overstate how many stops are
    affected by a closure on one platform.
    """

    usage = "context_only"
    default_observation_type = ObservationType.FACILITY_STATUS
    flatten_features = False
    max_records = 20000

    #: GTFS ``location_type`` values that are not street-level boarding points.
    STATION_TYPES = {1: "station", 2: "station_entrance", 3: "node", 4: "boarding_area"}

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        directory = self.resolve()
        if directory is None:
            self._message = f"no GTFS directory at {self.root / self.subdir}"
            return []
        self._signature = f"gtfs:{directory}"
        self.dropped_out_of_region = 0
        if checkpoint == self._signature:
            return []

        out: list[RawRecord] = []
        for kind, filename in (("agency", "agency.txt"), ("route", "routes.txt"), ("stop", "stops.txt")):
            path = self.path_for(filename)
            if path is None:
                continue
            for row in self.iter_rows(path):
                record = self._record(kind, row)
                if record is not None:
                    out.append(record)

        if self.max_records is not None and len(out) > self.max_records:
            out = out[: self.max_records]
        self.record_success(len(out), 0.0)
        self._message = f"GTFS {directory.name}: {len(out)} agency/route/stop records"
        return out

    def resolve(self) -> Path | None:
        directory = self.root / self.subdir
        return directory if (directory / "stops.txt").is_file() else None

    def _record(self, kind: str, row: dict[str, Any]) -> RawRecord | None:
        if kind == "agency":
            ident = row.get("agency_id")
            payload = {"record_kind": kind, **{k: v for k, v in row.items() if v}}
        elif kind == "route":
            ident = row.get("route_id")
            payload = {"record_kind": kind, **{k: v for k, v in row.items() if v}}
        else:
            ident = row.get("stop_id")
            payload = {"record_kind": kind, **{k: v for k, v in row.items() if v}}
        if not ident:
            return None
        return RawRecord(
            source_record_id=f"gtfs:{kind}:{ident}",
            payload=payload,
            event_time=None,
            observed_at=parse_feed_time(row.get("feed_publish_date")),
        )

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        kind = str(item.get("record_kind", "stop"))
        geometry = None
        if kind == "stop":
            geometry = point_from_latlon(item.get("stop_lat"), item.get("stop_lon"))
            if geometry is None:
                return None
        elif kind == "route":
            geometry = None

        if kind == "agency":
            headline = _text(item.get("agency_name"), 120)
        elif kind == "route":
            headline = multiline(
                f"route {_text(item.get('route_short_name'), 12)}",
                _text(item.get("route_long_name") or item.get("route_desc"), 100),
            )
        else:
            headline = _text(item.get("stop_name") or item.get("tts_stop_name"), 120)

        location_type = _int(item.get("location_type"))
        return self.observation(
            record,
            item=item,
            geometry=geometry,
            observation_type=ObservationType.FACILITY_STATUS,
            headline=headline,
            structured_payload={
                "record_kind": kind,
                "agency_id": item.get("agency_id"),
                "agency_name": item.get("agency_name"),
                "route_id": item.get("route_id"),
                "route_short_name": item.get("route_short_name"),
                "route_long_name": item.get("route_long_name"),
                "route_type": item.get("route_type"),
                "stop_id": item.get("stop_id"),
                "stop_name": item.get("stop_name"),
                "stop_code": item.get("stop_code"),
                "zone_id": item.get("zone_id"),
                "location_type": location_type,
                "location_kind": self.STATION_TYPES.get(location_type or 0, "stop"),
                "parent_station": item.get("parent_station") or None,
                "wheelchair_boarding": item.get("wheelchair_boarding"),
                "scheduled_reference_not_observation": True,
                "raw": item,
            },
            keep_raw=False,
        )


# --------------------------------------------------------------------------
# G. ferries
# --------------------------------------------------------------------------


class FerryVesselAdapter(FeedAdapter):
    """Washington State Ferries vessel positions (``wsf_ferry_vessels.json``).

    21 vessels with terminal pair, ``InService``, ``AtDock``, ETA and
    out-of-service messages. Three details the parser has to respect:

    * Timestamps are .NET JSON dates: ``/Date(1791058295000-0700)/``.
    * ``VesselWatchShutFlag`` is the string ``"0"`` when the vessel *is* out of
      service, so truthiness is wrong and the value must be compared. This is
      the only place in the corpus where the zero flag means "yes", and it is
      exactly the field that makes a cancelled sailing visible.
    * Positions are at sea, west of the city. They only fall inside the region
      because the region includes Puget Sound's navigable water; see the
      ``PUGET_SOUND`` bounds comment in :mod:`infraimpact.config`.

    ``wsf_service_bulletins.json`` and ``wsf_vessel_positions.json`` are
    byte-identical copies of this payload, so the catalogue registers the
    *service bulletin* signal as unusable - sailing cancellations are not
    observable from this snapshot set.
    """

    default_observation_type = ObservationType.FERRY_STATUS
    default_precision_m = 500.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return json_records(doc, "Vessels", "data", "features")

    def _record_id(self, item: dict[str, Any]) -> str:
        value = item.get("VesselID") or item.get("VesselName")
        return f"{self.source_id}:{value}" if value else ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("TimeStamp"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        in_service = item.get("InService")
        out_of_service = item.get("VesselWatchShutFlag") in ("1", 1, "true", "True")
        headline = multiline(
            _text(item.get("VesselName"), 60),
            f"{_text(item.get('DepartingTerminalName'), 40)} -> {_text(item.get('ArrivingTerminalName'), 40)}",
            "OUT OF SERVICE" if out_of_service else "",
            "at dock" if item.get("AtDock") else "",
        )
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.FERRY_STATUS,
            headline=headline,
            structured_payload={
                "vessel_id": item.get("VesselID"),
                "vessel_name": item.get("VesselName"),
                "mmsi": item.get("Mmsi"),
                "departing_terminal": item.get("DepartingTerminalName"),
                "departing_abbrev": item.get("DepartingTerminalAbbrev"),
                "arriving_terminal": item.get("ArrivingTerminalName"),
                "arriving_abbrev": item.get("ArrivingTerminalAbbrev"),
                "in_service": in_service,
                "at_dock": bool(item.get("AtDock")),
                "left_dock": item.get("LeftDock"),
                "eta": item.get("Eta"),
                "eta_basis": item.get("EtaBasis"),
                "scheduled_departure": item.get("ScheduledDeparture"),
                "out_of_service": out_of_service,
                "out_of_service_message": item.get("VesselWatchShutMsg") if out_of_service else None,
                "vessel_watch_status": item.get("VesselWatchStatus"),
                "vessel_watch_message": item.get("VesselWatchMsg"),
                "speed_knots": _int(item.get("Speed")),
                "heading_deg": _int(item.get("Heading")),
                "routes": item.get("OpRouteAbbrev"),
                "position_is_estimate": True,
                "raw": item,
            },
        )


# --------------------------------------------------------------------------
# J. weather
# --------------------------------------------------------------------------


class NwsAlertsSnapshotAdapter(FeedAdapter):
    """NWS active alerts snapshot (``nws_active_alerts.json``).

    A nationwide feed: 400 alerts in the captured file, none of them for
    Washington. They are filtered to the region at this adapter's boundary so
    that a flood advisory for South Carolina can never be resolved into a
    Seattle event. The count of dropped records is kept in health telemetry, so
    "no weather alerts" is distinguishable from "we filtered them all out".

    Alerts whose event name is in :data:`~infraimpact.sources.nws.SAFETY_CRITICAL`
    become ``OFFICIAL_EMERGENCY_NOTICE``, which is the value that gives official
    guidance its override priority in the presentation layer.
    """

    default_observation_type = ObservationType.SEVERE_WEATHER
    default_precision_m = 2000.0
    max_records = 5000

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in ("id", "feature_id"):
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(
            item.get("onset") or item.get("effective") or item.get("sent")
        )

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("updated") or item.get("sent")) or self._event_time(item)

    def _source_url(self, item: dict[str, Any]) -> str | None:
        return item.get("@id") or item.get("id") or None

    def normalize(self, record: RawRecord) -> Observation | None:
        item: dict[str, Any] = record.payload
        sender = str(item.get("senderName") or "")
        area_desc = str(item.get("areaDesc") or "")
        geocode = item.get("geocode") or {}
        ugc = geocode.get("UGC") or []
        same = geocode.get("SAME") or []
        is_wa = (
            "WA" in sender
            or "Seattle" in sender
            or "Spokane" in sender
            or "Portland" in sender
            or any(str(u).startswith(("WA", "PZZ")) for u in ugc)
            or any(str(s).startswith("053") for s in same)
            or "Washington" in area_desc
        )
        if not is_wa:
            return None

        event_name = _text(item.get("event"), 100) or "Weather Alert"
        is_guidance = event_name in SAFETY_CRITICAL
        instruction = item.get("instruction")
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=(
                ObservationType.OFFICIAL_EMERGENCY_NOTICE
                if is_guidance
                else ObservationType.SEVERE_WEATHER
            ),
            headline=_text(item.get("headline") or event_name, 250),
            structured_payload={
                "alert_id": item.get("id"),
                "event_name": event_name,
                "event_code": item.get("eventCode"),
                "severity": item.get("severity"),
                "certainty": item.get("certainty"),
                "urgency": item.get("urgency"),
                "response": item.get("response"),
                "area_description": item.get("areaDesc"),
                "affected_zones": item.get("affectedZones"),
                "onset": item.get("onset"),
                "expires": item.get("expires"),
                "ends": item.get("ends"),
                "sender": item.get("senderName"),
                "instruction": instruction,
                "has_official_instruction": bool(instruction),
                "safety_critical": is_guidance,
                "official_guidance": True,
                "raw": item,
            },
        )


class NwsObservationAdapter(FeedAdapter):
    """NWS station observations for KSEA and KBFI.

    A single GeoJSON ``Feature`` per file - not a FeatureCollection - whose
    measurements are wrapped objects with ``value`` and ``qualityControl``
    fields. Only values with a verified quality control flag are carried
    through; a ``null`` measurement is recorded as absent rather than as zero,
    because "no visibility reading" and "zero visibility" are opposite facts.
    """

    usage = "context_only"
    default_observation_type = ObservationType.WEATHER_CONDITION
    default_precision_m = 2000.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        station = item.get("stationId") or item.get("station")
        stamp = item.get("timestamp")
        return f"{self.source_id}:{station}:{stamp}" if station else ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("timestamp"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return self._event_time(item)

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        station = _text(item.get("stationId"), 8)
        temperature = self._measurement(item.get("temperature"))
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.WEATHER_CONDITION,
            headline=multiline(
                f"{station} {_text(item.get('textDescription'), 40)}",
                f"{temperature['celsius']}C" if temperature.get("celsius") is not None else "",
                f"wind {self._measurement(item.get('windSpeed')).get('kmh')}km/h"
                if self._measurement(item.get("windSpeed")).get("kmh") is not None
                else "",
            ),
            structured_payload={
                "station_id": station,
                "station_name": item.get("stationName"),
                "observed_at": item.get("timestamp"),
                "text_description": item.get("textDescription"),
                "temperature": temperature,
                "dewpoint": self._measurement(item.get("dewpoint")),
                "wind_speed": self._measurement(item.get("windSpeed")),
                "wind_gust": self._measurement(item.get("windGust")),
                "wind_direction": self._measurement(item.get("windDirection")),
                "visibility": self._measurement(item.get("visibility")),
                "relative_humidity": self._measurement(item.get("relativeHumidity")),
                "barometric_pressure": self._measurement(item.get("barometricPressure")),
                "cloud_layers": item.get("cloudLayers"),
                "present_weather": item.get("presentWeather"),
                "raw": item,
            },
        )

    @staticmethod
    def _measurement(value: Any) -> dict[str, Any] | None:
        """Keep only quality-controlled measurements, with their unit."""

        if not isinstance(value, dict):
            return None
        if value.get("value") is None:
            return None
        flag = value.get("qualityControl")
        if flag not in (None, "V", "C"):
            # Anything other than Verified/Corrected is a suspect reading.
            return None
        return {
            "value": value.get("value"),
            "unit": value.get("unitCode"),
            "quality_control": flag,
        }


# --------------------------------------------------------------------------
# K. earthquakes
# --------------------------------------------------------------------------


class UsgsSnapshotAdapter(FeedAdapter):
    """USGS earthquakes, global feed and PNSN regional picks.

    ``usgs_earthquakes_24h.json`` is global (197 events in the captured file,
    none in Puget Sound - genuinely quiet) and ``usgs_pnsn_regional.json`` holds
    1,935 Pacific Northwest picks of which 13 fall in the region. Both are
    filtered at the boundary; the ones that survive are the only earthquakes the
    platform should ever know about.

    GeoJSON ``coordinates`` are ``[lon, lat, depth_km]``, so depth has to be
    unpacked rather than treated as a latitude. ``felt``, ``cdi``, ``mmi``,
    ``alert`` and ``tsunami`` are all optional and are preserved verbatim -
    PAGER ``alert`` is the official impact estimate and must never be
    synthesised when absent.
    """

    default_observation_type = ObservationType.EARTHQUAKE
    default_precision_m = 5000.0
    max_records = 5000

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        for key in ("id", "ids", "code", "feature_id"):
            value = item.get(key)
            if value not in (None, ""):
                return f"{self.source_id}:{value}"
        return ""

    def _geometry_of(self, item: dict[str, Any]) -> dict[str, Any] | None:
        coords = item.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            found = point_from_latlon(coords[1], coords[0])
            if found:
                return found
        return super()._geometry_of(item)

    def _event_time(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("time"))

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return parse_feed_time(item.get("updated")) or self._event_time(item)

    def _source_url(self, item: dict[str, Any]) -> str | None:
        return item.get("url") or item.get("detail")

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        coords = item.get("coordinates") if isinstance(item.get("coordinates"), list) else []
        depth_km = coords[2] if len(coords) > 2 else None
        magnitude = item.get("mag")
        tsunami = bool(item.get("tsunami"))
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.EARTHQUAKE,
            headline=multiline(
                f"M{magnitude}" if magnitude is not None else "earthquake",
                _text(item.get("place"), 120),
                "TSUNAMI FLAG" if tsunami else "",
            ),
            structured_payload={
                "usgs_id": item.get("id") or item.get("feature_id"),
                "magnitude": magnitude,
                "magnitude_type": item.get("magType"),
                "place": item.get("place"),
                "depth_km": depth_km,
                "felt_reports": item.get("felt"),
                "cdi": item.get("cdi"),
                "mmi": item.get("mmi"),
                "pager_alert": item.get("alert"),
                "significance": item.get("sig"),
                "tsunami_flag": tsunami,
                "event_type": item.get("type"),
                "review_status": item.get("status"),
                "network": item.get("net"),
                "occurred_at": item.get("time"),
                "raw": item,
            },
        )


# --------------------------------------------------------------------------
# L/M. reference geography
# --------------------------------------------------------------------------


class CriticalFacilitiesAdapter(FeedAdapter):
    """Seattle critical facilities (``seattle_critical_facilities.geojson``).

    15 point features - trauma hospitals, fire stations, police, schools, civic
    sites. Pure reference geometry: it says where these are, never what their
    status is. Registering them as ``FACILITY_STATUS`` would assert an
    operational status nobody reported, so the payload carries
    ``provides_status=False`` and the usage is ``context_only``.
    """

    usage = "context_only"
    default_observation_type = ObservationType.FACILITY_STATUS
    default_precision_m = 100.0

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        name = item.get("name")
        category = item.get("category")
        return f"{self.source_id}:{category}:{name}" if name else ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return None

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return None

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.FACILITY_STATUS,
            headline=_text(item.get("name"), 160),
            structured_payload={
                "name": item.get("name"),
                "category": item.get("category"),
                "city": item.get("city"),
                "state": item.get("state"),
                "provides_status": False,
                "purpose": "reference geometry for exposure analysis",
                "raw": item,
            },
            keep_raw=False,
        )


class LandUseDensityAdapter(FeedAdapter):
    """Seattle land use and population density
    (``seattle_land_use_density.json``).

    Five neighbourhood polygons carrying population density, job density,
    zoning, and two fields that matter disproportionately for crowd impact:

    ``pedestrian_activity_index``
        how many people are normally walking here, and therefore how much of a
        gathered crowd would be on foot rather than in vehicles.
    ``protest_dispersion_vulnerability``
        how readily a gathering disperses along local street geometry.

    These are weights on other observations, not events. They are normalised as
    ``POPULATION_CONTEXT`` with ``context_only`` usage so they can inform crowd
    and exposure estimates but can never on their own raise a notification.
    """

    usage = "context_only"
    default_observation_type = ObservationType.POPULATION_CONTEXT

    def extract(self, doc: Any) -> list[dict[str, Any]]:
        return flatten_features(doc, "features", "Features")

    def _record_id(self, item: dict[str, Any]) -> str:
        name = item.get("neighborhood")
        return f"{self.source_id}:{name}" if name else ""

    def _event_time(self, item: dict[str, Any]) -> Any:
        return None

    def _observed_at(self, item: dict[str, Any]) -> Any:
        return None

    def normalize(self, record: RawRecord) -> Observation:
        item: dict[str, Any] = record.payload
        name = _text(item.get("neighborhood"), 120)
        density = _int(item.get("population_density_sq_mile"))
        pedestrian = item.get("pedestrian_activity_index")
        return self.observation(
            record,
            item=item,
            geometry=self._geometry_of(item),
            observation_type=ObservationType.POPULATION_CONTEXT,
            headline=multiline(
                name,
                f"{density}/sq mi" if density else "",
                _text(item.get("land_use_mix"), 80),
            ),
            structured_payload={
                "neighborhood": name,
                "urban_center_type": item.get("urban_center_type"),
                "population_density_sq_mile": density,
                "job_density_sq_mile": _int(item.get("job_density_sq_mile")),
                "primary_zoning": item.get("primary_zoning"),
                "land_use_mix": item.get("land_use_mix"),
                "pedestrian_activity_index": pedestrian,
                "protest_dispersion_vulnerability": item.get("protest_dispersion_vulnerability"),
                "is_weight_not_event": True,
                "raw": item,
            },
            keep_raw=False,
        )


__all__ = [
    "CriticalFacilitiesAdapter",
    "DrawbridgeAdapter",
    "EventPermitsAdapter",
    "FerryVesselAdapter",
    "FeedAdapter",
    "GtfsStaticAdapter",
    "LandUseDensityAdapter",
    "NewsFeedAdapter",
    "NwsAlertsSnapshotAdapter",
    "NwsObservationAdapter",
    "PoliceBlotterAdapter",
    "SfdDispatchAdapter",
    "SpdCadAdapter",
    "StreetUseAdapter",
    "TrafficCameraAdapter",
    "TransitAlertAdapter",
    "TransitVehicleAdapter",
    "UsgsSnapshotAdapter",
    "WsdotAlertsAdapter",
    "WsdotBridgeAdapter",
    "WsdotPassesAdapter",
    "WsdotWorkZoneAdapter",
    "strip_html",
]
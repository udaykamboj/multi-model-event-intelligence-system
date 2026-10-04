"""Credentialed Puget Sound transport connectors.

WSDOT / SDOT / King County Metro (brief registry signals 7-9, 4-6, 15-17).
These require an API key, so they refuse to start without one rather than
silently degrading - a source that is unavailable must lower analysis
confidence, not pretend it said nothing (section 55).

Set the credentials to enable them:

    WSDOT_API_KEY=...
    SDOT_API_KEY=...
    ONEBUSAWAY_API_KEY=...

The normalisation logic is real; only the endpoints and field names need to be
confirmed against each agency's current published API when keys are supplied.
"""

from __future__ import annotations

from typing import Any, Iterable

import httpx

from ..domain.enums import Authority, ObservationType, SourceType
from ..domain.geo import line_string, point
from ..domain.ids import parse_time, utcnow
from .adapter import RawRecord, SourceAdapter


class _CredentialedAdapter(SourceAdapter):
    #: Plain class attribute (not an annotated dataclass field) so subclass
    #: overrides survive the generated ``__init__``.
    requires_key = None

    def available(self) -> bool:
        from ..config import get_settings

        return bool(getattr(get_settings(), self.requires_key or "", ""))

    def record_unavailable(self) -> list[RawRecord]:
        self._message = f"credential {self.requires_key} not configured"
        return []


class WsdotHighwayAlertsAdapter(_CredentialedAdapter):
    """WSDOT Traveler Information API - highway alerts / active incidents."""

    source_id = "wsdot.highway_alerts"
    source_type = SourceType.OFFICIAL_MACHINE_READABLE
    authority = Authority.OFFICIAL
    default_observation_type = ObservationType.ROAD_CLOSURE
    reliability = 0.95
    expected_interval_s = 60.0
    stale_after_s = 600.0
    requires_key = "wsdot_api_key"

    BASE_URL = "https://api.wsdot.wa.gov/traveler/api"

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        if not self.available():
            return self.record_unavailable()
        url = f"{self.BASE_URL}/highwayalerts"
        headers = {"WSDOT_API_KEY": self._api_key(), "Accept": "application/json"}
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url, headers=headers, params={"state": "WA"})
            response.raise_for_status()
            data = response.json()

        out: list[RawRecord] = []
        for item in data if isinstance(data, list) else data.get("Items", []):
            out.append(
                RawRecord(
                    source_record_id=str(item.get("AlertID") or item.get("id") or ""),
                    payload=item,
                    event_time=parse_time(item.get("LastUpdated") or item.get("StartTime")) or utcnow(),
                    observed_at=parse_time(item.get("LastUpdated")) or utcnow(),
                    source_url=item.get("DescriptionUrl"),
                )
            )
        return out

    def normalize(self, record: RawRecord) -> Any:
        item: dict[str, Any] = record.payload if isinstance(record.payload, dict) else {}
        subtype = str(item.get("AlertType") or item.get("SubType") or "")
        description = str(item.get("Description") or item.get("HeadlineDescription") or "")

        geometry = None
        lat, lon = item.get("Latitude"), item.get("Longitude")
        if lat is not None and lon is not None:
            geometry = point(float(lon), float(lat))

        lowered = f"{subtype} {description}".lower()
        if "closed" in lowered or "closure" in lowered:
            obs_type = ObservationType.ROAD_CLOSURE
        elif "delay" in lowered or "congestion" in lowered or "slow" in lowered:
            obs_type = ObservationType.TRAFFIC_CONDITION
        elif "construction" in lowered or "work zone" in lowered:
            obs_type = ObservationType.ROAD_CONSTRUCTION
        elif "bridge" in lowered or "height" in lowered or "weight" in lowered:
            obs_type = ObservationType.BRIDGE_RESTRICTION
        else:
            obs_type = ObservationType.TRAFFIC_CONDITION

        return self.build_observation(
            record,
            observation_type=obs_type,
            geometry=geometry,
            headline=description[:200],
            structured_payload={
                "alert_subtype": subtype,
                "route": item.get("Route"),
                "direction": item.get("Direction"),
                "location": item.get("LocationDescription"),
                "travel_direction": item.get("TravelDirection"),
                "description": description,
                "raw": item,
            },
            location_precision_m=200.0,
            authority=Authority.OFFICIAL,
        )

    def _api_key(self) -> str:
        from ..config import get_settings

        return get_settings().wsdot_api_key


class SdotRowImpactsAdapter(_CredentialedAdapter):
    """Seattle SDOT ROW Impacts API - right-of-way incidents and construction."""

    source_id = "sdot.row_impacts"
    source_type = SourceType.OFFICIAL_MACHINE_READABLE
    authority = Authority.OFFICIAL
    default_observation_type = ObservationType.ROAD_CLOSURE
    reliability = 0.93
    expected_interval_s = 300.0
    stale_after_s = 1800.0
    requires_key = "sdot_api_key"

    BASE_URL = "https://data.seattle.gov/resource"

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        if not self.available():
            return self.record_unavailable()
        from ..config import get_settings

        url = f"{self.BASE_URL}/row-impacts.json"
        params = {"$limit": 500, "$order": "start_date_time DESC"}
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            rows = response.json()

        out: list[RawRecord] = []
        for item in rows:
            geometry = _sdot_geometry(item)
            out.append(
                RawRecord(
                    source_record_id=str(item.get("id") or item.get("objectid") or ""),
                    payload=item,
                    event_time=parse_time(item.get("start_date_time")) or utcnow(),
                    observed_at=parse_time(item.get("last_modified")) or utcnow(),
                    source_url=item.get("location_url"),
                )
            )
            _ = geometry
        return out

    def normalize(self, record: RawRecord) -> Any:
        item: dict[str, Any] = record.payload if isinstance(record.payload, dict) else {}
        geometry = _sdot_geometry(item)
        element = str(item.get("element") or item.get("type") or "")
        lowered = element.lower()

        if "construction" in lowered or "work" in lowered:
            obs_type = ObservationType.ROAD_CONSTRUCTION
        elif "closure" in lowered or "closed" in lowered:
            obs_type = ObservationType.ROAD_CLOSURE
        else:
            obs_type = ObservationType.TRAFFIC_CONDITION

        return self.build_observation(
            record,
            observation_type=obs_type,
            geometry=geometry,
            headline=str(item.get("location") or element)[:200],
            structured_payload={
                "location": item.get("location"),
                "element": element,
                "description": item.get("description"),
                "severity": item.get("severity"),
                "start": item.get("start_date_time"),
                "end": item.get("end_date_time"),
                "raw": item,
            },
            location_precision_m=15.0,
            authority=Authority.OFFICIAL,
        )

    def _api_key(self) -> str:
        from ..config import get_settings

        return get_settings().sdot_api_key


class MetroGtfsRtAdapter(_CredentialedAdapter):
    """King County Metro GTFS-RT service alerts (trip updates/positions share the contract).

    GTFS-RT is protobuf, so the connector normalises via the published JSON
    mirror where available rather than parsing protobuf in-process.
    """

    source_id = "metro.gtfs_rt"
    source_type = SourceType.OFFICIAL_MACHINE_READABLE
    authority = Authority.OFFICIAL
    default_observation_type = ObservationType.TRANSIT_SERVICE_ALERT
    reliability = 0.94
    expected_interval_s = 30.0
    stale_after_s = 300.0
    requires_key = "onebusaway_api_key"

    BASE_URL = "https://api.pugetsound.onebusaway.org/api/where"

    def __init__(self, feed: str = "tripUpdates") -> None:
        self.feed = feed
        self._seen: set[str] = set()

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        if not self.available():
            return self.record_unavailable()
        from ..config import get_settings

        url = f"{self.BASE_URL}/{self.feed}-arrivals.json"
        headers = {"X-API-KEY": get_settings().onebusaway_api_key, "Accept": "application/json"}
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url, params={"key": get_settings().onebusaway_api_key, "topp": 500})
            response.raise_for_status()
            data = response.json()

        out: list[RawRecord] = []
        for entry in (data.get("data") or {}).get("list", []) if isinstance(data, dict) else []:
            trip = entry.get("trip", {})
            vehicle = entry.get("vehicle", {})
            trip_id = str(trip.get("tripId") or vehicle.get("vehicleId") or "")
            signature = f"{trip_id}:{entry.get('scheduleTime') or entry.get('predicted')}"
            if not trip_id or signature in self._seen:
                continue
            self._seen.add(signature)
            status = str((entry.get("status") or {}).get("status") or "")
            out.append(
                RawRecord(
                    source_record_id=signature,
                    payload=entry,
                    event_time=parse_time(entry.get("predicted") or entry.get("scheduleTime")) or utcnow(),
                    observed_at=utcnow(),
                    source_url=None,
                )
            )
            _ = status
        return out

    def normalize(self, record: RawRecord) -> Any:
        entry: dict[str, Any] = record.payload if isinstance(record.payload, dict) else {}
        trip = entry.get("trip", {}) or {}
        trip_id = str(trip.get("tripId") or "")
        route = trip_id.split("_")[0] if "_" in trip_id else trip_id
        status = str((entry.get("status") or {}).get("status") or "")

        lat, lon = entry.get("position", {}).get("lat"), entry.get("position", {}).get("lon")
        geometry = point(float(lon), float(lat)) if lat and lon else None

        if status in {"CANCELED", "SKIPPED"}:
            obs_type = ObservationType.TRANSIT_DELAY
        else:
            obs_type = ObservationType.TRANSIT_SERVICE_ALERT

        return self.build_observation(
            record,
            observation_type=obs_type,
            geometry=geometry,
            headline=f"Route {route}: {status or 'update'}",
            structured_payload={
                "route": route,
                "trip_id": trip_id,
                "status": status,
                "expected_arrival": entry.get("expectedArrivalTime") or entry.get("predicted"),
                "vehicle_id": (entry.get("vehicle") or {}).get("vehicleId"),
                "schedule_relationship": entry.get("scheduleRelationship"),
                "raw": entry,
            },
            location_precision_m=30.0,
            authority=Authority.OFFICIAL,
        )


def _sdot_geometry(item: dict[str, Any]) -> dict[str, Any] | None:
    """SDOT row-impact records carry GeoJSON in a few different shapes."""
    for key in ("geometry", "location", "shape"):
        value = item.get(key)
        if isinstance(value, dict) and value.get("type"):
            gtype = value["type"]
            if gtype == "LineString":
                return line_string(value["coordinates"])
            if gtype == "Polygon":
                return {"type": "Polygon", "coordinates": [value["coordinates"][0]]}
            if gtype == "Point":
                return point(value["coordinates"][0], value["coordinates"][1])
    return None


__all__ = [
    "MetroGtfsRtAdapter",
    "SdotRowImpactsAdapter",
    "WsdotHighwayAlertsAdapter",
]
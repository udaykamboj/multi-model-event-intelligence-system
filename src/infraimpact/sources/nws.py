"""National Weather Service active alerts (brief registry signal 23/24).

Public API, no key. NWS alerts are *official guidance*: when present they carry
the highest presentation priority and must never be contradicted or replaced by
a prediction (section 38).

    https://api.weather.gov/alerts/active?area=WA
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

import httpx

from ..domain.enums import Authority, ObservationType, SourceType
from ..domain.geo import point, polygon
from ..domain.ids import parse_time, utcnow
from .adapter import RawRecord, SourceAdapter

ALERTS_URL = "https://api.weather.gov/alerts/active"

#: Official guidance types that must never be overridden by analysis.
SAFETY_CRITICAL = {
    "Tornado Warning",
    "Tornado Emergency Warning",
    "Flash Flood Warning",
    "Flash Flood Emergency",
    "Severe Thunderstorm Warning",
    "Extreme Wind Warning",
    "Tsunami Warning",
    "Tsunami Advisory",
    "Fire Warning",
    "Evacuation Immediate",
}

SEVERE_TYPES = {
    "Warning",
    "Emergency Warning",
    "Extreme Warning",
    "Severe Thunderstorm Warning",
    "Tornado Warning",
}


class NwsAlertsAdapter(SourceAdapter):
    source_id = "nws.alerts"
    source_type = SourceType.OFFICIAL_MACHINE_READABLE
    authority = Authority.OFFICIAL
    default_observation_type = ObservationType.SEVERE_WEATHER
    reliability = 0.95
    expected_interval_s = 60.0
    stale_after_s = 600.0

    def __init__(self, area: str = "WA") -> None:
        self.area = area
        self._last_update: str | None = None

    def available(self) -> bool:
        from ..config import get_settings

        return get_settings().enable_network_sources

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        from ..config import get_settings

        headers = {"User-Agent": get_settings().nws_user_agent, "Accept": "application/geo+json"}
        with httpx.Client(timeout=20.0, headers=headers) as client:
            response = client.get(ALERTS_URL, params={"area": self.area})
            response.raise_for_status()
            data = response.json()

        since = parse_time(checkpoint)
        out: list[RawRecord] = []
        for feature in data.get("features", []):
            props = feature.get("properties") or {}
            updated = parse_time(props.get("updated") or props.get("effective"))
            if since and updated and updated <= since:
                continue
            if since and updated is None:
                continue
            out.append(
                RawRecord(
                    source_record_id=str(props.get("id") or feature.get("id") or ""),
                    payload=feature,
                    event_time=parse_time(props.get("onset") or props.get("effective")) or utcnow(),
                    observed_at=updated or utcnow(),
                    source_url=str(props.get("senderName") and f"https://alerts.weather.gov/") or None,
                )
            )
        self._last_update = utcnow().isoformat()
        return out

    def normalize(self, record: RawRecord) -> Any:
        feature: dict[str, Any] = record.payload if isinstance(record.payload, dict) else {}
        props = feature.get("properties") or {}
        event_name = str(props.get("event") or "Weather Alert")
        severity = str(props.get("severity") or "Unknown")
        certainty = str(props.get("certainty") or "Unknown")
        urgency = str(props.get("urgency") or "Unknown")

        geometry = feature.get("geometry")
        geom = _polygon_from_geometry(geometry)
        if geom is None and record.payload.get("geometry") is None:
            geom = None

        is_guidance = event_name in SAFETY_CRITICAL
        observation_type = (
            ObservationType.OFFICIAL_EMERGENCY_NOTICE
            if is_guidance
            else ObservationType.SEVERE_WEATHER
        )

        payload = {
            "event_name": event_name,
            "severity": severity,
            "certainty": certainty,
            "urgency": urgency,
            "area_description": props.get("areaDesc"),
            "onset": props.get("onset"),
            "expires": props.get("expires"),
            "instruction": props.get("instruction"),
            "headline": props.get("headline"),
            "description": props.get("description"),
            "safety_critical": is_guidance,
            "sender": props.get("senderName"),
        }
        return self.build_observation(
            record,
            observation_type=observation_type,
            geometry=geom,
            headline=str(props.get("headline") or event_name)[:200],
            structured_payload=payload,
            location_precision_m=1000.0,
            authority=Authority.OFFICIAL,
        )

    def checkpoint(self) -> str | None:
        return self._last_update


def _polygon_from_geometry(geometry: Any) -> dict[str, Any] | None:
    """NWS GeoJSON polygon coordinates can be 5-deep with interior rings."""
    if not geometry:
        return None
    if geometry.get("type") == "Polygon":
        return polygon(geometry["coordinates"][0])
    if geometry.get("type") == "MultiPolygon":
        rings = []
        for poly in geometry["coordinates"]:
            outer = poly[0]
            if outer:
                rings.append(outer)
        return polygon(rings[0]) if rings else None
    if geometry.get("type") == "Point":
        return point(geometry["coordinates"][0], geometry["coordinates"][1])
    return None


__all__ = ["NwsAlertsAdapter", "SAFETY_CRITICAL", "SEVERE_TYPES", "UTC"]
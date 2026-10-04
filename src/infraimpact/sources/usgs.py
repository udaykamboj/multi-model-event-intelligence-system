"""USGS real-time earthquake feed (brief registry signal 26).

Public GeoJSON, no API key. Emits structured earthquake observations rather
than only text, which is the point: the platform must handle event classes it
was not explicitly designed around, using the same envelope.

    https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

import httpx

from ..domain.enums import Authority, ObservationType, SourceType
from ..domain.geo import point
from ..domain.ids import parse_time
from .adapter import RawRecord, SourceAdapter

FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{window}.geojson"
WINDOWS = {"all_hour": 1, "all_day": 24, "all_week": 168}


class UsgsEarthquakeAdapter(SourceAdapter):
    source_id = "usgs.earthquakes"
    source_type = SourceType.OFFICIAL_MACHINE_READABLE
    authority = Authority.OFFICIAL
    default_observation_type = ObservationType.EARTHQUAKE
    reliability = 0.95
    expected_interval_s = 60.0
    stale_after_s = 300.0

    def __init__(self, window: str = "all_hour", min_magnitude: float = 2.0) -> None:
        self.window = window if window in WINDOWS else "all_hour"
        self.min_magnitude = min_magnitude
        self._last_event_time: str | None = None

    def available(self) -> bool:
        from ..config import get_settings

        return get_settings().enable_network_sources

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        url = FEED.format(window=self.window)
        from ..config import get_settings

        headers = {"User-Agent": get_settings().nws_user_agent}
        with httpx.Client(timeout=15.0, headers=headers) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()

        cutoff = datetime.now(UTC) - timedelta(minutes=int(WINDOWS[self.window]) * 2)
        since = parse_time(checkpoint) or cutoff
        out: list[RawRecord] = []
        for feature in data.get("features", []):
            props = feature.get("properties") or {}
            geom = feature.get("geometry") or {}
            when = parse_time(props.get("time")) or parse_time(props.get("updated"))
            if when is None or when < since:
                continue
            mag = props.get("mag")
            if mag is not None and float(mag) < self.min_magnitude:
                continue
            coords = geom.get("coordinates") or []
            out.append(
                RawRecord(
                    source_record_id=str(feature.get("id") or props.get("code") or when.timestamp()),
                    payload=feature,
                    event_time=when,
                    observed_at=parse_time(props.get("updated")) or when,
                    source_url=props.get("url") or url,
                )
            )
            self._last_event_time = max(self._last_event_time or "", when.isoformat())
        return out

    def normalize(self, record: RawRecord) -> Any:
        feature: dict[str, Any] = record.payload if isinstance(record.payload, dict) else {}
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (0.0, 0.0)
        depth_km = coords[2] if len(coords) >= 3 else None
        magnitude = props.get("mag")

        headline = props.get("title") or f"Earthquake M{magnitude}"
        payload = {
            "magnitude": magnitude,
            "depth_km": depth_km,
            "place": props.get("place"),
            "felt_reports": props.get("felt"),
            "cdi": props.get("cdi"),
            "alert_level": props.get("alert"),
            "tsunami_flag": bool(props.get("tsunami")),
            "significance": props.get("sig"),
        }
        # Depth and magnitude are the structured facts; the platform's relevance
        # engine decides whether they matter for this region or user.
        return self.build_observation(
            record,
            observation_type=ObservationType.EARTHQUAKE,
            geometry=point(float(lon), float(lat)),
            headline=str(headline)[:200],
            structured_payload=payload,
            location_precision_m=5000.0,
            authority=Authority.OFFICIAL,
        )

    def checkpoint(self) -> str | None:
        return self._last_event_time
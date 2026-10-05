"""Autonomous background collector for live external sources (Design Platform §4-7).

Periodically queries configured real-world sources and refreshes live feeds:
- Seattle Fire Real-Time 911 Calls
- Seattle Police Department CAD 911 Calls
- Seattle Special Events Permits (rallies, marches, demonstrations)
- SDOT Street Closures (live and planned)
- WSDOT Highway Road Alerts & Incidents
- WSDOT Mountain Pass Reports
- USGS Real-Time Earthquakes (24h GeoJSON)
- National Weather Service Active Watches & Warnings
- Washington State Ferries (WSF Live Vessels)
- SDOT Live Drawbridge Open/Close Status
- WSDOT Active Construction Work Zones
- WSDOT Vertical Clearance & Bridge Restrictions
- Police & Local News RSS Feeds (SPD Blotter, Capitol Hill Seattle, West Seattle, KING5, KOMO)

Writes snapshot files atomically to ``live_feeds/`` so that snapshot adapters
naturally detect file changes via signature (mtime/size) and trigger the section 71
continuous ingestion, state rebuilding, capability selection, and user notification cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import Settings, get_settings, workspace_root
from ..domain.ids import utcnow

log = logging.getLogger(__name__)

#: Default user agent for ethical public API access
DEFAULT_USER_AGENT = "Mozilla/5.0 (DI3-Platform; Autonomous Event Intelligence; +https://github.com)"


@dataclass(frozen=True)
class SourceEndpoint:
    """Specification for one live source feed."""

    source_id: str
    url: str
    target_filename: str
    feed_format: str = "json"  # "json", "geojson", "rss"
    description: str = ""
    timeout_s: float = 15.0
    headers: dict[str, str] = field(default_factory=dict)


#: Live real-world Puget Sound and national safety endpoints
LIVE_SOURCE_ENDPOINTS: tuple[SourceEndpoint, ...] = (
    SourceEndpoint(
        source_id="seattle_fire_911",
        url="https://data.seattle.gov/resource/kzjm-xkqj.json?$limit=500&$order=datetime%20DESC",
        target_filename="seattle_fire_realtime_911.json",
        feed_format="json",
        description="Seattle Fire Department Real-Time 911 Dispatches",
    ),
    SourceEndpoint(
        source_id="seattle_spd_cad",
        url="https://data.seattle.gov/resource/33kz-ixgy.json?$limit=500&$order=cad_event_original_time_queued%20DESC",
        target_filename="seattle_spd_call_data.json",
        feed_format="json",
        description="Seattle Police Department 911 CAD Call Data",
    ),
    SourceEndpoint(
        source_id="seattle_special_events",
        url="https://data.seattle.gov/resource/dm95-f8w5.json?$limit=500&$order=event_start_date%20DESC",
        target_filename="seattle_special_events_permits.json",
        feed_format="json",
        description="Seattle Special Events Permits (rallies, marches, parades)",
    ),

    SourceEndpoint(
        source_id="sdot_street_closures",
        url="https://data.seattle.gov/resource/ium9-iqtc.json?$limit=500",
        target_filename="sdot_street_closures.json",
        feed_format="json",
        description="Seattle SDOT Street Closures (live and scheduled)",
    ),
    SourceEndpoint(
        source_id="wsdot_road_alerts",
        url="https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/TravelInfoRoadAlerts/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=200",
        target_filename="wsdot_road_alerts.json",
        feed_format="geojson",
        description="WSDOT Highway Road Alerts & Active Incidents",
    ),
    SourceEndpoint(
        source_id="wsdot_mountain_passes",
        url="https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/TravelInfoMtPassReports/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=50",
        target_filename="wsdot_mountain_passes.json",
        feed_format="geojson",
        description="WSDOT Mountain Pass Conditions & Restrictions",
    ),
    SourceEndpoint(
        source_id="usgs_earthquakes",
        url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
        target_filename="usgs_earthquakes_day.geojson",
        feed_format="geojson",
        description="USGS Earthquakes (24-hour global/regional)",
    ),
    SourceEndpoint(
        source_id="nws_active_alerts",
        url="https://api.weather.gov/alerts/active?status=actual&area=WA",
        target_filename="nws_active_alerts.json",
        feed_format="geojson",
        description="National Weather Service Active Watches & Warnings",
    ),
    SourceEndpoint(
        source_id="wsf_ferries",
        url="https://www.wsdot.wa.gov/ferries/api/vessels/rest/vessellocations?apiaccesscode=test",
        target_filename="wsf_ferry_vessels.json",
        feed_format="json",
        description="Washington State Ferries Live Vessels & Status",
    ),
    SourceEndpoint(
        source_id="sdot_drawbridges",
        url="https://data.seattle.gov/resource/gm8h-9449.json?$limit=100",
        target_filename="sdot_drawbridge_status.json",
        feed_format="json",
        description="SDOT Live Drawbridge Open/Close Status",
    ),
    SourceEndpoint(
        source_id="wsdot_work_zones",
        url="https://data.wsdot.wa.gov/arcgis/rest/services/WorkZone/WorkZone/MapServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=100",
        target_filename="wsdot_work_zones.json",
        feed_format="geojson",
        description="WSDOT Active Construction Work Zones",
    ),
    SourceEndpoint(
        source_id="wsdot_bridges",
        url="https://data.wsdot.wa.gov/arcgis/rest/services/Vertical_Clearance/Vertical_Clearance/MapServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=100",
        target_filename="wsdot_bridges.json",
        feed_format="geojson",
        description="WSDOT Vertical Clearance & Bridge Restrictions",
    ),
    SourceEndpoint(
        source_id="spd_blotter_rss",
        url="https://spdblotter.seattle.gov/feed/",
        target_filename="spd_blotter.json",
        feed_format="rss",
        description="Seattle Police Department Blotter RSS",
    ),
    SourceEndpoint(
        source_id="capitol_hill_news_rss",
        url="https://www.capitolhillseattle.com/feed/",
        target_filename="capitol_hill_news.json",
        feed_format="rss",
        description="Capitol Hill Seattle News RSS (Demonstrations & Local Events)",
    ),
    SourceEndpoint(
        source_id="west_seattle_news_rss",
        url="https://westseattleblog.com/feed/",
        target_filename="west_seattle_news.json",
        feed_format="rss",
        description="West Seattle Blog RSS (Bridge & Transit Disruptions)",
    ),
    SourceEndpoint(
        source_id="king5_news_rss",
        url="https://www.king5.com/feeds/syndication/rss/news/local",
        target_filename="king5_news.json",
        feed_format="rss",
        description="KING5 Seattle Local News RSS",
    ),
    SourceEndpoint(
        source_id="komo_news_rss",
        url="https://komonews.com/news/local.rss",
        target_filename="komo_news.json",
        feed_format="rss",
        description="KOMO Seattle Local News RSS",
    ),
)


@dataclass
class CollectionReport:
    """Summary of one collection pass."""

    started_at: datetime
    duration_s: float = 0.0
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    records_acquired: int = 0
    bytes_downloaded: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_s, 2),
            "succeeded": self.sources_succeeded,
            "failed": self.sources_failed,
            "records": self.records_acquired,
            "bytes": self.bytes_downloaded,
            "errors": dict(self.errors),
        }


class LiveSourceCollector:
    """Autonomous scheduler and executor for live source collection."""

    def __init__(
        self,
        target_dir: Path | None = None,
        *,
        endpoints: Sequence[SourceEndpoint] | None = None,
        interval_s: float = 60.0,
        timeout_s: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.target_dir = target_dir or (workspace_root() / "live_feeds")
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.endpoints = tuple(endpoints or LIVE_SOURCE_ENDPOINTS)
        self.interval_s = max(5.0, interval_s)
        self.timeout_s = max(2.0, timeout_s)
        self.user_agent = user_agent

        self._stopping = asyncio.Event()
        self._running = False
        self._last_report: CollectionReport | None = None
        self._ssl_ctx = ssl._create_unverified_context()
        self._total_passes = 0

    @property
    def last_report(self) -> CollectionReport | None:
        return self._last_report

    def is_running(self) -> bool:
        return self._running and not self._stopping.is_set()

    # -- execution hooks --------------------------------------------------

    def _fetch_endpoint_sync(self, endpoint: SourceEndpoint) -> tuple[int, int, str | None]:
        """Synchronously download and atomically save one endpoint."""
        target_path = self.target_dir / endpoint.target_filename
        tmp_path = self.target_dir / f"{endpoint.target_filename}.tmp"

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, application/geo+json, application/xml, text/xml, */*",
            **endpoint.headers,
        }
        req = urllib.request.Request(endpoint.url, headers=headers)

        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=endpoint.timeout_s or self.timeout_s) as res:
                raw_bytes = res.read()
        except Exception as exc:
            return 0, 0, f"HTTP fetch failed: {exc}"

        # Parse & structure
        records_count = 0
        try:
            if endpoint.feed_format in {"json", "geojson"}:
                data = json.loads(raw_bytes.decode("utf-8"))
                if isinstance(data, list):
                    records_count = len(data)
                elif isinstance(data, dict):
                    records_count = len(data.get("features", [])) or (1 if data else 0)
                # Atomic write
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, target_path)

            elif endpoint.feed_format == "rss":
                root = ET.fromstring(raw_bytes)
                items = []
                for item in root.findall(".//item"):
                    title = item.find("title").text if item.find("title") is not None else ""
                    link = item.find("link").text if item.find("link") is not None else ""
                    pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                    desc = item.find("description").text if item.find("description") is not None else ""
                    items.append({
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                        "description": (desc or "")[:350],
                    })
                records_count = len(items)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(items, f, indent=2)
                os.replace(tmp_path, target_path)

            else:
                with open(tmp_path, "wb") as f:
                    f.write(raw_bytes)
                os.replace(tmp_path, target_path)
                records_count = 1

        except Exception as exc:
            if tmp_path.exists():
                with contextlib.suppress(Exception):
                    tmp_path.unlink()
            return 0, len(raw_bytes), f"parse/write failed: {exc}"

        return records_count, len(raw_bytes), None

    async def collect_source(self, endpoint: SourceEndpoint) -> tuple[str, int, int, str | None]:
        """Run one source collection asynchronously off the event loop thread."""
        records, size_bytes, error = await asyncio.to_thread(self._fetch_endpoint_sync, endpoint)
        return endpoint.source_id, records, size_bytes, error

    async def collect_once(self) -> CollectionReport:
        """Run a complete collection pass across all configured endpoints."""
        started = time.perf_counter()
        report = CollectionReport(started_at=utcnow(), sources_attempted=len(self.endpoints))

        tasks = [self.collect_source(ep) for ep in self.endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for ep, res in zip(self.endpoints, results):
            if isinstance(res, Exception):
                report.sources_failed += 1
                report.errors[ep.source_id] = repr(res)
            else:
                source_id, records, size_bytes, err = res
                if err is not None:
                    report.sources_failed += 1
                    report.errors[source_id] = err
                else:
                    report.sources_succeeded += 1
                    report.records_acquired += records
                    report.bytes_downloaded += size_bytes

        report.duration_s = time.perf_counter() - started
        self._last_report = report
        self._total_passes += 1
        log.info(
            "source collector pass %d: %d/%d succeeded (%d records, %d bytes) in %.2fs",
            self._total_passes,
            report.sources_succeeded,
            report.sources_attempted,
            report.records_acquired,
            report.bytes_downloaded,
            report.duration_s,
        )
        return report

    async def run_forever(self) -> None:
        """Continuously poll configured sources until stopped."""
        self._running = True
        self._stopping.clear()
        log.info(
            "live source collector running: endpoints=%d interval=%.1fs target=%s",
            len(self.endpoints),
            self.interval_s,
            self.target_dir,
        )

        try:
            # Immediate initial collection
            await self.collect_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("initial collection pass encountered error: %r", exc)

        while not self._stopping.is_set():
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_s)
                if self._stopping.is_set():
                    break
                await self.collect_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected collector failure: %r", exc)

        self._running = False
        log.info("live source collector stopped cleanly")

    async def stop(self) -> None:
        """Signal collector to stop and wait for loop exit."""
        self._stopping.set()


__all__ = ["CollectionReport", "LIVE_SOURCE_ENDPOINTS", "LiveSourceCollector", "SourceEndpoint"]

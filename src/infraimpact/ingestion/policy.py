"""Stage 1 Ingestion Policies, Fingerprinting & Disappearance Detection (Stage 1 §2).

Provides:
1. Source policies specifying polling interval, feed semantics, ID field, and disappearance threshold.
2. Canonical record fingerprinting excluding transient fields (scrape time, ads, view counters).
3. Article deduplication via canonical URL, content hash, and title similarity.
4. Disappearance detection: missing records across N consecutive successful polls emit
   a `source_record_ended` observation to cleanly end conditions with a measured duration.
5. Ingestion freshness and future-date guards.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..domain.enums import (
    Authority,
    EventKind,
    EventPhase,
    ObservationType,
    SignificanceClass,
    SourceSemantics,
    SourceType,
)
from ..domain.ids import deterministic_id
from ..domain.schemas import Observation, ObservationQuality, Provenance, SourcePolicy, SourceRecord

log = logging.getLogger(__name__)

#: Stage 1 section 9: Source Families for syndication / republication tracking
SOURCE_FAMILIES: dict[str, str] = {
    "king5_news_rss": "local_tv_news",
    "komo_news_rss": "local_tv_news",
    "capitol_hill_news_rss": "community_news",
    "west_seattle_news_rss": "community_news",
    "seattle_spd_cad": "seattle_police",
    "spd_blotter_rss": "seattle_police",
    "seattle_fire_911": "seattle_fire",
    "sdot_street_closures": "sdot",
    "sdot_drawbridges": "sdot",
    "seattle_special_events": "sdot",
    "wsdot_road_alerts": "wsdot",
    "wsdot_mountain_passes": "wsdot",
    "wsdot_work_zones": "wsdot",
    "wsdot_bridges": "wsdot",
    "wsf_ferries": "wsdot",
    "nws_active_alerts": "nws",
    "usgs_earthquakes": "usgs",
    "synthetic.official": "synthetic",
    "synthetic.police": "synthetic",
    "synthetic.news": "synthetic",
    "synthetic.sensor": "synthetic",
}

#: Default source policies per registered feed (Stage 1 section 2)
DEFAULT_SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "seattle_fire_911": SourcePolicy(
        source_id="seattle_fire_911",
        poll_interval_s=30.0,
        semantics=SourceSemantics.EVENT_FEED,
        id_field="incident_number",
        expected_update_rate=10.0,
        disappearance_threshold_polls=3,
        freshness_ttl_s=21600.0,  # 6h
    ),
    "seattle_spd_cad": SourcePolicy(
        source_id="seattle_spd_cad",
        poll_interval_s=30.0,
        semantics=SourceSemantics.EVENT_FEED,
        id_field="cad_event_number",
        expected_update_rate=15.0,
        disappearance_threshold_polls=3,
        freshness_ttl_s=21600.0,  # 6h
    ),
    "sdot_street_closures": SourcePolicy(
        source_id="sdot_street_closures",
        poll_interval_s=60.0,
        semantics=SourceSemantics.STATE_FEED,
        id_field="closure_id",
        expected_update_rate=5.0,
        disappearance_threshold_polls=3,  # Road closures end after 3 missed polls
        freshness_ttl_s=86400.0 * 7,      # Up to a week if still active
    ),
    "seattle_special_events": SourcePolicy(
        source_id="seattle_special_events",
        poll_interval_s=300.0,
        semantics=SourceSemantics.SCHEDULE_FEED,
        id_field="permit_number",
        expected_update_rate=1.0,
        disappearance_threshold_polls=5,
        freshness_ttl_s=86400.0 * 30,
    ),
    "wsdot_road_alerts": SourcePolicy(
        source_id="wsdot_road_alerts",
        poll_interval_s=60.0,
        semantics=SourceSemantics.EVENT_FEED,
        id_field="alert_id",
        expected_update_rate=5.0,
        disappearance_threshold_polls=3,
        freshness_ttl_s=21600.0,
    ),
    "wsdot_mountain_passes": SourcePolicy(
        source_id="wsdot_mountain_passes",
        poll_interval_s=120.0,
        semantics=SourceSemantics.STATE_FEED,
        id_field="mountain_pass_id",
        expected_update_rate=1.0,
        disappearance_threshold_polls=4,
        freshness_ttl_s=86400.0,
    ),
    "usgs_earthquakes": SourcePolicy(
        source_id="usgs_earthquakes",
        poll_interval_s=120.0,
        semantics=SourceSemantics.EVENT_FEED,
        id_field="id",
        expected_update_rate=0.5,
        disappearance_threshold_polls=5,
        freshness_ttl_s=86400.0 * 2,
    ),
    "nws_active_alerts": SourcePolicy(
        source_id="nws_active_alerts",
        poll_interval_s=60.0,
        semantics=SourceSemantics.EVENT_FEED,
        id_field="id",
        expected_update_rate=2.0,
        disappearance_threshold_polls=3,
        freshness_ttl_s=43200.0,
    ),
    "wsf_ferries": SourcePolicy(
        source_id="wsf_ferries",
        poll_interval_s=60.0,
        semantics=SourceSemantics.STATE_FEED,
        id_field="vessel_id",
        expected_update_rate=5.0,
        disappearance_threshold_polls=4,
        freshness_ttl_s=14400.0,
    ),
    "sdot_drawbridges": SourcePolicy(
        source_id="sdot_drawbridges",
        poll_interval_s=60.0,
        semantics=SourceSemantics.STATE_FEED,
        id_field="bridge_id",
        expected_update_rate=2.0,
        disappearance_threshold_polls=3,
        freshness_ttl_s=14400.0,
    ),
    "wsdot_work_zones": SourcePolicy(
        source_id="wsdot_work_zones",
        poll_interval_s=300.0,
        semantics=SourceSemantics.STATE_FEED,
        id_field="work_zone_id",
        expected_update_rate=1.0,
        disappearance_threshold_polls=5,
        freshness_ttl_s=86400.0 * 7,
    ),
    "wsdot_bridges": SourcePolicy(
        source_id="wsdot_bridges",
        poll_interval_s=600.0,
        semantics=SourceSemantics.STATE_FEED,
        id_field="bridge_id",
        expected_update_rate=0.2,
        disappearance_threshold_polls=6,
        freshness_ttl_s=86400.0 * 30,
    ),
    "spd_blotter_rss": SourcePolicy(
        source_id="spd_blotter_rss",
        poll_interval_s=180.0,
        semantics=SourceSemantics.ARTICLE_FEED,
        id_field="link",
        expected_update_rate=2.0,
        disappearance_threshold_polls=10,
        freshness_ttl_s=43200.0,
    ),
    "capitol_hill_news_rss": SourcePolicy(
        source_id="capitol_hill_news_rss",
        poll_interval_s=180.0,
        semantics=SourceSemantics.ARTICLE_FEED,
        id_field="link",
        expected_update_rate=2.0,
        disappearance_threshold_polls=10,
        freshness_ttl_s=43200.0,
    ),
    "west_seattle_news_rss": SourcePolicy(
        source_id="west_seattle_news_rss",
        poll_interval_s=180.0,
        semantics=SourceSemantics.ARTICLE_FEED,
        id_field="link",
        expected_update_rate=2.0,
        disappearance_threshold_polls=10,
        freshness_ttl_s=43200.0,
    ),
    "king5_news_rss": SourcePolicy(
        source_id="king5_news_rss",
        poll_interval_s=180.0,
        semantics=SourceSemantics.ARTICLE_FEED,
        id_field="link",
        expected_update_rate=4.0,
        disappearance_threshold_polls=10,
        freshness_ttl_s=43200.0,
    ),
    "komo_news_rss": SourcePolicy(
        source_id="komo_news_rss",
        poll_interval_s=180.0,
        semantics=SourceSemantics.ARTICLE_FEED,
        id_field="link",
        expected_update_rate=4.0,
        disappearance_threshold_polls=10,
        freshness_ttl_s=43200.0,
    ),
}

#: Keys ignored during fingerprinting to prevent noise/timestamp churning
TRANSIENT_KEYS = frozenset(
    {
        "fetched_at",
        "scraped_at",
        "observed_at",
        "ingested_at",
        "query_time",
        "poll_timestamp",
        "view_count",
        "clicks",
        "ads",
        "_admission",
        "cache_buster",
        "session_id",
        "etag",
        "timestamp_ms",
    }
)


def get_source_policy(source_id: str) -> SourcePolicy:
    """Retrieve policy for a source, falling back to sane defaults."""
    if source_id in DEFAULT_SOURCE_POLICIES:
        return DEFAULT_SOURCE_POLICIES[source_id]
    normalized_id = source_id.replace(".", "_")
    if normalized_id in DEFAULT_SOURCE_POLICIES:
        return DEFAULT_SOURCE_POLICIES[normalized_id]
    if any(source_id.startswith(p) for p in ("sdot", "wsdot", "closure", "construction")):
        return SourcePolicy(
            source_id=source_id,
            poll_interval_s=60.0,
            semantics=SourceSemantics.STATE_FEED,
            id_field="id",
            expected_update_rate=1.0,
            disappearance_threshold_polls=3,
            freshness_ttl_s=86400.0,
        )
    return SourcePolicy(
        source_id=source_id,
        poll_interval_s=60.0,
        semantics=SourceSemantics.EVENT_FEED,
        id_field="id",
        expected_update_rate=1.0,
        disappearance_threshold_polls=3,
        freshness_ttl_s=21600.0,
    )


def canonicalize_url(url: str) -> str:
    """Strip tracking query parameters (utm_*, ref, etc.) and fragments from article URLs."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        clean_query = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.startswith("utm_") and k not in {"ref", "fbclid", "gclid", "ocid"}
        ]
        clean_url = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.params,
                urlencode(clean_query),
                "",
            )
        )
        return clean_url
    except Exception:
        return url.strip()


def compute_record_fingerprint(source_id: str, payload: dict[str, Any]) -> str:
    """Stage 1 section 2: compute fingerprint = hash(normalized meaningful fields).

    Excludes fetch timestamps, view counters, ads, and transient parameters.
    """
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        if k.lower() in TRANSIENT_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            cleaned[k] = v
        elif isinstance(v, dict):
            cleaned[k] = {
                sub_k: sub_v for sub_k, sub_v in v.items() if sub_k.lower() not in TRANSIENT_KEYS
            }
        elif isinstance(v, (list, tuple)):
            cleaned[k] = list(v)

    raw_json = json.dumps(cleaned, sort_keys=True, default=str)
    return hashlib.sha256(raw_json.encode("utf-8")).hexdigest()[:24]


def create_ended_observation(
    source_id: str,
    source_record_id: str,
    last_observation: Observation | None = None,
    ended_at: datetime | None = None,
) -> Observation:
    """Stage 1 section 2/3: emit source_record_ended observation when a condition disappears.

    This answers 'when did the road closure end?'.
    """
    ended_time = ended_at or datetime.now(UTC)
    obs_type = last_observation.observation_type if last_observation else ObservationType.ROAD_CLOSURE
    headline = (
        f"{last_observation.headline} - CLEARED"
        if last_observation and last_observation.headline
        else f"{source_id} record {source_record_id} ended"
    )
    obs_id = deterministic_id("obs_end", source_id, source_record_id, ended_time.isoformat())

    return Observation(
        observation_id=obs_id,
        source_id=source_id,
        source_record_id=source_record_id,
        event_time=ended_time,
        event_time_confidence="known",
        published_at=ended_time,
        observed_at=ended_time,
        ingested_at=ended_time,
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=obs_type,
        significance_class=SignificanceClass.EVENT_CANDIDATE,
        version=(last_observation.version + 1) if last_observation else 2,
        geometry=last_observation.geometry if last_observation else None,
        location_precision_m=last_observation.location_precision_m if last_observation else None,
        headline=headline,
        structured_payload={
            "status": "cleared",
            "is_closed": False,
            "disappeared": True,
            "ended_at": ended_time.isoformat(),
            "prior_observation_id": last_observation.observation_id if last_observation else None,
        },
        provenance=Provenance(
            authority=Authority.OFFICIAL,
            retrieval_method="derived",
            content_hash=deterministic_id("hash_end", source_id, source_record_id),
        ),
        quality=ObservationQuality(
            source_reliability=0.9,
            temporal_precision=0.9,
            spatial_precision=0.9 if last_observation and last_observation.geometry else 0.2,
            extraction_confidence=1.0,
        ),
        centroid=last_observation.centroid if last_observation else None,
        event_id=last_observation.event_id if last_observation else None,
    )

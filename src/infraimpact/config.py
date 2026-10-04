"""Configuration and region profiles (brief section 4/67).

Freshness expectations live in source configuration, never in application
logic (section 56). Nothing here is Puget-Sound-specific in code paths - a
region profile selects adapters and datasets.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain.enums import CapabilityTier
from .domain.geo import bbox_polygon


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key, "") or str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Environment-driven settings with safe local defaults."""

    app_name: str = "infraimpact"
    environment: str = _env("INFRAIMPACT_ENV", "local")

    # storage: sqlite now, postgres/postgis in production (section 42)
    database_url: str = _env("INFRAIMPACT_DATABASE_URL", "sqlite:///./data/infraimpact.db")
    driver: str = _env("INFRAIMPACT_DRIVER", "sqlite")
    raw_store_path: str = _env("INFRAIMPACT_RAW_STORE", "./data/raw")

    # event bus
    bus_backend: str = _env("INFRAIMPACT_BUS", "memory")
    kafka_bootstrap: str = _env("INFRAIMPACT_KAFKA_BOOTSTRAP", "localhost:9092")

    # runtime
    region_id: str = _env("INFRAIMPACT_REGION", "puget-sound")
    loop_interval_s: float = _env_float("INFRAIMPACT_LOOP_INTERVAL_S", 2.0)
    poll_interval_s: float = _env_float("INFRAIMPACT_POLL_INTERVAL_S", 5.0)
    enable_collector: bool = _env_bool(
        "INFRAIMPACT_ENABLE_COLLECTOR",
        "pytest" not in sys.modules and _env("INFRAIMPACT_ENV", "local") != "test",
    )
    collector_interval_s: float = _env_float("INFRAIMPACT_COLLECTOR_INTERVAL_S", 60.0)
    collector_timeout_s: float = _env_float("INFRAIMPACT_COLLECTOR_TIMEOUT_S", 15.0)
    event_ttl_hours: float = _env_float("INFRAIMPACT_EVENT_TTL_HOURS", 12.0)
    resolver_time_window_min: float = _env_float("INFRAIMPACT_RESOLVER_WINDOW_MIN", 180.0)
    resolver_search_radius_m: float = _env_float("INFRAIMPACT_RESOLVER_RADIUS_M", 2500.0)
    resolver_merge_threshold: float = _env_float("INFRAIMPACT_RESOLVER_THRESHOLD", 0.62)

    # analysis gating
    notify_min_priority: float = _env_float("INFRAIMPACT_NOTIFY_MIN_PRIORITY", 0.35)
    official_guidance_bypass: bool = _env_bool("INFRAIMPACT_OFFICIAL_BYPASS", True)
    enable_network_sources: bool = _env_bool("INFRAIMPACT_ENABLE_NETWORK", False)
    #: The synthetic scenario generator fabricates events. It exists so the
    #: loop can be exercised with no network and no snapshot data, so it is
    #: off unless explicitly asked for: a real ledger must not silently
    #: contain invented incidents.
    enable_synthetic_sources: bool = _env_bool("INFRAIMPACT_ENABLE_SYNTHETIC", False)

    # interpretation / orchestration (section 18)
    #: The LLM layer runs unless explicitly switched off. It is advisory
    #: everywhere it is used, so leaving it on is a cost question, not a safety
    #: one - but the switch exists so an operator can run a comparison against a
    #: build with no interpretation at all.
    llm_enabled: bool = _env_bool("INFRAIMPACT_ENABLE_LLM", True)
    #: Completion calls permitted per cycle across the whole loop. The loop fans
    #: out over events and then over users, so an unbounded budget turns one busy
    #: event into dozens of near-identical calls.
    llm_calls_per_cycle: int = _env_int("INFRAIMPACT_LLM_CALLS_PER_CYCLE", 12)

    # external credentials (never reach clients - section 61)
    wsdot_api_key: str = _env("WSDOT_API_KEY", "")
    sdot_api_key: str = _env("SDOT_API_KEY", "")
    onebusaway_api_key: str = _env("ONEBUSAWAY_API_KEY", "")
    acled_api_key: str = _env("ACLED_API_KEY", "")
    nws_user_agent: str = _env("NWS_USER_AGENT", "infraimpact/0.1 (contact@example.com)")

    @property
    def is_sqlite(self) -> bool:
        return self.driver == "sqlite" or self.database_url.startswith("sqlite")


@dataclass(frozen=True)
class SourceSpec:
    """Section 56: expected freshness profile per source."""

    source_id: str
    expected_interval_s: float
    stale_after_s: float
    usage: str = "realtime"
    authority: str = "official"
    reliability: float = 0.9
    capability_tier: CapabilityTier = CapabilityTier.TRIGGERED
    enabled: bool = True
    note: str = ""


@dataclass(frozen=True)
class RegionProfile:
    """Section 4. A region selects adapters and datasets; nothing else."""

    region_id: str
    display_name: str
    timezone: str
    bounds: tuple[float, float, float, float]
    center: tuple[float, float]
    source_adapters: tuple[str, ...]
    #: Fabricated scenario sources. Never included unless explicitly enabled,
    #: because they write invented events into the same ledger as real ones.
    synthetic_sources: tuple[str, ...] = ()
    infrastructure_datasets: tuple[str, ...] = ()
    source_specs: dict[str, SourceSpec] = field(default_factory=dict)

    @property
    def bbox(self) -> dict[str, Any]:
        return bbox_polygon(self.bounds)


# Greater Puget Sound. Seattle at -122.335, 47.608.
#
# The bounds deliberately include navigable water, not just land. A
# land-only box clips Puget Sound at roughly longitude -122.55, which puts
# half the Washington State Ferries fleet outside the region even though they
# are sailing the Seattle/Bainbridge, Seattle/Bremerton and Seattle/Vashon
# routes. Verified against ``live_feeds/wsf_ferry_vessels.json``: a land-only
# box admits 12 of 21 vessels; this one admits all 21, plus 13 Pacific
# Northwest seismic picks instead of 4. The cost is some extra statewide and
# offshore noise, which the relevance engine already discards downstream.
PUGET_SOUND = RegionProfile(
    region_id="puget-sound",
    display_name="Seattle / Greater Puget Sound",
    timezone="America/Los_Angeles",
    bounds=(-123.10, 47.10, -121.70, 48.70),
    center=(-122.335, 47.608),
    # Live HTTP connectors, used when INFRAIMPACT_ENABLE_NETWORK=1. These are
    # the network-mode counterparts of catalogue signals such as
    # ``usgs.earthquakes`` and ``nws.alerts`` - alternatives, not additions,
    # so exactly one retrieval path per signal is ever active.
    source_adapters=(
        "usgs.earthquakes",
        "nws.alerts",
        "wsdot.highway_alerts",
        "sdot.row_impacts",
        "metro.gtfs_rt",
    ),
    synthetic_sources=("synthetic.puget_sound",),
    infrastructure_datasets=(
        "roads.osm",
        "transit.gtfs",
        "facilities.hospitals_schools_fire",
        "population.census",
    ),
    source_specs={
        "synthetic.puget_sound": SourceSpec(
            source_id="synthetic.puget_sound",
            expected_interval_s=2.0,
            stale_after_s=15.0,
            usage="realtime",
            authority="internal",
            reliability=0.7,
            capability_tier=CapabilityTier.CHEAP,
            note="Offline scenario generator; keeps the loop runnable with no network.",
        ),
        "usgs.earthquakes": SourceSpec(
            source_id="usgs.earthquakes",
            expected_interval_s=60.0,
            stale_after_s=300.0,
            usage="realtime",
            reliability=0.95,
            note="Public GeoJSON feed, no key required.",
        ),
        "nws.alerts": SourceSpec(
            source_id="nws.alerts",
            expected_interval_s=60.0,
            stale_after_s=600.0,
            usage="realtime",
            reliability=0.95,
            note="Public active-alerts feed, no key required.",
        ),
        "wsdot.highway_alerts": SourceSpec(
            source_id="wsdot.highway_alerts",
            expected_interval_s=60.0,
            stale_after_s=600.0,
            reliability=0.95,
            note="Requires WSDOT_API_KEY.",
        ),
        "sdot.row_impacts": SourceSpec(
            source_id="sdot.row_impacts",
            expected_interval_s=300.0,
            stale_after_s=1800.0,
            reliability=0.93,
            note="Requires SDOT_API_KEY.",
        ),
        "metro.gtfs_rt": SourceSpec(
            source_id="metro.gtfs_rt",
            expected_interval_s=30.0,
            stale_after_s=300.0,
            reliability=0.94,
            note="TripUpdates/Positions/Alerts via OneBusAway-compatible endpoint.",
        ),
        "historical.acled": SourceSpec(
            source_id="historical.acled",
            expected_interval_s=604800.0,
            stale_after_s=1209600.0,
            usage="historical_only",
            reliability=0.85,
            note="Weekly reviewed data. Historical/training layer, not a live detector.",
        ),
    },
)

REGIONS: dict[str, RegionProfile] = {PUGET_SOUND.region_id: PUGET_SOUND}


def get_region(region_id: str | None = None) -> RegionProfile:
    rid = region_id or os.environ.get("INFRAIMPACT_REGION", PUGET_SOUND.region_id)
    if rid not in REGIONS:
        raise KeyError(f"unknown region '{rid}'; known: {sorted(REGIONS)}")
    return REGIONS[rid]


def get_settings() -> Settings:
    return Settings()


def workspace_root() -> Path:
    """Locate the repository root that holds ``live_feeds/``, ``data/`` and ``llm/``.

    Catalogue ``subdir`` values are paths relative to this root, so a signal can
    live outside ``data/`` - the captured live feeds sit at the top level while
    the datasets sit under ``data/`` - and the supplied ``llm`` package is
    addressed relative to it too.

    It lives here rather than in ``sources.snapshot`` because three layers need
    it (sources, the LLM adapter, the CLI), and a path helper in a leaf module
    forces the other two to import the source layer to find their own root.

    Order: ``INFRAIMPACT_DATA_ROOT``, then the first ancestor of this file or
    the working directory that contains both ``live_feeds`` and ``data``, then
    the current working directory.
    """

    override = os.environ.get("INFRAIMPACT_DATA_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    seen: set[Path] = set()
    for start in (Path(__file__).resolve().parents[2], Path.cwd().resolve()):
        for candidate in [start, *start.parents]:
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "live_feeds").is_dir() and (candidate / "data").is_dir():
                return candidate
    return Path.cwd()


def sqlite_path(url: str) -> Path:
    raw = url.split("sqlite:///", 1)[-1] if url.startswith("sqlite:///") else url
    return Path(raw)


def data_dir() -> Path:
    return sqlite_path(get_settings().raw_store_path).parent
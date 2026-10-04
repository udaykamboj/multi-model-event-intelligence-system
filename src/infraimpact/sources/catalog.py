"""Real-time signal catalogue (brief section 4, spec sections 33-35).

One table describing every signal the platform can ingest for a region. This
module is *data*, not behaviour: adapters are registered against ``adapter``
names here, and nothing outside this file needs to know which agencies or
endpoints exist.

Two things this table is deliberately honest about
--------------------------------------------------
1. **Aliases.** ``data/live_feeds`` contains 46 files but only 29 distinct
   payloads; several are byte-identical copies saved under different names.
   Each entry lists every filename that backs it, and the adapters only read
   the first one that exists, so duplicates cost nothing at runtime.

2. **Known gaps.** Some snapshot files do not contain what their name
   suggests (there is no traffic-*flow* payload; there are no ferry service
   bulletins). Those entries are marked ``usable=False`` with a ``note``
   explaining why. They stay in the catalogue so the gap is visible rather
   than silently missing, and so the live endpoint listed for them can be
   turned on the moment credentials exist.

``endpoint`` records where the signal comes from when polling live; ``files``
records what is on disk right now. ``usage`` separates live detection from
historical context so a weekly-reviewed dataset can never drive a real-time
notification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: The fifteen signal families the platform is specified to cover.
Category = Literal[
    "police_fire",          # A  police and fire activity
    "demonstrations",       # B  demonstrations and special-event permits
    "news",                 # C  local and regional news
    "road_closure",         # D  road closures and highway incidents
    "traffic",              # E  traffic speeds, travel times, cameras
    "transit",              # F  transit vehicles, delays, service alerts
    "ferry",                # G  ferry disruptions
    "construction",         # H  construction and work zones
    "bridges",              # I  bridges and transport bottlenecks
    "weather",              # J  weather and emergency alerts
    "seismic",              # K  earthquakes and seismic activity
    "facilities",           # L  hospitals, schools, fire, public buildings
    "population",           # M  population density and land use
    "historical",           # N  historical event records
    "user_context",         # O  user location, destination, normal routes
]

#: Signals that describe a baseline rather than a change. They still become
#: observations, but they may never raise a notification on their own.
CONTEXT_ONLY = "context_only"


@dataclass(frozen=True)
class SignalSpec:
    """One ingestible signal.

    ``adapter`` is the key registered in :mod:`infraimpact.sources.registry`.
    ``files`` are snapshot filenames under ``data/live_feeds`` (or
    ``data/`` for datasets); the first existing one is read. ``endpoint`` is
    the live URL the same adapter would poll when network mode is enabled.
    """

    source_id: str
    category: Category
    adapter: str
    label: str

    #: Snapshot filenames backing this signal, most-canonical first.
    files: tuple[str, ...] = ()
    #: Path to ``files`` relative to the workspace root. Captured live
    #: signals sit at ``live_feeds/``; datasets sit under ``data/``.
    subdir: str = "live_feeds"

    #: Live endpoint for the same signal (network mode).
    endpoint: str | None = None
    #: Whether the endpoint needs an API key to be polled.
    requires_key: str | None = None

    #: Nominal refresh period and staleness threshold, in seconds.
    interval_s: float = 300.0
    stale_after_s: float = 1800.0

    #: How much the source can be trusted, 0-1. Feeds that are official but
    #: occasionally stale or mis-geocoded sit below 1.0 deliberately.
    reliability: float = 0.9

    #: ``realtime`` feeds may drive notifications. ``historical_only`` feeds
    #: exist for similarity and baseline work and are excluded from alerting.
    usage: str = "realtime"

    #: False when the snapshot does not actually contain the described signal.
    usable: bool = True

    note: str = ""
    #: Extra names this signal is known by, for operators grepping configs.
    also_known_as: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary_file(self) -> str | None:
        return self.files[0] if self.files else None


# --------------------------------------------------------------------------
# Live endpoints. Grouped to mirror the categories above so the table stays
# readable; `download_all_sources*.py` in data/scripts uses the same URLs.
# --------------------------------------------------------------------------

_SPD_CAD = "https://data.seattle.gov/resource/33kz-ixgy.json?$limit=500&$order=cad_event_original_time_queued%20DESC"
_SPD_BLOTTER = "https://spdblotter.seattle.gov/feed/"
_SFD_911 = "https://data.seattle.gov/resource/kzjm-xkqj.json?$limit=500&$order=datetime%20DESC"
_SFD_HAZARDS = (
    "https://data.seattle.gov/resource/kzjm-xkqj.json?$limit=200"
    "&$where=type%20like%20'%25Hazard%25'%20OR%20type%20like%20'%25Rescue%25'"
)
_SPECIAL_EVENTS = "https://data.seattle.gov/resource/dm95-f8w5.json?$limit=500"
_PARKS_PERMITS = "https://data.seattle.gov/resource/dm95-f8w5.json?$limit=200&$where=event_location_neighborhood%20is%20not%20null"
_STREET_USE = "https://data.seattle.gov/resource/ium9-iqtc.json?$limit=500"
_KING5 = "https://www.king5.com/feeds/syndication/rss/news/local"
_KOMO = "https://komonews.com/news/local.rss"
_CAPITOL_HILL = "https://www.capitolhillseattle.com/feed/"
_WEST_SEA = "https://westseattleblog.com/feed/"
_SEATTLE_TIMES = "https://www.seattletimes.com/seattle-news/feed/"
_WSDOT_ALERTS = (
    "https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/"
    "TravelInfoRoadAlerts/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=200"
)
_WSDOT_PASSES = (
    "https://data.wsdot.wa.gov/arcgis/rest/services/TravelInformation/"
    "TravelInfoMtPassReports/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=50"
)
_WSDOT_CLEARANCE = (
    "https://data.wsdot.wa.gov/arcgis/rest/services/Vertical_Clearance/"
    "Vertical_Clearance/MapServer/0/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=100"
)
_WSDOT_WORK_ZONES = (
    "https://data.wsdot.wa.gov/arcgis/rest/services/WorkZone/WorkZone/MapServer/0/query"
    "?where=1%3D1&outFields=*&f=geojson&resultRecordCount=100"
)
_SDOT_CAMERAS = "https://web.seattle.gov/Travelers/api/Map/Data?zoomId=13&type=2"
_ONEBUSAWAY_VEHICLES = "https://api.pugetsound.onebusaway.org/api/where/vehicles-for-agency/{agency}.json"
_ONEBUSAWAY_ALERTS = "https://api.pugetsound.onebusaway.org/api/gtfs_realtime/alerts-for-agency/{agency}.pb"
_WSF_VESSELS = "https://www.wsdot.wa.gov/ferries/api/vessels/rest/vessellocations?apiaccesscode=test"
_NWS_ALERTS = "https://api.weather.gov/alerts/active?status=actual"
_USGS_DAY = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"


#: Every signal the Puget Sound profile can ingest, in category order.
SIGNALS: tuple[SignalSpec, ...] = (
    # -- A. police and fire activity ---------------------------------------
    SignalSpec(
        source_id="spd.cad_911",
        category="police_fire",
        adapter="feed.spd_cad",
        label="Seattle Police 911 CAD calls for service",
        files=("seattle_spd_call_data.json", "spd_cad_911.json"),
        endpoint=_SPD_CAD,
        interval_s=120.0,
        stale_after_s=900.0,
        reliability=0.93,
        note="Live 911 call data. Includes dispatch lat/lon and final call type, "
        "so clearance revisions arrive as new records rather than overwrites.",
        also_known_as=("spd_cad_911.json",),
    ),
    SignalSpec(
        source_id="spd.blotter",
        category="police_fire",
        adapter="feed.blotter",
        label="Seattle Police online blotter",
        files=("spd_blotter.json",),
        endpoint=_SPD_BLOTTER,
        interval_s=300.0,
        stale_after_s=1800.0,
        reliability=0.85,
        note="Narrative RSS. Unstructured, so headlines are treated as claims "
        "to be extracted rather than facts (section 18).",
    ),
    SignalSpec(
        source_id="sfd.dispatch_911",
        category="police_fire",
        adapter="feed.sfd_dispatch",
        label="Seattle Fire 911 dispatch",
        files=("seattle_fire_realtime_911.json", "sfd_realtime_911.json"),
        endpoint=_SFD_911,
        interval_s=120.0,
        stale_after_s=900.0,
        reliability=0.93,
        note="Fire and aid response dispatches with incident numbers.",
        also_known_as=("sfd_realtime_911.json",),
    ),
    SignalSpec(
        source_id="sfd.active_hazards",
        category="police_fire",
        adapter="feed.sfd_dispatch",
        label="Seattle Fire hazard and rescue dispatches",
        files=("sfd_active_hazards.json",),
        endpoint=_SFD_HAZARDS,
        interval_s=180.0,
        stale_after_s=1200.0,
        reliability=0.9,
        note="Hazard/rescue subset of the SFD dispatch feed.",
    ),

    # -- B. demonstrations and special-event permits -----------------------
    SignalSpec(
        source_id="sdot.special_event_permits",
        category="demonstrations",
        adapter="feed.event_permits",
        label="Seattle special-event permits",
        files=("seattle_special_events_permits.json", "sdot_special_events_permits.json"),
        endpoint=_SPECIAL_EVENTS,
        interval_s=900.0,
        stale_after_s=86400.0,
        reliability=0.9,
        note="Issued permits carry no coordinates, only a neighbourhood. "
        "Location precision is therefore neighbourhood-level, never fabricated.",
        also_known_as=("sdot_special_events_permits.json",),
    ),
    SignalSpec(
        source_id="seattle.parks_permits",
        category="demonstrations",
        adapter="feed.event_permits",
        label="Seattle Parks special-event permits",
        files=("seattle_parks_permits.json",),
        endpoint=_PARKS_PERMITS,
        interval_s=900.0,
        stale_after_s=86400.0,
        reliability=0.88,
        note="Parks-issued permits, same schema as SDOT permits.",
    ),

    # -- C. local and regional news ----------------------------------------
    SignalSpec(
        source_id="news.king5",
        category="news",
        adapter="feed.news",
        label="KING 5 local news",
        files=("king5_news.json", "news_king5.json"),
        endpoint=_KING5,
        interval_s=600.0,
        stale_after_s=3600.0,
        reliability=0.7,
        note="Media RSS. Low reliability by design: reports are claims until "
        "corroborated, never authoritative on their own.",
        also_known_as=("news_king5.json",),
    ),
    SignalSpec(
        source_id="news.komo",
        category="news",
        adapter="feed.news",
        label="KOMO 9 local news",
        files=("komo_news.json", "news_komo.json"),
        endpoint=_KOMO,
        interval_s=600.0,
        stale_after_s=3600.0,
        reliability=0.7,
        also_known_as=("news_komo.json",),
    ),
    SignalSpec(
        source_id="news.capitol_hill",
        category="news",
        adapter="feed.news",
        label="Capitol Hill Seattle blog",
        files=("capitol_hill_news.json", "news_capitol_hill_seattle.json"),
        endpoint=_CAPITOL_HILL,
        interval_s=600.0,
        stale_after_s=3600.0,
        reliability=0.65,
        note="Single-neighbourhood outlet; useful for local colour, weak on authority.",
        also_known_as=("news_capitol_hill_seattle.json",),
    ),
    SignalSpec(
        source_id="news.seattle_times",
        category="news",
        adapter="feed.news",
        label="The Seattle Times",
        files=("news_seattle_times.json",),
        endpoint=_SEATTLE_TIMES,
        interval_s=600.0,
        stale_after_s=3600.0,
        reliability=0.75,
    ),

    # -- D. road closures and highway incidents ---------------------------
    SignalSpec(
        source_id="sdot.street_use",
        category="road_closure",
        adapter="feed.street_use",
        label="SDOT street use permits and closures",
        files=("sdot_street_closures.json", "sdot_street_use_permits.json", "sdot_traffic_events.json", "sdot_construction_hubs.json"),
        endpoint=_STREET_USE,
        interval_s=600.0,
        stale_after_s=7200.0,
        reliability=0.92,
        note="Carries an inline LineString per segment, so closures are real "
        "geometry rather than address strings. The four snapshot filenames are "
        "byte-identical copies of one payload.",
        also_known_as=(
            "sdot_street_use_permits.json",
            "sdot_traffic_events.json",
            "sdot_construction_hubs.json",
        ),
    ),
    SignalSpec(
        source_id="wsdot.road_alerts",
        category="road_closure",
        adapter="feed.wsdot_alerts",
        label="WSDOT highway incidents and closures",
        files=("wsdot_road_alerts.json",),
        endpoint=_WSDOT_ALERTS,
        interval_s=120.0,
        stale_after_s=900.0,
        reliability=0.95,
        note="RoadClosedFlag is carried through verbatim so a closure is never "
        "inferred from a severity string.",
    ),

    # -- E. traffic speeds, travel times, cameras -------------------------
    SignalSpec(
        source_id="sdot.traffic_cameras",
        category="traffic",
        adapter="feed.traffic_cameras",
        label="SDOT traffic camera locations",
        files=("sdot_traffic_cameras.json",),
        endpoint=_SDOT_CAMERAS,
        interval_s=86400.0,
        stale_after_s=604800.0,
        usage=CONTEXT_ONLY,
        reliability=0.9,
        note="314 camera sites. Reference data: gives coverage geometry for "
        "visual confirmation, contributes no speed measurement.",
    ),
    SignalSpec(
        source_id="sdot.traffic_flow",
        category="traffic",
        adapter="feed.traffic_cameras",
        label="SDOT traffic speeds",
        files=("sdot_traffic_flow.json",),
        endpoint=_SDOT_CAMERAS,
        interval_s=120.0,
        stale_after_s=900.0,
        reliability=0.0,
        usage=CONTEXT_ONLY,
        usable=False,
        note="GAP: the snapshot is byte-identical to sdot_traffic_cameras and "
        "contains no speed or volume readings. Signal derivation stays "
        "offline until a real speeds feed is wired in.",
    ),
    SignalSpec(
        source_id="sdot.drawbridges",
        category="bridges",
        adapter="feed.drawbridges",
        label="SDOT movable bridge open/close schedule",
        files=("sdot_drawbridge_status.json", "sdot_drawbridges.json"),
        interval_s=3600.0,
        stale_after_s=21600.0,
        usage=CONTEXT_ONLY,
        reliability=0.9,
        note="100 drawbridges with coordinates and recurring open/close windows. "
        "A schedule is a predictable delay risk on a user's route, not an "
        "incident; the two snapshot names are byte-identical copies.",
        also_known_as=("sdot_drawbridges.json",),
    ),
    SignalSpec(
        source_id="wsdot.traffic_cameras",
        category="traffic",
        adapter="feed.traffic_cameras",
        label="WSDOT traffic cameras",
        files=("wsdot_traffic_cameras.json",),
        interval_s=120.0,
        stale_after_s=900.0,
        reliability=0.0,
        usage=CONTEXT_ONLY,
        usable=False,
        note="GAP: a truncated copy of the bridge vertical-clearance payload - the "
        "same 20 properties and the same feature ids 1-50 of that feed's 100 "
        "structures, with no camera field of any kind (no location, image, url "
        "or view). The absence of cameras is therefore a real gap in the data, "
        "not a parsing failure. Camera coverage in this region comes from the "
        "SDOT set instead.",
    ),
    SignalSpec(
        source_id="wsdot.mountain_passes",
        category="traffic",
        adapter="feed.wsdot_passes",
        label="WSDOT mountain pass conditions",
        files=("wsdot_mountain_passes.json", "wsdot_travel_times.json"),
        endpoint=_WSDOT_PASSES,
        interval_s=900.0,
        stale_after_s=7200.0,
        reliability=0.9,
        note="Seasonal pass reports, mostly out of season in Puget Sound. "
        "travel_times.json is a byte-identical copy of this payload.",
        also_known_as=("wsdot_travel_times.json",),
    ),

    # -- F. transit --------------------------------------------------------
    SignalSpec(
        source_id="metro.vehicles",
        category="transit",
        adapter="feed.transit_vehicles",
        label="King County Metro vehicle positions",
        files=("transit_kcm_vehicles.json",),
        endpoint=_ONEBUSAWAY_VEHICLES.format(agency="1"),
        requires_key="onebusaway_api_key",
        interval_s=30.0,
        stale_after_s=300.0,
        reliability=0.94,
        note="1,020 vehicles with scheduled deviation and live positions.",
    ),
    SignalSpec(
        source_id="sound_transit.vehicles",
        category="transit",
        adapter="feed.transit_vehicles",
        label="Sound Transit vehicle positions",
        files=("transit_sound_transit_vehicles.json",),
        endpoint=_ONEBUSAWAY_VEHICLES.format(agency="40"),
        requires_key="onebusaway_api_key",
        interval_s=30.0,
        stale_after_s=300.0,
        reliability=0.9,
        note="160 light-rail and bus vehicles (agency 40).",
    ),
    SignalSpec(
        source_id="metro.service_alerts",
        category="transit",
        adapter="feed.transit_alerts",
        label="King County Metro service alerts",
        files=("transit_kcm_gtfs_rt_alerts.json", "transit_kcm_gtfs_rt_delays.json"),
        endpoint=_ONEBUSAWAY_ALERTS.format(agency="1"),
        requires_key="onebusaway_api_key",
        interval_s=60.0,
        stale_after_s=600.0,
        reliability=0.9,
        note="GTFS-RT alerts. gtfs_rt_delays.json is a byte-identical copy.",
        also_known_as=("transit_kcm_gtfs_rt_delays.json",),
    ),
    SignalSpec(
        source_id="sound_transit.service_alerts",
        category="transit",
        adapter="feed.transit_alerts",
        label="Sound Transit service alerts",
        files=("transit_sound_transit_link_status.json",),
        endpoint=_ONEBUSAWAY_ALERTS.format(agency="40"),
        requires_key="onebusaway_api_key",
        interval_s=60.0,
        stale_after_s=600.0,
        reliability=0.0,
        usage=CONTEXT_ONLY,
        usable=False,
        note="GAP: the file is a byte-identical copy of the King County Metro "
        "alerts, so it contains no Link light-rail data. Registered as unusable "
        "so it cannot be mistaken for Light Rail coverage.",
    ),
    SignalSpec(
        source_id="metro.gtfs_schedule",
        category="transit",
        adapter="feed.gtfs_static",
        label="King County Metro GTFS schedule",
        files=("routes.txt", "stops.txt", "trips.txt"),
        subdir="data/reference/transit_gtfs",
        interval_s=604800.0,
        stale_after_s=2592000.0,
        usage=CONTEXT_ONLY,
        reliability=0.95,
        note="142 routes and 6,265 stops. Reference layer that makes 'your bus' "
        "resolvable to real routes and stops.",
    ),

    # -- G. ferries --------------------------------------------------------
    SignalSpec(
        source_id="wsf.vessels",
        category="ferry",
        adapter="feed.ferry_vessels",
        label="Washington State Ferries vessel positions",
        files=("wsf_ferry_vessels.json", "wsf_vessel_positions.json"),
        endpoint=_WSF_VESSELS,
        interval_s=60.0,
        stale_after_s=600.0,
        reliability=0.92,
        note="21 vessels with dock state, ETA and out-of-service messages.",
        also_known_as=("wsf_vessel_positions.json",),
    ),
    SignalSpec(
        source_id="wsf.service_bulletins",
        category="ferry",
        adapter="feed.ferry_vessels",
        label="Washington State Ferries service bulletins",
        files=("wsf_service_bulletins.json",),
        interval_s=300.0,
        stale_after_s=1800.0,
        reliability=0.0,
        usage=CONTEXT_ONLY,
        usable=False,
        note="GAP: byte-identical to wsf_ferry_vessels, so no disruption "
        "bulletins are actually present. Ferry cancellations are invisible "
        "until a real bulletin feed is connected.",
    ),

    # -- H. construction and work zones -----------------------------------
    SignalSpec(
        source_id="wsdot.work_zones",
        category="construction",
        adapter="feed.wsdot_work_zones",
        label="WSDOT work zones and lane closures",
        files=("wsdot_work_zones.json",),
        endpoint=_WSDOT_WORK_ZONES,
        interval_s=900.0,
        stale_after_s=7200.0,
        reliability=0.93,
        note="68 work zones as LineStrings with lane-closure descriptions and "
        "compass direction.",
    ),

    # -- I. bridges and bottlenecks ---------------------------------------
    SignalSpec(
        source_id="wsdot.vertical_clearance",
        category="bridges",
        adapter="feed.wsdot_bridges",
        label="WSDOT bridge vertical clearance",
        files=("wsdot_bridges.json", "wsdot_bridge_restrictions.json", "wsdot_truck_restrictions.json"),
        interval_s=604800.0,
        stale_after_s=2592000.0,
        usage=CONTEXT_ONLY,
        reliability=0.93,
        note="100 structures with clearance in inches. Static reference: a "
        "truck exceeding clearance is a risk signal, not a live incident.",
        also_known_as=("wsdot_bridge_restrictions.json", "wsdot_truck_restrictions.json"),
    ),
    SignalSpec(
        source_id="wsdot.truck_restrictions",
        category="bridges",
        adapter="feed.wsdot_bridges",
        label="WSDOT truck restrictions",
        files=("wsdot_truck_restrictions.json",),
        interval_s=3600.0,
        stale_after_s=21600.0,
        reliability=0.0,
        usage=CONTEXT_ONLY,
        usable=False,
        note="GAP: byte-identical to the vertical-clearance payload; no weight "
        "or truck-specific restrictions are present.",
    ),

    # -- J. weather and emergency alerts ----------------------------------
    SignalSpec(
        source_id="nws.alerts",
        category="weather",
        adapter="feed.nws_alerts",
        label="NWS active weather alerts",
        files=("nws_active_alerts.json",),
        endpoint=_NWS_ALERTS,
        interval_s=120.0,
        stale_after_s=900.0,
        reliability=0.97,
        note="Nationwide feed. Records are filtered to the region bounding box "
        "before they become observations; an alert for another state never "
        "reaches the pipeline.",
    ),
    SignalSpec(
        source_id="nws.station_observations",
        category="weather",
        adapter="feed.nws_observations",
        label="NWS station observations (KSEA, KBFI)",
        files=("nws_ksea_observations.json", "nws_kbfi_observations.json"),
        interval_s=300.0,
        stale_after_s=1800.0,
        usage=CONTEXT_ONLY,
        reliability=0.95,
        note="Ground truth for weather conditions at Sea-Tac and Boeing Field.",
        also_known_as=("nws_kbfi_observations.json",),
    ),

    # -- K. earthquakes ---------------------------------------------------
    SignalSpec(
        source_id="usgs.earthquakes",
        category="seismic",
        adapter="feed.usgs_earthquakes",
        label="USGS earthquakes (24h, all magnitudes)",
        files=("usgs_earthquakes_24h.json", "usgs_earthquakes_day.geojson"),
        endpoint=_USGS_DAY,
        interval_s=300.0,
        stale_after_s=1800.0,
        reliability=0.96,
        note="Includes felt reports, PAGER alert level and tsunami flag.",
    ),
    SignalSpec(
        source_id="usgs.pnsn_regional",
        category="seismic",
        adapter="feed.usgs_earthquakes",
        label="USGS/PNSN Pacific Northwest regional network",
        files=("usgs_pnsn_regional.json",),
        interval_s=300.0,
        stale_after_s=1800.0,
        reliability=0.94,
        note="Regional picks; smaller events the global feed would miss.",
    ),

    # -- L. critical facilities -------------------------------------------
    SignalSpec(
        source_id="facilities.critical",
        category="facilities",
        adapter="feed.critical_facilities",
        label="Seattle critical facilities",
        files=("seattle_critical_facilities.geojson",),
        subdir="data/reference/infrastructure_gis",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage=CONTEXT_ONLY,
        reliability=0.9,
        note="15 facilities (trauma hospitals, fire, police, schools, civic "
        "sites). Reference geometry for exposure, not a status feed.",
    ),

    # -- M. population density and land use -------------------------------
    SignalSpec(
        source_id="population.land_use_density",
        category="population",
        adapter="feed.land_use_density",
        label="Seattle land use and population density",
        files=("seattle_land_use_density.json",),
        subdir="data/reference/infrastructure_gis",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage=CONTEXT_ONLY,
        reliability=0.8,
        note="5 neighbourhood polygons with density, land-use mix and a "
        "pedestrian-activity index used to weight crowd estimates.",
    ),

    # -- N. historical event records --------------------------------------
    # Everything below is `historical_only`: it may inform similarity,
    # baseline and crowd-size reasoning, and must never on its own raise a
    # realtime notification. That gate is enforced in
    # `analysis.relevance` and in `SourceRegistry.realtime()`, not here.
    SignalSpec(
        source_id="historical.protest_events",
        category="historical",
        adapter="historical.acled_regional",
        label="ACLED Washington State protest events",
        files=("washington_state_protest_events.csv",),
        subdir="data/regional/washington_focused",
        interval_s=604800.0,
        stale_after_s=2592000.0,
        usage="historical_only",
        reliability=0.85,
        note="10,462 events with arrests, chemical agents, injuries and crowd "
        "size. Feeds similarity and baseline comparison only (section 34). "
        "The closest analogue corpus to the product's actual domain: real "
        "Washington demonstrations with real recorded crowd sizes.",
    ),
    SignalSpec(
        source_id="historical.spd_unrest_crime",
        category="historical",
        adapter="historical.municipal_crime",
        label="Seattle Police 2020 unrest crime reports",
        files=("seattle_spd_crimes_2020_unrest.csv",),
        subdir="data/regional/washington_focused",
        interval_s=604800.0,
        stale_after_s=2592000.0,
        usage="historical_only",
        reliability=0.9,
        note="1,742 geocoded NIBRS reports from the 2020 unrest period; the "
        "empirical basis for what infrastructure damage follows. Columns are "
        "`latitude`/`longitude`, not the `lat`/`lon` the other cities use.",
    ),
    SignalSpec(
        source_id="historical.capitol_unrest_crime",
        category="historical",
        adapter="historical.municipal_crime",
        label="Metropolitan Police Department unrest and Capitol riot crime reports",
        files=(
            "dc_mpd_crimes_2020_unrest.csv",
            "dc_mpd_crimes_2021_capitol.csv",
        ),
        subdir="data/regional/washington_focused",
        interval_s=604800.0,
        stale_after_s=2592000.0,
        usage="historical_only",
        reliability=0.85,
        note="Two MPD exports, 2020 unrest and 2021 Capitol. Coordinates are "
        "upper-case `LATITUDE`/`LONGITUDE` - the only file in the corpus that "
        "spells them that way, so a reader keyed on the usual lowercase names "
        "places none of these rows. 2021 is a different year from every other "
        "municipal file here and is kept as a separate signal rather than "
        "merged into the 2020 comparison.",
    ),
    SignalSpec(
        source_id="historical.crowd_counting",
        category="historical",
        adapter="historical.crowd_counting",
        label="Crowd Counting Consortium compiled events",
        files=(
            "ccc_phase3_public_2025_present.csv",
            "ccc_compiled_20212024.tab",
            "ccc_compiled_20172020.tab",
        ),
        subdir="data/research/crowd_counting_consortium",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.8,
        note="The best crowd-size corpus available: three releases with a "
        "numeric `size_mean`/`size_low`/`size_high` band per event, plus "
        "arrests, injuries, property damage and chemical-agent use. Note the "
        "two dialects - the 2021-2025 files are `.tab`, the phase-3 file is "
        "`.csv` with a renamed column set.",
    ),
    SignalSpec(
        source_id="historical.acled_count_cube",
        category="historical",
        adapter="historical.acled_count_cube",
        label="ACLED demonstration event count cubes",
        files=("demonstration_events.xlsx",),
        subdir="data/research/acled_hdx",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.7,
        note="A 40 MB workbook whose sheets expand to ~500 MB of sheet XML. "
        "It contains **no event records at all** - only pre-aggregated "
        "`Country/.../Month/Year/Events` count cubes, behind a licensing "
        "notice on sheet 1. Read streaming and only as counts; reading it as "
        "events would invent a million demonstrations that do not exist.",
    ),
    SignalSpec(
        source_id="historical.mass_mobilization",
        category="historical",
        adapter="historical.mida",
        label="MIDA mass mobilization events",
        files=("mmALL_073120_csv.tab",),
        subdir="data/research/mass_mobilization",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.6,
        note="Country-level protest events 1990-2020 with participant bands "
        "and state responses. **No coordinates**: `location` is 'national', "
        "'capital' or a region name, so these are usable as a global baseline "
        "rate and never as a place.",
    ),
    SignalSpec(
        source_id="historical.navco_campaigns",
        category="historical",
        adapter="historical.navco",
        label="NAVCO 2.1 mobilization campaigns",
        files=("NAVCO2-1_ForPublication.tab",),
        subdir="data/research/navco_campaigns",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.65,
        note="102 columns on campaign goals, tactics, duration, size bands "
        "and repression. `navco3-0full.xlsx` in the same directory is **not** "
        "NAVCO data - it is GDELT/CAMEO codebook material - so it is not "
        "wired as a signal. Country-level only; no coordinates.",
    ),
    SignalSpec(
        source_id="historical.ucdp_conflict",
        category="historical",
        adapter="historical.ucdp",
        label="UCDP/PRIO GED conflict events",
        files=("GEDEvent_v24_1.csv",),
        subdir="data/research/ucdp_conflict_events",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.9,
        note="255 MB, ~260k georeferenced conflict events with fatalities by "
        "party. `ged241-csv.zip` is the same data compressed; only one is "
        "read. Worldwide, so the region filter normally keeps very few - which "
        "is correct: conflict events elsewhere are not Seattle events.",
    ),
    SignalSpec(
        source_id="historical.gdelt_news",
        category="historical",
        adapter="historical.gdelt",
        label="GDELT news event exports",
        files=(
            "20200601.export.CSV",
            "20200531.export.CSV",
            "20200530.export.CSV",
        ),
        subdir="data/research/gdelt_events",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.4,
        note="Tab-delimited with a `.CSV` extension and **no header row** - "
        "the first data row is what a naive reader mistakes for one. Used as "
        "a news-volume-per-location signal around 2020-05-30..06-01, the week "
        "of the George Floyd protests. Column indices are verified against the "
        "file at read time rather than assumed from a schema version.",
    ),
    SignalSpec(
        source_id="historical.google_mobility",
        category="historical",
        adapter="historical.mobility",
        label="Google Community Mobility daily change",
        files=(
            "Google_Mobility_City_Daily.csv",
            "Google_Mobility_County_Daily.csv",
            "Google_Mobility_State_Daily.csv",
        ),
        subdir="data/research/mobility_and_traffic",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.75,
        note="Percentage change in visits since the pre-pandemic baseline - the "
        "cleanest available proxy for real movement in a place. `cityid` is "
        "resolved to coordinates through `GeoIDs_City.csv`; `countyfips` and "
        "state rows carry no geometry of their own. A `.csv.gz` twin of the "
        "county file is byte-equivalent and is not read twice.",
    ),
    SignalSpec(
        source_id="historical.affinity_spend",
        category="historical",
        adapter="historical.affinity",
        label="Affinity consumer spending by city",
        files=("Affinity_City_Daily.csv",),
        subdir="data/research/economic_opportunity_insights",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.6,
        note="Card-spending change by city and category. Missing values are a "
        "literal `.` rather than empty, so a naive float() either crashes or, "
        "worse, is coerced to zero and reads as a collapse in spending. City "
        "coordinates come from `GeoIDs_City.csv`.",
    ),
    SignalSpec(
        source_id="historical.employment",
        category="historical",
        adapter="historical.employment",
        label="County weekly employment change",
        files=("Employment_County_Weekly_2020.csv",),
        subdir="data/research/economic_opportunity_insights",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.7,
        note="Weekly employment change by county FIPS with sector splits. "
        "County FIPS is kept as an identifier, not resolved to a point: a "
        "county is an area, and pretending otherwise would place a King County "
        "figure at a single arbitrary address.",
    ),
    SignalSpec(
        source_id="historical.womply_merchants",
        category="historical",
        adapter="historical.womply",
        label="Womply small-business activity by county",
        files=("Womply_County_Weekly.csv",),
        subdir="data/research/economic_opportunity_insights",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.65,
        note="Weekly merchant and revenue change by county - a proxy for "
        "small-business exposure that complements the spending data.",
    ),
    SignalSpec(
        source_id="historical.municipal_crime",
        category="historical",
        adapter="historical.municipal_crime",
        label="Municipal 2020 unrest crime and 311 damage reports",
        files=(
            "chicago_311_infrastructure_damage_2020.csv",
            "nyc_311_infrastructure_damage_2020.csv",
            "los_angeles_civil_unrest_crimes_2020.csv",
            "chicago_civil_unrest_crimes_2020.csv",
            "nyc_civil_unrest_crimes_2020.csv",
            "san_francisco_civil_unrest_crimes_2020.csv",
        ),
        subdir="data/research/municipal_crime_and_damage",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.8,
        note="Six cities, seven schemas, none of which agree on column names: "
        "Chicago/NYC/Seattle use `latitude`/`longitude`, LAPD uses `lat`/`lon`, "
        "and every city's offence vocabulary is its own. The adapter keys the "
        "column map off the filename, so each file is read in its own dialect; "
        "a shared fallback silently produces empty fields rather than an error.",
    ),
    SignalSpec(
        source_id="historical.fatal_shootings",
        category="historical",
        adapter="historical.fatal_shootings",
        label="Fatal police shootings",
        files=("fatal-police-shootings-data.csv",),
        subdir="data/research/violence_and_fatalities",
        interval_s=2592000.0,
        stale_after_s=7776000.0,
        usage="historical_only",
        reliability=0.85,
        note="Georeferenced fatal police shootings with `location_precision` "
        "and `race_source` quality flags. Included as a *baseline of a rare, "
        "high-consequence event type* - never as a predictor of an individual "
        "and never as a per-neighbourhood risk score, which the data's "
        "geography and size cannot support.",
    ),

    # -- O. user context (internal, never a source adapter) ---------------
    SignalSpec(
        source_id="user.context",
        category="user_context",
        adapter="internal.user_context",
        label="User location, destination, saved places, normal routes",
        files=(),
        endpoint="internal://user_context",
        interval_s=0.0,
        stale_after_s=0.0,
        usage=CONTEXT_ONLY,
        reliability=1.0,
        note="Supplied by the authenticated user, never polled. Listed here so "
        "the catalogue covers the full signal inventory the spec describes.",
    ),
)


SIGNALS_BY_ID: dict[str, SignalSpec] = {s.source_id: s for s in SIGNALS}


def signals_by_category(category: str) -> list[SignalSpec]:
    return [s for s in SIGNALS if s.category == category]


def usable_signals() -> list[SignalSpec]:
    return [s for s in SIGNALS if s.usable]


def realtime_signals() -> list[SignalSpec]:
    return [s for s in SIGNALS if s.usable and s.usage == "realtime"]


def categories() -> list[str]:
    seen: list[str] = []
    for s in SIGNALS:
        if s.category not in seen:
            seen.append(s.category)
    return seen


def coverage() -> dict[str, dict[str, int]]:
    """Per-category usable/total counts, for ``infraimpact feeds --coverage``."""

    out: dict[str, dict[str, int]] = {}
    for s in SIGNALS:
        row = out.setdefault(s.category, {"total": 0, "usable": 0, "snapshots": 0})
        row["total"] += 1
        row["usable"] += int(s.usable)
        row["snapshots"] += int(bool(s.files))
    return out


__all__ = [
    "CONTEXT_ONLY",
    "SIGNALS",
    "SIGNALS_BY_ID",
    "SignalSpec",
    "categories",
    "coverage",
    "realtime_signals",
    "signals_by_category",
    "usable_signals",
]

"""Source registry (brief section 4).

The region profile selects sources; nothing else in the system references a
concrete connector. Adding a signal means adding a row to
:mod:`infraimpact.sources.catalog` and registering its adapter here - no changes
to event logic, and no other module needs to know which agencies exist.

Two families of adapter live side by side
-----------------------------------------
``feed.*``   Snapshot adapters that read the captured payloads in
             ``live_feeds/`` and ``data/``. This is the default, because it is
             reproducible and needs no credentials.

``<agency>.*``  Live HTTP connectors from :mod:`infraimpact.sources.transport`,
             :mod:`infraimpact.sources.nws` and :mod:`infraimpact.sources.usgs`.
             Used when ``INFRAIMPACT_ENABLE_NETWORK=1``.

They are alternatives for the same signals, not additional ones. Only one of
each is ever active, selected by :attr:`Settings.enable_network_sources`, so a
signal can never be double-counted from two retrieval paths.

The synthetic scenario generator is available but *off* unless
``INFRAIMPACT_ENABLE_SYNTHETIC=1``. A system whose product is evidence-backed
impact must not quietly mix fabricated events into a real ledger.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..config import RegionProfile, Settings, get_region, get_settings
from ..domain.schemas import SourceHealth
from .adapter import SourceAdapter, unavailable_reason
from .catalog import SIGNALS, SignalSpec
from .feeds import (
    CriticalFacilitiesAdapter,
    DrawbridgeAdapter,
    EventPermitsAdapter,
    FerryVesselAdapter,
    GtfsStaticAdapter,
    LandUseDensityAdapter,
    NewsFeedAdapter,
    NwsAlertsSnapshotAdapter,
    NwsObservationAdapter,
    PoliceBlotterAdapter,
    SfdDispatchAdapter,
    SpdCadAdapter,
    StreetUseAdapter,
    TrafficCameraAdapter,
    TransitAlertAdapter,
    TransitVehicleAdapter,
    UsgsSnapshotAdapter,
    WsdotAlertsAdapter,
    WsdotBridgeAdapter,
    WsdotPassesAdapter,
    WsdotWorkZoneAdapter,
)
from .historical import (
    AcledCountCubeAdapter,
    AcledRegionalAdapter,
    AffinityAdapter,
    CrowdCountingAdapter,
    EmploymentAdapter,
    FatalPoliceShootingsAdapter,
    GdeltAdapter,
    GoogleMobilityAdapter,
    MidaAdapter,
    MunicipalCrimeAdapter,
    NavcoAdapter,
    UcdpAdapter,
    WomplyAdapter,
)  # noqa: F401 - historical adapters are registered above
from .nws import NwsAlertsAdapter
from .synthetic import SyntheticPugetSoundAdapter
from .transport import (
    MetroGtfsRtAdapter,
    SdotRowImpactsAdapter,
    WsdotHighwayAlertsAdapter,
)
from .usgs import UsgsEarthquakeAdapter

log = logging.getLogger(__name__)

#: Every adapter key the catalogue and the region profiles may reference.
ADAPTERS: dict[str, type[SourceAdapter]] = {
    # -- snapshot adapters, one class per catalogue entry -------------------
    "feed.spd_cad": SpdCadAdapter,
    "feed.news": NewsFeedAdapter,
    "feed.blotter": PoliceBlotterAdapter,
    "feed.sfd_dispatch": SfdDispatchAdapter,
    "feed.event_permits": EventPermitsAdapter,
    "feed.street_use": StreetUseAdapter,
    "feed.wsdot_alerts": WsdotAlertsAdapter,
    "feed.traffic_cameras": TrafficCameraAdapter,
    "feed.drawbridges": DrawbridgeAdapter,
    "feed.wsdot_passes": WsdotPassesAdapter,
    "feed.wsdot_bridges": WsdotBridgeAdapter,
    "feed.wsdot_work_zones": WsdotWorkZoneAdapter,
    "feed.transit_vehicles": TransitVehicleAdapter,
    "feed.transit_alerts": TransitAlertAdapter,
    "feed.gtfs_static": GtfsStaticAdapter,
    "feed.ferry_vessels": FerryVesselAdapter,
    "feed.nws_alerts": NwsAlertsSnapshotAdapter,
    "feed.nws_observations": NwsObservationAdapter,
    "feed.usgs_earthquakes": UsgsSnapshotAdapter,
    "feed.critical_facilities": CriticalFacilitiesAdapter,
    "feed.land_use_density": LandUseDensityAdapter,
    # -- historical and research corpora (historical_only) -----------------
    "historical.acled_regional": AcledRegionalAdapter,
    "historical.acled_count_cube": AcledCountCubeAdapter,
    "historical.crowd_counting": CrowdCountingAdapter,
    "historical.mida": MidaAdapter,
    "historical.navco": NavcoAdapter,
    "historical.ucdp": UcdpAdapter,
    "historical.gdelt": GdeltAdapter,
    "historical.mobility": GoogleMobilityAdapter,
    "historical.affinity": AffinityAdapter,
    "historical.employment": EmploymentAdapter,
    "historical.womply": WomplyAdapter,
    "historical.municipal_crime": MunicipalCrimeAdapter,
    "historical.fatal_shootings": FatalPoliceShootingsAdapter,
    # -- live HTTP connectors (network mode) -------------------------------
    "synthetic.puget_sound": SyntheticPugetSoundAdapter,
    "usgs.earthquakes": UsgsEarthquakeAdapter,
    "nws.alerts": NwsAlertsAdapter,
    "wsdot.highway_alerts": WsdotHighwayAlertsAdapter,
    "sdot.row_impacts": SdotRowImpactsAdapter,
    "metro.gtfs_rt": MetroGtfsRtAdapter,
}


#: Signals the platform never polls. ``user.context`` is supplied by the
#: authenticated user through the API (section 59: location is user-supplied and
#: ephemeral), so there is no adapter to build and no endpoint to call - it is in
#: the catalogue so the inventory the brief describes is complete, not because
#: something reads a file for it.
NOT_POLLED = frozenset({"internal.user_context"})


def build_catalogue_adapters(
    *,
    only: Iterable[str] | None = None,
    include_historical: bool = False,
    include_unusable: bool = False,
    include_unpolled: bool = False,
) -> list[SourceAdapter]:
    """Instantiate one adapter per catalogue signal.

    ``usable=False`` signals are skipped by default. They stay in the catalogue
    so the gap is visible and the live endpoint can be switched on the day the
    payload starts matching its name; emitting them anyway would produce
    duplicate camera sites under a second source id and claim coverage the
    data does not contain.

    Signals in :data:`NOT_POLLED` are skipped unconditionally unless
    ``include_unpolled`` is set. They document an input rather than describe a
    feed, and warning about their absent adapters on every startup would be
    noise that trains an operator to ignore this warning entirely - which is
    exactly when a genuinely unknown adapter name would slip through.
    """

    wanted = set(only) if only else None
    out: list[SourceAdapter] = []
    for spec in SIGNALS:
        if wanted is not None and spec.source_id not in wanted:
            continue
        if not spec.usable and not include_unusable:
            continue
        if spec.usage == "historical_only" and not include_historical:
            continue
        if spec.adapter in NOT_POLLED and not include_unpolled:
            continue
        cls = ADAPTERS.get(spec.adapter)
        if cls is None:
            log.warning("catalogue signal %s names unknown adapter %r", spec.source_id, spec.adapter)
            continue
        out.append(cls(spec))
    return out


def catalogue_adapters_for(specs: Iterable[SignalSpec]) -> list[SourceAdapter]:
    """Instantiate adapters for an explicit list of catalogue signals."""

    out: list[SourceAdapter] = []
    for spec in specs:
        cls = ADAPTERS.get(spec.adapter)
        if cls is not None:
            out.append(cls(spec))
    return out


class SourceRegistry:
    """Owns adapter lifecycle and health for one region.

    Exactly one retrieval path per signal: snapshots by default, live HTTP when
    ``INFRAIMPACT_ENABLE_NETWORK=1``. Historical corpora are attached only when
    ``include_historical=True``, because they feed similarity and baseline work
    and must never drive a real-time notification.
    """

    def __init__(
        self,
        region: RegionProfile | None = None,
        settings: Settings | None = None,
        only: Iterable[str] | None = None,
        *,
        include_historical: bool = False,
        include_synthetic: bool = False,
        use_catalogue: bool | None = None,
    ) -> None:
        self.region = region or get_region()
        self.settings = settings or get_settings()
        self._adapters: dict[str, SourceAdapter] = {}
        wanted = set(only) if only else None

        # Snapshots are the default retrieval path. Turning the network on
        # swaps in the live connectors for the signals that have one.
        self.use_catalogue = (
            not self.settings.enable_network_sources if use_catalogue is None else use_catalogue
        )

        if self.use_catalogue:
            for adapter in build_catalogue_adapters(
                only=wanted, include_historical=include_historical
            ):
                if wanted and adapter.source_id not in wanted:
                    continue
                self._register(adapter)
        else:
            for name in self.region.source_adapters:
                if wanted and name not in wanted:
                    continue
                self._register_named(name)

        if include_synthetic or self.settings.enable_synthetic_sources:
            for name in self.region.synthetic_sources:
                if wanted and name not in wanted:
                    continue
                self._register_named(name)

    def _register(self, adapter: SourceAdapter) -> None:
        if not adapter.source_id:
            log.warning("adapter %s has no source_id; skipping", type(adapter).__name__)
            return
        self._adapters[adapter.source_id] = adapter

    def _register_named(self, name: str) -> None:
        spec = self.region.source_specs.get(name)
        if spec and not spec.enabled:
            log.info("source %s disabled by region profile", name)
            return
        cls = ADAPTERS.get(name)
        if cls is None:
            log.warning("no adapter registered for %r", name)
            return
        self._register(cls())

    @property
    def adapters(self) -> list[SourceAdapter]:
        return list(self._adapters.values())

    def get(self, source_id: str) -> SourceAdapter | None:
        return self._adapters.get(source_id)

    def runnable(self) -> list[SourceAdapter]:
        out = []
        for adapter in self._adapters.values():
            reason = unavailable_reason(adapter)
            if reason:
                log.debug("skipping %s: %s", adapter.source_id, reason)
                continue
            out.append(adapter)
        return out

    def realtime(self) -> list[SourceAdapter]:
        """Runnable adapters allowed to raise a notification (section 33)."""

        return [a for a in self.runnable() if a.usage == "realtime"]

    def health(self) -> list[SourceHealth]:
        return [a.health() for a in self._adapters.values()]

    def __len__(self) -> int:
        return len(self._adapters)


__all__ = ["ADAPTERS", "SourceRegistry", "build_catalogue_adapters", "catalogue_adapters_for"]
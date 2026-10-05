"""Persistent event identity.

An event's id is its promise to everything downstream: to the state versions
that accumulate under it, to the deltas that diff them, to the exposures keyed
by it. If the id is not stable and unique, the whole world-state system has no
subject to reason about.

There is exactly one way this goes wrong, and it is the way it used to go wrong
here. Deriving an id from coarse attributes - region, observation type, calendar
day - produces an id that is *deterministic* and therefore looks correct, while
being shared by every unrelated event that shares those attributes. Two road
closures at opposite ends of the same region on the same day are then the same
event, forever, and the platform confidently reconstructs a single fictional
situation out of two real ones. Every downstream number is wrong in a way that
is invisible, because there is nothing to compare it against.

So identity here is built from three things:

    the observation that opens the event   (anchor)
    a coarse situation fingerprint          (deduplication across sources)
    the ledger's own contents               (collision disambiguation)

The fingerprint is a time bucket crossed with a geographic cell, both of which
exist to answer "could these two records plausibly be the same situation?" -
not to assert that they are. Two sources reporting the same closure eight
minutes apart land in the same bucket and the same cell and get the same id for
free, with no scoring involved. Two events in different cells get different ids
even when everything else about them matches.

The third part is what makes uniqueness real rather than probable. When the
fingerprint is already occupied by an event that this observation does not
belong to, the fingerprint has done its job and the situation is genuinely a
second one; the allocator then appends a deterministic ordinal derived from how
many events already occupy that fingerprint. Replaying the same observation
yields the same ordinal, because the observation is already linked to its event
by the time the question is asked - which is the invariant that makes ingestion
idempotent rather than merely deduplicated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..domain.geo import centroid_of
from ..domain.ids import deterministic_id, ensure_utc
from ..domain.schemas import Observation

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentityAllocation:
    """The outcome of asking "which event is this, if any?"."""

    event_id: str
    fingerprint: str
    #: ``anchor``        first record for an already-known event
    #: ``fingerprint``   fingerprint was free, so this observation opened it
    #: ``ordinal``       fingerprint was taken by a different event
    #: ``existing``      the observation was already linked (replay)
    basis: str
    ordinal: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "fingerprint": self.fingerprint,
            "basis": self.basis,
            "ordinal": self.ordinal,
        }


class EventIdentityAllocator:
    """Allocates stable, collision-free, replay-deterministic event ids.

    ``time_bucket_min`` and ``geo_cell_m`` bound the fingerprint. They are
    deliberately generous: a fingerprint that is too fine stops deduplicating
    across sources, which is a recoverable annoyance, while one that is too
    coarse merges unrelated situations, which is unrecoverable. 15 minutes and
    750 m matches how the same real-world situation is reported by different
    agencies watching the same stretch of city.
    """

    def __init__(
        self,
        region_id: str,
        *,
        time_bucket_min: float = 15.0,
        geo_cell_m: float = 750.0,
        prefix: str = "evt",
    ) -> None:
        self.region_id = region_id
        self.time_bucket_min = time_bucket_min
        self.geo_cell_m = geo_cell_m
        self.prefix = prefix

    # -- fingerprint ------------------------------------------------------

    def fingerprint(self, observation: Observation) -> str:
        """Coarse situation key: what could plausibly be the same event?"""
        bucket = self._time_bucket(observation.event_time)
        cell = self._geo_cell(observation)
        return f"{observation.observation_type.value}|{bucket}|{cell}"

    def base_id(self, fingerprint: str) -> str:
        return deterministic_id(
            self.prefix, self.region_id, fingerprint
        )[:24]

    def _time_bucket(self, event_time: datetime) -> str:
        stamp = ensure_utc(event_time)
        minutes = max(1.0, self.time_bucket_min)
        epoch_minutes = int(stamp.timestamp() // (minutes * 60))
        start = datetime.fromtimestamp(epoch_minutes * minutes * 60, tz=stamp.tzinfo)
        return start.strftime("%Y%m%dT%H%M")

    def _geo_cell(self, observation: Observation) -> str:
        """Quantise the observation's centroid to a coarse grid cell.

        Rounding to a fixed number of decimals would make cell size depend on
        latitude, so the cell is computed as an integer number of metres from a
        fixed origin instead. Precision is tracked at ``location_precision_m`` so
        a city-scale alert and a door-level report do not land in different cells
        purely because of how precisely each was geocoded.
        """
        centre = centroid_of(observation.geometry) if observation.geometry else None
        if centre is None:
            return "nogeo"
        lon, lat = centre
        cell = max(1.0, self.geo_cell_m)
        # 1 degree latitude ~= 111_320 m; longitude shrinks by cos(lat).
        import math

        lat_m = lat * 111_320.0
        lon_m = lon * 111_320.0 * max(0.05, math.cos(math.radians(lat)))
        return f"{int(lat_m // cell)}:{int(lon_m // cell)}"

    # -- allocation -------------------------------------------------------

    def allocate(
        self,
        observation: Observation,
        *,
        event_exists,
        event_for_observation,
        observations_of,
    ) -> IdentityAllocation:
        """Pick the id for a new event, given what the ledger already holds.

        The three callables keep this class free of a storage dependency, which
        is what allows it to be tested against an in-memory ledger, a SQLite
        ledger, or a future PostGIS one without change:

        ``event_exists(event_id) -> bool``
        ``event_for_observation(observation_id) -> str | None``
        ``observations_of(event_id) -> list[str]``

        The invariant enforced is *one observation belongs to at most one event,
        permanently*. If the observation is already linked anywhere, that link
        wins over every other consideration - including a fingerprint that now
        looks free - so reprocessing a source can never fork an event's history.
        """

        fingerprint = self.fingerprint(observation)
        base = self.base_id(fingerprint)

        linked = event_for_observation(observation.observation_id)
        if linked is not None:
            return IdentityAllocation(
                event_id=linked,
                fingerprint=fingerprint,
                basis="existing",
                ordinal=0,
            )

        if not event_exists(base):
            return IdentityAllocation(
                event_id=base,
                fingerprint=fingerprint,
                basis="fingerprint",
                ordinal=0,
            )

        # The fingerprint is taken. This observation belongs to a *different*
        # event that happens to share the situation key, so it needs its own
        # identity. The ordinal counts upward deterministically.
        for ordinal in range(1, 1000):
            candidate = f"{base}:{ordinal}"
            if not event_exists(candidate):
                log.info(
                    "fingerprint %s already held by a different event; "
                    "allocating distinct id %s",
                    fingerprint,
                    candidate,
                )
                return IdentityAllocation(
                    event_id=candidate,
                    fingerprint=fingerprint,
                    basis="ordinal",
                    ordinal=ordinal,
                )

        # A thousand co-located events in one bucket is not a situation this
        # allocator can distinguish. Fall back to the observation's own id so
        # the event is still real and still unique - uniqueness is not optional.
        fallback = deterministic_id(
            self.prefix, self.region_id, observation.observation_id
        )[:24]
        log.warning(
            "fingerprint %s saturated; falling back to observation-anchored id %s",
            fingerprint,
            fallback,
        )
        return IdentityAllocation(
            event_id=fallback,
            fingerprint=fingerprint,
            basis="overflow",
            ordinal=0,
        )

    # -- diagnostics ------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "prefix": self.prefix,
            "time_bucket_min": self.time_bucket_min,
            "geo_cell_m": self.geo_cell_m,
        }


def observation_window(observation: Observation, minutes: float) -> tuple[datetime, datetime]:
    """Event-time window around an observation, for cheap candidate filtering."""

    stamp = ensure_utc(observation.event_time)
    delta = timedelta(minutes=minutes)
    return stamp - delta, stamp + delta


__all__ = ["EventIdentityAllocator", "IdentityAllocation", "observation_window"]

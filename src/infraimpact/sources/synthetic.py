"""Synthetic Puget Sound scenario generator.

Purpose: make the whole pipeline runnable and testable with no network and no
API keys, while exercising every stage - event resolution, claim extraction,
state deltas, infrastructure exposure, and notification gating.

The script deliberately includes low-value updates ("demonstration continues")
that should produce *no* material delta. That is the negative case which proves
the change engine and the notification suppressor actually work (section 33).

This adapter is internal/derived, never treated as authoritative.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from ..domain.enums import Authority, ObservationType, SourceType
from ..domain.geo import line_string, point, polygon
from ..domain.ids import utcnow
from .adapter import RawRecord, SourceAdapter

# Downtown Seattle reference points.
WESTLAKE = (-122.3370, 47.6162)
PIKE_PLACE = (-122.3420, 47.6090)
FOURTH_AND_UNION = (-122.3370, 47.6090)
THIRD_AND_PIKE = (-122.3385, 47.6080)
UNIVERSITY_ST = (-122.3130, 47.6100)
CAPITOL_HILL = (-122.3190, 47.6220)


def _point_record(rec_id: str, lonlat: tuple[float, float], when: datetime, headline: str, payload: dict[str, Any]) -> RawRecord:
    return RawRecord(
        source_record_id=rec_id,
        payload=payload,
        event_time=when,
        observed_at=when,
        source_url=f"synthetic://puget-sound/{rec_id}",
    )


class SyntheticPugetSoundAdapter(SourceAdapter):
    """Emits a scripted multi-stage demonstration + infrastructure cascade."""

    source_id = "synthetic.puget_sound"
    source_type = SourceType.INTERNAL_DERIVED
    authority = Authority.INTERNAL
    reliability = 0.7
    expected_interval_s = 2.0
    stale_after_s = 15.0
    connector_version = "0.1.0"

    #: (seconds after previous step, record factory name, kwargs)
    def _script(self) -> list[tuple[float, str, dict[str, Any]]]:
        return [
            (0.0, "permit", {}),
            (1.0, "police_gathering", {}),
            (2.0, "news_moving_north", {}),
            (3.0, "sdot_closure_4th", {}),
            (4.0, "metro_suspends_40", {}),
            (5.0, "noise_continues", {}),
            (6.0, "sdot_closure_3rd", {}),
            (7.0, "fire_dispatch", {}),
            (8.0, "police_expansion", {}),
            (9.0, "power_outage", {}),
            (10.0, "noise_continues", {}),
            (11.0, "news_march_route", {}),
            (12.0, "sdot_reopen_4th", {}),
            (13.0, "metro_resumes_40", {}),
            (14.0, "noise_continues", {}),
            (15.0, "police_dispersed", {}),
        ]

    def __init__(self) -> None:
        self._cursor = 0
        self._started = utcnow()
        self._cycle = 0

    # -- record factories -------------------------------------------------

    def _permit(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"permit-westlake-{self._cycle}",
            payload={
                "event": "Special event permit",
                "location": "Westlake Park, Seattle",
                "route": "Westlake Park N -> 4th Ave -> Pike St",
                "start": t.isoformat(),
                "expected_duration_min": 180,
                "permitted_activity": "march/rally",
                "organizer": "synthetic organizer",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://seattle/special-events",
        )

    def _police_gathering(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"spd-gathering-{self._cycle}",
            payload={
                "beat": "B12",
                "call_type": "large gathering",
                "detail": "Officers responding to reported gathering near Westlake Park.",
                "units": 2,
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://spd/calls",
        )

    def _news_moving_north(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"news-moving-{self._cycle}",
            payload={
                "headline": "Demonstrators move north along Fourth Avenue",
                "body": (
                    "Participants of a permitted demonstration left Westlake Park and are "
                    "moving north along Fourth Avenue. Police are present but the gathering "
                    "is described as peaceful. Organisers said the march will continue toward "
                    "Pike Street."
                ),
                "outlet": "synthetic regional news",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://news/moving-north",
        )

    def _sdot_closure_4th(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"sdot-closure-4th-{self._cycle}",
            payload={
                "street": "4th Ave",
                "from": "Union St",
                "to": "Pike St",
                "closure_type": "full",
                "reason": "special event",
                "expected_reopen": (t + timedelta(hours=2)).isoformat(),
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://sdot/row-impacts",
        )

    def _metro_suspends_40(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"metro-alert-40-{self._cycle}",
            payload={
                "route": "40",
                "route_long": "UW-HTC/Seattle Center-Downtown Seattle-Ballard",
                "alert_type": "detour",
                "header": "Route 40 suspended downtown",
                "description": "Route 40 is suspended between 3rd Ave and Union St due to a special event.",
                "severity": "severe",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://metro/service-alerts",
        )

    def _noise(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"news-noise-{self._cycle}-{int(t.timestamp())}",
            payload={
                "headline": "Demonstration continues downtown",
                "body": "The downtown demonstration continues with no new developments reported.",
                "outlet": "synthetic regional news",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://news/noise",
        )

    def _sdot_closure_3rd(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"sdot-closure-3rd-{self._cycle}",
            payload={
                "street": "3rd Ave",
                "from": "Pike St",
                "to": "Seneca St",
                "closure_type": "full",
                "reason": "special event",
                "expected_reopen": (t + timedelta(hours=2)).isoformat(),
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://sdot/row-impacts",
        )

    def _fire_dispatch(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"sff-call-{self._cycle}",
            payload={
                "incident_type": "aid response",
                "detail": "EMS responding to a medical assistance call in the demonstration area.",
                "location": "Pike St and 4th Ave",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://sff/dispatch",
        )

    def _police_expansion(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"spd-expansion-{self._cycle}",
            payload={
                "beat": "B12",
                "call_type": "crowd control",
                "detail": "Gathering has expanded to include University Street and 3rd Avenue.",
                "units": 6,
                "estimated_crowd": 900,
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://spd/calls",
        )

    def _power_outage(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"scl-outage-{self._cycle}",
            payload={
                "outage_type": "planned",
                "customers_affected": 120,
                "area": "Pioneer Square",
                "cause": "planned equipment maintenance",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://seattle-city-light/outages",
        )

    def _news_march_route(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"news-route-{self._cycle}",
            payload={
                "headline": "Organisers publish remaining march route",
                "body": (
                    "Organisers published the remaining march route: 4th Ave north to Pine St, "
                    "west to 5th Ave, then to Seattle Center."
                ),
                "outlet": "synthetic regional news",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://news/route",
        )

    def _sdot_reopen(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"sdot-reopen-4th-{self._cycle}",
            payload={
                "street": "4th Ave",
                "from": "Union St",
                "to": "Pike St",
                "closure_type": "reopened",
                "reason": "special event",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://sdot/row-impacts",
        )

    def _metro_resumes(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"metro-resume-40-{self._cycle}",
            payload={
                "route": "40",
                "alert_type": "service_restored",
                "header": "Route 40 service restored",
                "description": "Route 40 has resumed normal routing downtown.",
                "severity": "info",
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://metro/service-alerts",
        )

    def _police_dispersed(self, t: datetime) -> RawRecord:
        return RawRecord(
            source_record_id=f"spd-dispersed-{self._cycle}",
            payload={
                "beat": "B12",
                "call_type": "crowd dispersal",
                "detail": "Demonstration has dispersed. Roads reopening.",
                "units": 4,
            },
            event_time=t,
            observed_at=t,
            source_url="synthetic://spd/calls",
        )

    _FACTORIES = {
        "permit": "_permit",
        "police_gathering": "_police_gathering",
        "news_moving_north": "_news_moving_north",
        "sdot_closure_4th": "_sdot_closure_4th",
        "metro_suspends_40": "_metro_suspends_40",
        "noise_continues": "_noise",
        "sdot_closure_3rd": "_sdot_closure_3rd",
        "fire_dispatch": "_fire_dispatch",
        "police_expansion": "_police_expansion",
        "power_outage": "_power_outage",
        "news_march_route": "_news_march_route",
        "sdot_reopen_4th": "_sdot_reopen",
        "metro_resumes_40": "_metro_resumes",
        "police_dispersed": "_police_dispersed",
    }

    # -- adapter protocol -------------------------------------------------

    def poll(self, checkpoint: str | None) -> Iterable[RawRecord]:
        script = self._script()
        due: list[RawRecord] = []
        now = utcnow()
        elapsed = (now - self._started).total_seconds()

        while self._cursor < len(script):
            delay, name, _ = script[self._cursor]
            if elapsed < delay:
                break
            t = self._started + timedelta(seconds=delay)
            factory = getattr(self, self._FACTORIES[name])
            due.append(factory(t))
            self._cursor += 1

        if self._cursor >= len(script):
            # Restart the scenario so a long-running process keeps demonstrating
            # the full lifecycle instead of going silent.
            self._cycle += 1
            self._cursor = 0
            self._started = now

        return due

    def normalize(self, record: RawRecord) -> Any:
        from ..domain.schemas import Observation

        p = record.payload if isinstance(record.payload, dict) else {}
        kind = record.source_record_id.split("-")[0]

        if kind == "permit":
            return self.build_observation(
                record,
                observation_type=ObservationType.PERMIT_EVENT,
                geometry=polygon(
                    [
                        [-122.3395, 47.6145],
                        [-122.3345, 47.6145],
                        [-122.3345, 47.6180],
                        [-122.3395, 47.6180],
                        [-122.3395, 47.6145],
                    ]
                ),
                headline="Permitted march, Westlake Park",
                location_precision_m=15.0,
                authority=Authority.OFFICIAL,
            )

        if kind in {"spd", "sff"}:
            return self.build_observation(
                record,
                observation_type=(
                    ObservationType.POLICE_RESPONSE
                    if kind == "spd"
                    else ObservationType.FIRE_DISPATCH
                ),
                geometry=point(*WESTLAKE),
                headline=str(p.get("detail", ""))[:200],
                location_precision_m=600.0,  # beat-level, matching real SPD data
                authority=Authority.OFFICIAL,
            )

        if kind == "news":
            movement = "north" in p.get("headline", "").lower()
            return self.build_observation(
                record,
                observation_type=ObservationType.NEWS_ARTICLE,
                geometry=point(*PIKE_PLACE) if movement else point(*WESTLAKE),
                headline=str(p.get("headline", ""))[:200],
                location_precision_m=800.0,
                authority=Authority.ESTABLISHED_MEDIA,
                reliability=0.7,
            )

        if kind == "sdot":
            reopened = p.get("closure_type") == "reopened"
            return self.build_observation(
                record,
                observation_type=ObservationType.ROAD_CLOSURE,
                geometry=line_string([list(FOURTH_AND_UNION), [-122.3370, 47.6130], [-122.3370, 47.6075]]),
                headline=("Reopened: " if reopened else "Closed: ") + str(p.get("street", "")),
                location_precision_m=10.0,
                authority=Authority.OFFICIAL,
            )

        if kind == "metro":
            restored = p.get("alert_type") == "service_restored"
            return self.build_observation(
                record,
                observation_type=ObservationType.TRANSIT_SERVICE_ALERT,
                geometry=line_string([list(UNIVERSITY_ST), [-122.3250, 47.6120], list(FOURTH_AND_UNION)]),
                headline=str(p.get("header", ""))[:200],
                location_precision_m=25.0,
                authority=Authority.OFFICIAL,
            )

        if kind == "scl":
            return self.build_observation(
                record,
                observation_type=ObservationType.POWER_OUTAGE,
                geometry=polygon(
                    [
                        [-122.3450, 47.6020],
                        [-122.3330, 47.6020],
                        [-122.3330, 47.6100],
                        [-122.3450, 47.6100],
                        [-122.3450, 47.6020],
                    ]
                ),
                headline="Planned power outage, Pioneer Square",
                location_precision_m=300.0,
                authority=Authority.OFFICIAL,
            )

        return self.build_observation(
            record,
            observation_type=ObservationType.INFERENCE,
            geometry=point(*WESTLAKE),
            headline="Unclassified synthetic record",
            authority=Authority.INTERNAL,
        )

    def available(self) -> bool:
        return True


__all__ = ["SyntheticPugetSoundAdapter", "UTC"]
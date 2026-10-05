"""Stage 1 Significance Gate (Stage 1 §4).

Assigns a cheap, deterministic significance class to every observation before correlation:
- `event_candidate`: genuine incident or abnormal condition that demands event correlation.
- `state_only`: routine state baseline ('road clear', 'bridge clearance 17ft', 'ferry at dock').
  Stored in ledger and state store, but NEVER creates or triggers an active event.
- `context`: schedule, permit, or land-use context. Kept and indexed, creates events only
  in phase 'scheduled'.
- `noise`: routine administrative calls, advertising, or off-topic media. Kept in ledger, never correlated.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.enums import ObservationType, SignificanceClass
from ..domain.schemas import Observation

#: Routine SPD CAD / dispatch call types that represent steady-state patrols, not real-world incidents
ROUTINE_DISPATCH_PATTERNS = re.compile(
    r"\b(directed patrol|premise check|prevention check|area check|business check|"
    r"traffic stop|barking dog|parking enforcement|abandoned vehicle|found property|"
    r"complainant|problem solving project|training|administrative|detail)\b",
    re.IGNORECASE,
)

#: High-significance dispatch call types (accidents, fires, violence, hazards, closures)
CRITICAL_DISPATCH_PATTERNS = re.compile(
    r"\b(collision|crash|accident|fire|smoke|shooting|shots|gunshot|assault|robbery|"
    r"rescue|overdose|cardiac|extrication|hazard|block|closure|closed|collapse|"
    r"earthquake|evacuat|protest|riot|demonstrat|outage|spill|bomb|weapon)\b",
    re.IGNORECASE,
)

#: High-significance news keywords indicating a real-world incident
NEWS_INCIDENT_PATTERNS = re.compile(
    r"\b(crash|collision|killed|injured|victim|fire|explosion|police|investigat|"
    r"suspect|arrested|shooting|shot|protest|demonstration|march|closure|shut down|"
    r"earthquake|flood|wildfire|storm|warning|outage|derailment)\b",
    re.IGNORECASE,
)

#: Steady-state phrases indicating normal or cleared infrastructure
STEADY_STATE_PATTERNS = re.compile(
    r"\b(road clear|normal traffic|all lanes open|open to traffic|clearance \d+|"
    r"ferry at dock|in service|operating normally|on schedule|no restrictions)\b",
    re.IGNORECASE,
)


def classify_significance(observation: Observation) -> SignificanceClass:
    """Deterministic significance gate (Stage 1 §4).

    Classifies the observation into:
    EVENT_CANDIDATE, STATE_ONLY, CONTEXT, or NOISE.
    """
    obs_type = observation.observation_type
    headline = observation.headline.lower()
    payload = observation.structured_payload or {}
    text_content = f"{headline} {str(payload)}".lower()

    # 1. Physical World Hazards: Earthquakes, Severe Weather, Floods, Wildfires
    if obs_type in {
        ObservationType.EARTHQUAKE,
        ObservationType.SEVERE_WEATHER,
        ObservationType.FLOODING,
        ObservationType.WILDFIRE,
        ObservationType.OFFICIAL_EMERGENCY_NOTICE,
    }:
        return SignificanceClass.EVENT_CANDIDATE

    # 2. Public safety: Police, Fire, Violence
    if obs_type in {
        ObservationType.FIRE_DISPATCH,
        ObservationType.POLICE_RESPONSE,
        ObservationType.VIOLENCE_EVENT,
    }:
        if CRITICAL_DISPATCH_PATTERNS.search(headline):
            return SignificanceClass.EVENT_CANDIDATE
        if ROUTINE_DISPATCH_PATTERNS.search(headline):
            return SignificanceClass.NOISE
        # Default for dispatch is event candidate if not explicitly routine
        return SignificanceClass.EVENT_CANDIDATE

    # 3. Road & Infrastructure Conditions
    if obs_type in {
        ObservationType.ROAD_CLOSURE,
        ObservationType.ROAD_CONSTRUCTION,
        ObservationType.BRIDGE_RESTRICTION,
        ObservationType.POWER_OUTAGE,
        ObservationType.WATER_OUTAGE,
        ObservationType.TRANSIT_SERVICE_ALERT,
    }:
        # Check if this is explicitly a steady-state or clear announcement
        if STEADY_STATE_PATTERNS.search(text_content):
            return SignificanceClass.STATE_ONLY
        # End / cleared notifications for existing conditions are event candidates so they close the event
        if payload.get("disappeared") or payload.get("status") in {"cleared", "reopened", "restored"}:
            return SignificanceClass.EVENT_CANDIDATE
        return SignificanceClass.EVENT_CANDIDATE

    # 4. Steady-state sensors and telemetry: Traffic Flow, Travel Time, Vessel Positions
    if obs_type in {
        ObservationType.TRAFFIC_FLOW,
        ObservationType.TRAFFIC_CONDITION,
        ObservationType.TRAVEL_TIME,
        ObservationType.VEHICLE_POSITION,
        ObservationType.FERRY_STATUS,
        ObservationType.CAMERA_IMAGERY,
        ObservationType.FACILITY_STATUS,
    }:
        # Only severe delays or closures qualify as event candidates
        if CRITICAL_DISPATCH_PATTERNS.search(text_content) or "blocked" in text_content or "delay > 30" in text_content:
            return SignificanceClass.EVENT_CANDIDATE
        return SignificanceClass.STATE_ONLY

    # 5. Permits and Schedule Context
    if obs_type in {
        ObservationType.PERMIT_EVENT,
        ObservationType.POPULATION_CONTEXT,
        ObservationType.ACTIVITY_CONTEXT,
    }:
        return SignificanceClass.CONTEXT

    # 6. Public gatherings & protests
    if obs_type == ObservationType.PUBLIC_GATHERING_REPORT:
        return SignificanceClass.EVENT_CANDIDATE

    # 7. Unstructured News Articles
    if obs_type == ObservationType.NEWS_ARTICLE:
        if NEWS_INCIDENT_PATTERNS.search(text_content):
            return SignificanceClass.EVENT_CANDIDATE
        return SignificanceClass.NOISE

    return SignificanceClass.EVENT_CANDIDATE

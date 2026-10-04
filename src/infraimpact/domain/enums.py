"""Controlled vocabularies.

Brief section 65: "Never change semantics silently." These enums are the
schema's semantics, so additions are additive and renames are breaking.
"""

from __future__ import annotations

from enum import StrEnum


class SourceType(StrEnum):
    """Coarse provenance class. Drives the truth hierarchy (section 37)."""

    OFFICIAL_MACHINE_READABLE = "official_machine_readable"
    OFFICIAL_HUMAN_READABLE = "official_human_readable"
    ESTABLISHED_NEWS = "established_news"
    THIRD_PARTY_DATABASE = "third_party_database"
    UNVERIFIED_REPORT = "unverified_report"
    INTERNAL_DERIVED = "internal_derived"
    USER_SUPPLIED = "user_supplied"


class Authority(StrEnum):
    """Tier 1..5 of the truth hierarchy (section 37)."""

    OFFICIAL = "official"
    SEMI_OFFICIAL = "semi_official"
    ESTABLISHED_MEDIA = "established_media"
    COMMUNITY = "community"
    UNVERIFIED = "unverified"
    INTERNAL = "internal"


class ObservationType(StrEnum):
    """Universal observation taxonomy.

    Deliberately *domain-neutral*. Event-type specifics live in
    ``structured_payload``; this enum only says what kind of world-fact the
    record asserts. Adding an event class must not require adding enum values.
    """

    # Physical world
    EARTHQUAKE = "earthquake"
    SEVERE_WEATHER = "severe_weather"
    WEATHER_CONDITION = "weather_condition"
    FLOODING = "flooding"
    WILDFIRE = "wildfire"

    # Infrastructure state
    ROAD_CLOSURE = "road_closure"
    ROAD_CONSTRUCTION = "road_construction"
    TRAFFIC_CONDITION = "traffic_condition"
    TRAFFIC_FLOW = "traffic_flow"
    TRAVEL_TIME = "travel_time"
    CAMERA_IMAGERY = "camera_imagery"
    BRIDGE_RESTRICTION = "bridge_restriction"
    TRANSIT_SERVICE_ALERT = "transit_service_alert"
    TRANSIT_DELAY = "transit_delay"
    VEHICLE_POSITION = "vehicle_position"
    FERRY_STATUS = "ferry_status"

    # Utilities and facilities
    POWER_OUTAGE = "power_outage"
    WATER_OUTAGE = "water_outage"
    FACILITY_STATUS = "facility_status"

    #: Slow-moving population and land-use context (density, zoning, pedestrian
    #: activity). Additive per section 65: it was missing, and labelling these
    #: records as ``FACILITY_STATUS`` would assert that a neighbourhood polygon
    #: is a facility. Never a change signal - it is a weight on other
    #: observations, never an event in its own right.
    POPULATION_CONTEXT = "population_context"

    #: Movement, spending, employment and business activity - the behavioural
    #: counterpart to ``POPULATION_CONTEXT``. Additive per section 65 and
    #: weighted the same way: how much people are moving and spending in a
    #: place is a multiplier on other observations, never an incident. Keeps
    #: mobility and card-spending corpora out of the event taxonomy, where they
    #: do not belong.
    ACTIVITY_CONTEXT = "activity_context"

    #: An observed violent or lethal act, already having happened: a conflict
    #: event with fatalities, a fatal police shooting. Additive per section 65
    #: - none of the existing values describe one, and reusing e.g.
    #: ``POLICE_RESPONSE`` would misattribute authorship of the act. This
    #: value is descriptive of a record; it is never a prediction about a
    #: person or group, which section 20 forbids.
    VIOLENCE_EVENT = "violence_event"

    # Public safety and human activity
    POLICE_RESPONSE = "police_response"
    FIRE_DISPATCH = "fire_dispatch"
    OFFICIAL_EMERGENCY_NOTICE = "official_emergency_notice"
    PERMIT_EVENT = "permit_event"
    PUBLIC_GATHERING_REPORT = "public_gathering_report"

    # Media / unstructured
    NEWS_ARTICLE = "news_article"

    # Internal
    INFERENCE = "inference"
    FORECAST = "forecast"


class TruthStatus(StrEnum):
    """Section 41. Never render these identically."""

    CONFIRMED = "confirmed"
    REPORTED = "reported"
    INFERRED = "inferred"
    PREDICTED = "predicted"


class InferenceKind(StrEnum):
    """Section 2.3 / 38: observation, interpretation, prediction, recommendation.

    These four are stored separately and never collapsed.
    """

    OBSERVATION = "observation"
    INTERPRETATION = "interpretation"
    PREDICTION = "prediction"
    RECOMMENDATION = "recommendation"


class EventStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    CLOSED = "closed"


class HealthState(StrEnum):
    """Section 55."""

    HEALTHY = "healthy"
    DELAYED = "delayed"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class CapabilityTier(StrEnum):
    """Section 58: cost-aware analysis."""

    CHEAP = "cheap"
    TRIGGERED = "triggered"
    EXPENSIVE = "expensive"


class InfrastructureDomain(StrEnum):
    ROAD = "road"
    TRANSIT = "transit"
    UTILITY = "utility"
    PUBLIC_SAFETY = "public_safety"
    PUBLIC_FACILITY = "public_facility"
    FERRIES = "ferries"
    UNKNOWN = "unknown"


class NodeClass(StrEnum):
    """Section 30: infrastructure graph node classes."""

    ROAD_SEGMENT = "road_segment"
    INTERSECTION = "intersection"
    BRIDGE = "bridge"
    TUNNEL = "tunnel"
    TRANSIT_ROUTE = "transit_route"
    TRANSIT_STOP = "transit_stop"
    STATION = "station"
    FERRY_TERMINAL = "ferry_terminal"
    HOSPITAL = "hospital"
    SCHOOL = "school"
    EMERGENCY_FACILITY = "emergency_facility"
    UTILITY_AREA = "utility_area"
    USER_ROUTE_SEGMENT = "user_route_segment"


class EdgeKind(StrEnum):
    CONNECTS_TO = "CONNECTS_TO"
    SERVES = "SERVES"
    DEPENDS_ON = "DEPENDS_ON"
    INTERSECTS = "INTERSECTS"
    NEAR = "NEAR"
    ROUTES_THROUGH = "ROUTES_THROUGH"


class PresentationType(StrEnum):
    """Section 37: the backend ranks; the renderer decides.

    The backend must never emit ``show_level_2_screen``.
    """

    OFFICIAL_GUIDANCE = "official_guidance"
    ROUTE_DISRUPTION = "route_disruption"
    ALTERNATIVE_ROUTE = "alternative_route"
    INFRASTRUCTURE_IMPACT = "infrastructure_impact"
    USER_EXPOSURE = "user_exposure"
    EVENT_SUMMARY = "event_summary"
    WHAT_CHANGED = "what_changed"
    HISTORICAL_COMPARISON = "historical_comparison"
    EVIDENCE = "evidence"
    UNCERTAINTY = "uncertainty"
    QUIET = "quiet"


class Urgency(StrEnum):
    NONE = "none"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    IMMEDIATE = "immediate"


class NotificationReason(StrEnum):
    NEW_EVENT = "new_event"
    STATUS_CHANGE = "status_change"
    LOCATION_CHANGE = "location_change"
    IMPACT_CHANGE = "impact_change"
    ROUTE_CHANGE = "route_change"
    ESCALATION = "escalation"
    RESOLUTION = "resolution"
    OFFICIAL_GUIDANCE = "official_guidance"
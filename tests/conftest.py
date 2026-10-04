"""Shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from infraimpact.bus.event_bus import InMemoryEventBus  # noqa: E402
from infraimpact.domain.enums import Urgency  # noqa: E402
from infraimpact.domain.schemas import (  # noqa: E402
    AffectedInfrastructure,
    EventState,
    EvidenceSummary,
    Forecast,
    NotificationPreferences,
    RouteImpact,
    RouteProfile,
    SavedPlace,
    UserContext,
    UserExposure,
)
from infraimpact.storage.sqlite_driver import SqlitePlatformRepository  # noqa: E402

# Downtown Seattle, used throughout as the "centre" of the scenario.
DOWNTOWN = (-122.335, 47.608)
FAR_AWAY = (-122.42, 47.70)


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def repo(tmp_path) -> SqlitePlatformRepository:
    repository = SqlitePlatformRepository(f"sqlite:///{tmp_path / 'test.db'}")
    yield repository
    repository.close()


def point(lon: float, lat: float) -> dict:
    return {"type": "Point", "coordinates": [lon, lat]}


def line(*coords: tuple[float, float]) -> dict:
    return {"type": "LineString", "coordinates": [list(c) for c in coords]}


@pytest.fixture
def downtown_user() -> UserContext:
    return UserContext(
        user_id="u_downtown",
        saved_places=(
            SavedPlace(
                place_id="p_home",
                name="Home",
                kind="home",
                geometry=point(*DOWNTOWN),
            ),
        ),
        route_profiles=(
            RouteProfile(
                route_id="r_commute",
                name="Downtown commute",
                geometry=line((-122.37, 47.62), DOWNTOWN),
                modes=("drive", "transit"),
                node_ids=("n_a", "n_b"),
            ),
        ),
        current_location=point(*DOWNTOWN),
        preferences=NotificationPreferences(minimum_urgency=Urgency.LOW),
    )


@pytest.fixture
def remote_user() -> UserContext:
    return UserContext(
        user_id="u_remote",
        saved_places=(
            SavedPlace(
                place_id="p_home",
                name="Home",
                kind="home",
                geometry=point(*FAR_AWAY),
            ),
        ),
    )


def make_state(
    *,
    event_id: str = "evt_test",
    state_version: int = 1,
    geometry: dict | None = None,
    impacts: tuple[AffectedInfrastructure, ...] = (),
    official: bool = False,
    geometry_confidence: float = 0.9,
    source_count: int = 2,
    independent: int = 2,
    contradictions: int = 0,
    status: str = "active",
    observation_ids: tuple[str, ...] = (),
) -> EventState:
    evidence = EvidenceSummary(
        source_count=source_count,
        independent_source_count=independent,
        authorities={"official": 1} if official else {"established_media": source_count},
        observation_types={"official_emergency_notice": 1} if official else {},
        contradictions=contradictions,
        vector={
            "source_authority": 0.9 if official else 0.6,
            "independent_corroboration": 0.5 if independent > 1 else 0.0,
        },
    )
    return EventState(
        event_id=event_id,
        state_version=state_version,
        status=status,
        geometry=geometry,
        geometry_confidence=geometry_confidence,
        affected_infrastructure=impacts,
        evidence=evidence,
        observation_ids=observation_ids,
        derived={"is_official_guidance": 1.0} if official else {},
    )


def make_impact(
    identifier: str = "road_1",
    *,
    severity: Urgency = Urgency.HIGH,
    geometry: dict | None = None,
    truth_status: str = "confirmed",
    domain: str = "road",
) -> AffectedInfrastructure:
    from infraimpact.domain.enums import InfrastructureDomain, TruthStatus

    return AffectedInfrastructure(
        domain=InfrastructureDomain(domain),
        identifier=identifier,
        name=identifier.replace("_", " ").title(),
        geometry=geometry if geometry is not None else point(*DOWNTOWN),
        severity=severity,
        truth_status=TruthStatus(truth_status),
        observation_ids=("obs_1",),
    )


def make_exposure(
    *,
    user_id: str = "u_downtown",
    event_id: str = "evt_test",
    level: Urgency = Urgency.HIGH,
    score: float = 0.7,
    confidence: float = 0.8,
    routes: tuple[RouteImpact, ...] = (),
    places: dict[str, float] | None = None,
    distance_m: float | None = 50.0,
    run_id: str = "run_1",
) -> UserExposure:
    return UserExposure(
        user_id=user_id,
        event_id=event_id,
        analysis_run_id=run_id,
        distance_m=distance_m,
        inside_impact_area=distance_m == 0.0,
        route_impacts=routes,
        saved_place_impacts=places or {},
        exposure_level=level,
        exposure_score=score,
        confidence=confidence,
    )


def make_forecast(probability: float = 0.7, *, width: float = 0.2) -> Forecast:
    from infraimpact.domain.enums import InfrastructureDomain

    lower = max(0.0, probability - width / 2)
    return Forecast(
        target="road_1 closure",
        domain=InfrastructureDomain.ROAD,
        horizon_minutes=30,
        probability=probability,
        lower=lower,
        upper=min(1.0, lower + width),
        model_id="test",
    )
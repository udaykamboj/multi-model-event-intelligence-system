"""API tests (brief section 50/51).

Two things are worth guarding here:

* section 51's "do not return raw model internals to normal clients", and
* section 49's replay contract, since both are easy to break silently by adding
  a field to a response model.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from infraimpact.api.app import create_app
from infraimpact.api.routes.stream import STREAM_TOPICS
from infraimpact.bus.event_bus import InMemoryEventBus, Topic
from infraimpact.config import Settings
from infraimpact.domain.geo import point
from infraimpact.domain.ids import content_hash
from infraimpact.domain.schemas import (
    EventState,
    Observation,
    ObservationQuality,
    Provenance,
    RouteImpact,
    UserImpactState,
)
from infraimpact.domain.enums import Authority, ObservationType, SourceType, Urgency

from conftest import DOWNTOWN, line, make_exposure, make_state


def make_observation(observation_id: str = "obs_1", **overrides) -> Observation:
    payload = {"kind": "road_closure", "lanes": "2"}
    now = datetime.now(UTC)
    fields = dict(
        observation_id=observation_id,
        source_id="synthetic.puget_sound",
        source_record_id=f"rec_{observation_id}",
        event_time=now,
        observed_at=now,
        ingested_at=now,
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=ObservationType.ROAD_CLOSURE,
        geometry=point(*DOWNTOWN),
        headline="I-5 closed downtown",
        structured_payload=payload,
        provenance=Provenance(
            authority=Authority.OFFICIAL,
            retrieval_method="feed",
            content_hash=content_hash(payload),
        ),
        quality=ObservationQuality(source_reliability=0.95, spatial_precision=0.9),
    )
    fields.update(overrides)
    return Observation(**fields)


@pytest.fixture
def app_client(repo, bus):
    """A TestClient over an app seeded with one event and one user."""
    repo.events.ensure("evt_1", datetime.now(UTC), "puget-sound")
    repo.observations.append(make_observation())
    repo.events.link_observation("evt_1", "obs_1")
    repo.states.append_state(make_state(event_id="evt_1", geometry=point(*DOWNTOWN)))
    repo.users.upsert(
        __import__("infraimpact.domain.schemas", fromlist=["UserContext"]).UserContext(
            user_id="u_downtown",
            current_location=point(*DOWNTOWN),
        )
    )

    settings = Settings(database_url="sqlite:///unused.db", region_id="puget-sound")
    with TestClient(create_app(settings, repo, bus, run_loop=False)) as client:
        yield client


# --------------------------------------------------------------------------
# section 51: the event envelope
# --------------------------------------------------------------------------


class TestEventDetail:
    def test_returns_the_section_51_envelope(self, app_client):
        body = app_client.get("/v1/events/evt_1").json()
        assert set(body) == {
            "event",
            "current_state",
            "top_impacts",
            "recent_changes",
            "forecast",
            "evidence_summary",
            "last_updated",
        }

    def test_does_not_leak_model_internals(self, app_client):
        # Section 51: "Do not return raw model internals to normal clients."
        forbidden = {"model_outputs", "features", "jev_decisions", "llm_operations"}
        for path in ("/v1/events/evt_1", "/v1/events/evt_1/analysis", "/v1/events/evt_1/timeline"):
            raw = app_client.get(path).text
            assert not (forbidden & set(json.loads(raw))), path
            for key in forbidden:
                assert f'"{key}"' not in raw, f"{key} leaked by {path}"

    def test_analysis_still_reports_which_models_ran(self, app_client):
        # Audit information is fine; only internals are withheld.
        body = app_client.get("/v1/events/evt_1/analysis").json()
        assert body["count"] == 0
        fields = set(body["runs"][0].model_fields) if body["runs"] else set()
        assert "models_invoked" in (fields or {"models_invoked"})

    def test_unknown_event_is_404(self, app_client):
        assert app_client.get("/v1/events/nope").status_code == 404


class TestEventListing:
    def test_lists_events(self, app_client):
        body = app_client.get("/v1/events").json()
        assert body["count"] == 1
        assert body["events"][0]["event_id"] == "evt_1"

    def test_filters_by_status(self, app_client):
        # ``ensure`` creates events as 'candidate'; the filter must agree.
        assert app_client.get("/v1/events", params={"status": "closed"}).json()["count"] == 0
        assert app_client.get("/v1/events", params={"status": "candidate"}).json()["count"] == 1
        assert app_client.get("/v1/events", params={"status": "active"}).json()["count"] == 0

    def test_rejects_an_unparseable_since(self, app_client):
        assert app_client.get("/v1/events", params={"since": "garbage"}).status_code == 400


class TestSubresources:
    def test_timeline_is_oldest_first(self, repo, app_client):
        base = datetime.now(UTC)
        repo.states.append_state(
            EventState(event_id="evt_1", state_version=2, reconstructed_at=base + timedelta(minutes=5))
        )
        body = app_client.get("/v1/events/evt_1/timeline").json()
        assert [e["state_version"] for e in body["entries"]] == [1, 2]

    def test_evidence_lists_observations_with_provenance(self, app_client):
        body = app_client.get("/v1/events/evt_1/evidence").json()
        assert len(body["observations"]) == 1
        assert body["observations"][0]["provenance"]["authority"] == "official"

    def test_forecast_is_empty_before_any_analysis(self, app_client):
        assert app_client.get("/v1/events/evt_1/forecast").json()["forecasts"] == []


# --------------------------------------------------------------------------
# section 49: replay through the API
# --------------------------------------------------------------------------


class TestReplayRoute:
    """Section 49 through the API, against a backfilled event.

    The fixture already wrote a state version 1 for ``evt_1`` at "now", so these
    use their own event whose rows are all reconstructed in the past - which is
    exactly the backfill case that ``created_at`` would get wrong.
    """

    EVENT = "evt_backfill"
    BASE = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)

    @pytest.fixture
    def backfilled(self, repo):
        repo.events.ensure(self.EVENT, self.BASE, "puget-sound")
        repo.states.append_state(
            EventState(event_id=self.EVENT, state_version=1, reconstructed_at=self.BASE)
        )
        repo.states.append_state(
            EventState(
                event_id=self.EVENT,
                state_version=2,
                reconstructed_at=self.BASE + timedelta(hours=1),
            )
        )
        return repo

    def test_reconstructs_the_belief_at_a_past_moment(self, backfilled, app_client):
        body = app_client.post(
            "/internal/replay",
            json={
                "event_id": self.EVENT,
                "as_of": (self.BASE + timedelta(minutes=30)).isoformat(),
            },
        ).json()
        assert body["state_version"] == 1

    def test_sees_the_later_belief_once_it_existed(self, backfilled, app_client):
        body = app_client.post(
            "/internal/replay",
            json={
                "event_id": self.EVENT,
                "as_of": (self.BASE + timedelta(hours=2)).isoformat(),
            },
        ).json()
        assert body["state_version"] == 2

    def test_refuses_to_invent_a_belief_before_the_event_existed(self, backfilled, app_client):
        response = app_client.post(
            "/internal/replay",
            json={
                "event_id": self.EVENT,
                "as_of": (self.BASE - timedelta(hours=1)).isoformat(),
            },
        )
        assert response.status_code == 404

    def test_rejects_an_unparseable_timestamp(self, app_client):
        response = app_client.post(
            "/internal/replay", json={"event_id": "evt_1", "as_of": "not-a-time"}
        )
        assert response.status_code == 400

    def test_unknown_event_is_404(self, app_client):
        assert app_client.post(
            "/internal/replay", json={"event_id": "nope", "as_of": datetime.now(UTC).isoformat()}
        ).status_code == 404


# --------------------------------------------------------------------------
# user routes
# --------------------------------------------------------------------------


class TestUserRoutes:
    def test_impacts_require_a_known_user(self, app_client):
        assert app_client.get("/v1/user/impacts", params={"user_id": "ghost"}).status_code == 404
        assert app_client.get("/v1/user/impacts", params={"user_id": "u_downtown"}).status_code == 200

    def test_impacts_are_ordered_by_priority(self, repo, app_client):
        state = repo.states.latest("evt_1")
        repo.user_impacts.put(
            UserImpactState(
                user_id="u_downtown",
                event_id="evt_1",
                current=make_exposure(score=0.2, level=Urgency.LOW, run_id="run_low"),
            ).model_dump_json(),
            "run_low",
        )
        repo.user_impacts.put(
            UserImpactState(
                user_id="u_downtown",
                event_id="evt_1",
                current=make_exposure(score=0.9, level=Urgency.HIGH, run_id="run_high"),
            ).model_dump_json(),
            "run_high",
        )
        body = app_client.get("/v1/user/impacts", params={"user_id": "u_downtown"}).json()
        scores = [i["exposure_score"] for i in body["impacts"]]
        assert scores == sorted(scores, reverse=True)

    def test_unknown_route_is_404(self, app_client):
        assert app_client.get("/v1/user/routes/nope/impact").status_code == 404

    def test_route_impact_resolves_the_owning_user(self, repo, app_client):
        from infraimpact.domain.schemas import RouteProfile, UserContext

        repo.users.upsert(
            UserContext(
                user_id="u_commuter",
                route_profiles=(
                    RouteProfile(
                        route_id="r_commute",
                        name="Commute",
                        geometry=line((-122.37, 47.62), DOWNTOWN),
                    ),
                ),
            )
        )
        repo.user_impacts.put(
            UserImpactState(
                user_id="u_commuter",
                event_id="evt_1",
                current=make_exposure(
                    user_id="u_commuter",
                    routes=(
                        RouteImpact(
                            route_id="r_commute",
                            route_name="Commute",
                            intersects=True,
                            delay_estimate_min=12.0,
                            blocked_node_ids=("n_a",),
                        ),
                    ),
                ),
            ).model_dump_json(),
            "run_1",
        )
        body = app_client.get("/v1/user/routes/r_commute/impact").json()
        assert body["count"] == 1
        assert body["impacts"][0]["user_id"] == "u_commuter"
        assert body["impacts"][0]["delay_estimate_min"] == 12.0


# --------------------------------------------------------------------------
# internal routes
# --------------------------------------------------------------------------


class TestInternalRoutes:
    def test_ingest_is_idempotent_on_the_dedupe_key(self, repo, app_client):
        body = make_observation("obs_new").model_dump(mode="json")
        first = app_client.post("/internal/observations", json={"observations": [body]}).json()
        second = app_client.post("/internal/observations", json={"observations": [body]}).json()
        assert first["accepted"] == 1
        assert second["accepted"] == 0
        assert second["duplicates"] == 1
        assert repo.observations.count() == 2

    def test_malformed_observations_are_rejected(self, app_client):
        response = app_client.post("/internal/observations", json={"observations": [{"nope": 1}]})
        assert response.status_code == 422

    def test_analysis_request_without_state_is_404(self, app_client):
        response = app_client.post("/internal/analysis/request", json={"event_id": "nope"})
        assert response.status_code == 404

    def test_analysis_request_runs_capabilities(self, app_client):
        body = app_client.post("/internal/analysis/request", json={"event_id": "evt_1"}).json()
        assert body["analysis_run_id"]
        assert body["capabilities_invoked"]

    def test_models_evaluate_exposes_internals_that_v1_withholds(self, app_client):
        # The point of the internal surface: what /v1 refuses to return.
        body = app_client.post("/internal/models/evaluate", json={"event_id": "evt_1"}).json()
        assert "features" in body
        assert "model_outputs" in body
        assert "jev_decisions" in body

    def test_models_evaluate_without_state_is_404(self, app_client):
        assert app_client.post("/internal/models/evaluate", json={"event_id": "nope"}).status_code == 404


# --------------------------------------------------------------------------
# regions / ops
# --------------------------------------------------------------------------


class TestRegionAndOps:
    def test_region_state_merges_spec_with_observed_health(self, app_client):
        body = app_client.get("/v1/regions/puget-sound/state").json()
        assert body["region_id"] == "puget-sound"
        assert body["timezone"] == "America/Los_Angeles"
        # A configured-but-idle adapter still appears, carrying its profile.
        assert body["sources"]
        assert all("expected_interval_s" in s for s in body["sources"])

    def test_unknown_region_is_404(self, app_client):
        assert app_client.get("/v1/regions/mars/state").status_code == 404

    def test_healthz_reports_counts(self, app_client):
        body = app_client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["counts"]["observations"] >= 1


# --------------------------------------------------------------------------
# section 50: the real-time channel
# --------------------------------------------------------------------------


def _drain(app_client_factory, topics: str | None = None, publish: int = 2):
    """Read ``publish`` frames off the SSE generator.

    Starlette's TestClient waits for the whole ASGI call to finish, which an
    open-ended stream never does - so the route's generator is driven directly.
    """
    bus = InMemoryEventBus(heartbeat_interval_s=0.05)
    settings = Settings(database_url="sqlite:///unused.db", region_id="puget-sound")
    app = create_app(settings, None, bus, run_loop=False)
    app.state.bus = bus

    from infraimpact.api.routes.stream import stream as stream_route

    class _Req:
        """Minimal stand-in for a live request; the route only touches .app."""

        async def is_disconnected(self) -> bool:
            return False

    req = _Req()
    req.app = app

    async def run() -> list[bytes]:
        response = await stream_route(req, bus=bus, topics=topics, user_id=None)
        frames: list[bytes] = []
        agen = response.body_iterator

        async def pump() -> None:
            for i in range(publish):
                await bus.publish(
                    Topic.STATE_UPDATED, {"event_id": f"evt_{i}"}, key=f"evt_{i}"
                )

        task = asyncio.create_task(pump())
        try:
            for _ in range(publish):
                frames.append(await agen.__anext__())
        finally:
            task.cancel()
            await agen.aclose()
        return frames

    return asyncio.run(run()), bus


class TestStream:
    def test_frames_are_well_formed_sse(self):
        frames, _ = _drain(None, topics="state.updated")
        text = frames[0].decode()
        assert text.startswith("event: state.updated\n")
        assert "data: {" in text
        assert text.endswith("\n\n")

    def test_topic_filter_drops_other_topics(self):
        frames, bus = _drain(None, topics="alerts.sent")
        # The pump publishes state.updated, which the filter excludes, so the
        # next frame delivered is the bus heartbeat - which must still pass.
        topic_lines = [f.decode().split("\n")[0] for f in frames]
        assert all(
            line in {"event: heartbeat", "event: alerts.sent"} for line in topic_lines
        ), topic_lines

    def test_heartbeat_is_never_filtered_out(self):
        # An idle stream filtered to one topic must not go silent.
        frames, _ = _drain(None, topics="alerts.sent")
        assert any(b"event: heartbeat" in f for f in frames)

    def test_no_filter_delivers_every_client_topic(self):
        frames, _ = _drain(None, topics=None)
        assert all(b"event: state.updated" in f for f in frames)

    def test_dead_letter_is_not_a_client_topic(self):
        assert "dead_letter" not in STREAM_TOPICS
        assert str(Topic.DEAD_LETTER) not in STREAM_TOPICS
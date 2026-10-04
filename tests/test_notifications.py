"""Sections 52 and 53: notification architecture and conditions.

Section 52 fixes the pipeline order and the dedupe key. Section 53 lists the
conditions to consider and permits one bypass. Both are tested here against a
real SQLite notification repository, because the suppression logic is only
meaningful against actual stored state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_exposure, make_impact, make_state

from infraimpact.domain.enums import NotificationReason, Urgency
from infraimpact.domain.schemas import (
    NotificationCandidate,
    NotificationPreferences,
    PresentationItem,
    UserContext,
)
from infraimpact.users.notifications import (
    NotificationContext,
    NotificationEngine,
    _in_quiet_hours,
)
from infraimpact.users.presentation import PresentationType


@pytest.fixture
def engine(repo):
    return NotificationEngine(
        timezone="America/Los_Angeles", repository=repo.notifications
    )


@pytest.fixture
def user():
    return UserContext(
        user_id="u1",
        current_location={"type": "Point", "coordinates": [-122.335, 47.608]},
        preferences=NotificationPreferences(minimum_urgency=Urgency.LOW),
    )


@pytest.fixture
def presentation():
    return (
        PresentationItem(
            type=PresentationType.ROUTE_DISRUPTION,
            priority=0.9,
            headline="Your usual route is affected",
            evidence_ids=("obs_1",),
        ),
    )


def ctx(
    user,
    presentation,
    *,
    exposure=None,
    state=None,
    version=1,
    is_new_event=True,
    previous_exposure_level=None,
    **kwargs,
):
    """Build a NotificationContext.

    ``is_new_event`` defaults to True so the candidate reason is NEW_EVENT
    unless a test asks for something else. ``previous_exposure_level`` is
    passed separately because it drives the escalation check in ``_reason``.
    """
    exposure = exposure or make_exposure(
        user_id=user.user_id, level=Urgency.HIGH, score=0.7, confidence=0.8
    )
    state = state or make_state()
    from infraimpact.users.priority import PriorityContext, UserPriorityEngine

    priority = UserPriorityEngine().compute(
        PriorityContext(exposure=exposure, state=state, is_new_event=is_new_event)
    )
    return NotificationContext(
        user=user,
        exposure=exposure,
        priority=priority,
        presentation=presentation,
        state=state,
        state_version=version,
        is_new_event=is_new_event,
        previous_exposure_level=previous_exposure_level,
        **kwargs,
    )


def store(repo, ctx_obj, *, hours_ago: float = 0.0, reason=None) -> NotificationCandidate:
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    candidate = NotificationCandidate(
        notification_id=f"nt_{hours_ago}_{ctx_obj.user.user_id}_{ctx_obj.state_version}",
        user_id=ctx_obj.user.user_id,
        event_id=ctx_obj.exposure.event_id,
        reason=reason or NotificationReason.NEW_EVENT,
        urgency=ctx_obj.priority.urgency_band,
        headline="prior alert",
        body="",
        dedupe_key=NotificationEngine.dedupe_key(ctx_obj),
        created_at=when,
    )
    repo.notifications.enqueue(candidate)
    repo.notifications.mark_sent(candidate.notification_id)
    return candidate


class TestDedupeKey:
    def test_key_has_the_four_documented_parts(self, user, presentation):
        c = ctx(user, presentation, version=7)
        key = NotificationEngine.dedupe_key(c)
        assert key.split("|") == ["u1", "evt_test", "event", "7"]

    def test_key_changes_with_state_version(self, user, presentation):
        assert NotificationEngine.dedupe_key(
            ctx(user, presentation, version=1)
        ) != NotificationEngine.dedupe_key(ctx(user, presentation, version=2))

    def test_key_changes_with_impact_type(self, user, presentation):
        a = ctx(user, presentation)
        b = ctx(user, presentation)
        b.impact_type = "road"
        assert NotificationEngine.dedupe_key(a) != NotificationEngine.dedupe_key(b)


class TestSection52Pipeline:
    def test_new_event_is_delivered(self, engine, user, presentation):
        result = engine.evaluate(ctx(user, presentation))
        assert result is not None
        assert result.decision.allowed
        assert result.candidate.reason is NotificationReason.NEW_EVENT

    def test_identical_state_version_is_suppressed(self, engine, repo, user, presentation):
        c = ctx(user, presentation, version=5)
        store(repo, c)
        result = engine.evaluate(c)
        assert not result.decision.allowed
        assert result.decision.reason == "duplicate_dedupe_key"

    def test_candidate_exists_even_when_suppressed(self, engine, repo, user, presentation):
        # "Why did the user not get told?" must always be answerable.
        c = ctx(user, presentation, version=5)
        store(repo, c)
        result = engine.evaluate(c)
        assert result.candidate is not None
        assert result.candidate.dedupe_key.endswith("|5")

    def test_new_state_version_gets_past_exact_dedupe(self, engine, repo, user, presentation):
        # Exact dedupe is keyed on state_version, so version 2 is a different
        # key. It still has to clear equivalence suppression, which is the
        # layer that stops a repeat poll re-alerting an unchanged situation -
        # so an escalation is what gets it through.
        store(repo, ctx(user, presentation, version=1), hours_ago=3.0)
        result = engine.evaluate(
            ctx(
                user,
                presentation,
                exposure=make_exposure(level=Urgency.IMMEDIATE, score=0.9, confidence=0.9),
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.HIGH,
            )
        )
        assert result.decision.allowed

    def test_no_repository_means_no_suppression(self, user, presentation):
        bare = NotificationEngine(timezone="UTC", repository=None)
        result = bare.evaluate(ctx(user, presentation))
        assert result.decision.allowed


class TestEquivalenceSuppression:
    def test_repeat_within_window_is_suppressed(self, engine, repo, user, presentation):
        store(repo, ctx(user, presentation, version=1), hours_ago=0.05)
        result = engine.evaluate(
            ctx(user, presentation, version=2, is_new_event=False)
        )
        assert not result.decision.allowed
        assert result.decision.reason == "equivalent_recent_alert"

    def test_repeat_outside_window_is_allowed(self, engine, repo, user, presentation):
        store(repo, ctx(user, presentation, version=1), hours_ago=2.0)
        result = engine.evaluate(ctx(user, presentation, version=2))
        assert result.decision.allowed

    def test_escalation_gets_through(self, engine, repo, user, presentation):
        # A prior low alert, now high: the user must hear about the change.
        # is_new_event must be False, or NEW_EVENT is returned before the
        # escalation check is ever consulted.
        low = make_exposure(level=Urgency.LOW, score=0.2, confidence=0.8)
        store(repo, ctx(user, presentation, exposure=low, version=1), hours_ago=0.1)
        high = make_exposure(level=Urgency.IMMEDIATE, score=0.9, confidence=0.9)
        result = engine.evaluate(
            ctx(
                user,
                presentation,
                exposure=high,
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.LOW,
            )
        )
        assert result.decision.allowed
        assert result.candidate.reason is NotificationReason.ESCALATION

    def test_deescalation_does_not_bypass(self, engine, repo, user, presentation):
        high = make_exposure(level=Urgency.HIGH, score=0.7, confidence=0.8)
        store(repo, ctx(user, presentation, exposure=high, version=1), hours_ago=0.1)
        low = make_exposure(level=Urgency.LOW, score=0.2, confidence=0.8)
        result = engine.evaluate(
            ctx(
                user,
                presentation,
                exposure=low,
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.HIGH,
            )
        )
        assert not result.decision.allowed

    def test_another_user_is_not_affected(self, engine, repo, user, presentation):
        store(repo, ctx(user, presentation, version=1), hours_ago=0.05)
        other = user.model_copy(update={"user_id": "u2"})
        result = engine.evaluate(
            ctx(
                other,
                presentation,
                exposure=make_exposure(user_id="u2", level=Urgency.IMMEDIATE, score=0.9),
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.LOW,
            )
        )
        assert result.decision.allowed

    def test_another_event_is_not_affected(self, engine, repo, user, presentation):
        store(repo, ctx(user, presentation, version=1), hours_ago=0.05)
        other_exposure = make_exposure(event_id="evt_other", level=Urgency.HIGH, score=0.7)
        result = engine.evaluate(
            ctx(
                user,
                presentation,
                exposure=other_exposure,
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.LOW,
            )
        )
        assert result.decision.allowed


class TestPolicyChecks:
    def test_below_minimum_urgency_is_suppressed(self, engine, user, presentation):
        quiet_user = user.model_copy(
            update={
                "preferences": NotificationPreferences(minimum_urgency=Urgency.IMMEDIATE)
            }
        )
        mild = make_exposure(level=Urgency.LOW, score=0.2, confidence=0.8)
        result = engine.evaluate(ctx(quiet_user, presentation, exposure=mild))
        assert not result.decision.allowed
        assert result.decision.reason == "below_minimum_urgency"

    def test_no_channels_means_no_delivery(self, engine, user, presentation):
        silent = user.model_copy(
            update={"preferences": NotificationPreferences(channels=())}
        )
        result = engine.evaluate(ctx(silent, presentation))
        assert not result.decision.allowed
        assert result.decision.reason == "no_channels_enabled"

    def test_unexposed_user_gets_no_candidate(self, engine, user, presentation):
        far = make_exposure(level=Urgency.NONE, score=0.0, confidence=0.5, distance_m=9000.0)
        assert engine.evaluate(ctx(user, presentation, exposure=far)) is None


#: A fixed instant whose *Pacific local* hour is 03:00, so it falls inside any
#: overnight quiet window regardless of when the suite happens to run.
#:
#: Quiet-hours tests inject this rather than reading the wall clock. They used
#: ``quiet_hours_local=(0, 23)``, which reads as "quiet almost always" and is
#: therefore false for exactly the one hour in every day when the clock is past
#: 23:00 - so the suite failed once a day, and the failure looked like a policy
#: regression rather than a broken test.
QUIET_HOUR = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)  # 03:00 PDT


def sleeper_prefs() -> NotificationPreferences:
    """Preferences for someone asleep: everything wanted, nothing deliverable."""

    return NotificationPreferences(
        minimum_urgency=Urgency.LOW, quiet_hours_local=(22, 7)
    )


class TestQuietHours:
    @pytest.mark.parametrize("hour,start,end,expected", [
        (23, 22, 7, True),
        (3, 22, 7, True),
        (12, 22, 7, False),
        (5, 2, 6, True),
        (1, 2, 6, False),
        (8, 22, 7, False),
        (22, 22, 7, True),
    ])
    def test_window_arithmetic(self, hour, start, end, expected):
        assert _in_quiet_hours(hour, start, end) is expected

    def test_quiet_hours_suppress_a_normal_alert(self, engine, user, presentation):
        sleeper = user.model_copy(update={"preferences": sleeper_prefs()})
        result = engine.evaluate(ctx(sleeper, presentation), now=QUIET_HOUR)
        assert not result.decision.allowed
        assert result.decision.reason == "quiet_hours"

    def test_official_guidance_bypasses_quiet_hours(self, engine, user, presentation):
        sleeper = user.model_copy(update={"preferences": sleeper_prefs()})
        state = make_state(official=True)
        result = engine.evaluate(
            ctx(sleeper, presentation, state=state), now=QUIET_HOUR
        )
        assert result.decision.allowed
        assert "quiet_hours" in result.decision.bypassed

    def test_quiet_hours_do_not_suppress_outside_the_window(self, engine, user, presentation):
        """The window, not the policy, is what suppresses. 19:00 is not quiet."""
        sleeper = user.model_copy(update={"preferences": sleeper_prefs()})
        evening = datetime(2026, 7, 15, 2, 0, tzinfo=UTC)  # 19:00 PDT
        result = engine.evaluate(ctx(sleeper, presentation), now=evening)
        assert result.decision.allowed

    def test_no_quiet_hours_configured_never_suppresses(self, engine, user, presentation):
        result = engine.evaluate(ctx(user, presentation))
        assert result.decision.allowed


def escalating(level: Urgency, score: float):
    return make_exposure(level=level, score=score, confidence=0.8)


class TestRateLimiting:
    """Rate limiting is reached by genuinely escalating alerts.

    Repeated identical alerts are already stopped by equivalence suppression,
    which runs earlier in the section 52 pipeline. Escalations are the traffic
    that actually consumes a user's alert budget, so that is what these tests
    fill the budget with.
    """

    def test_limit_is_enforced(self, engine, repo, user, presentation):
        capped = user.model_copy(
            update={
                "preferences": NotificationPreferences(
                    minimum_urgency=Urgency.LOW, max_alerts_per_hour=2
                )
            }
        )
        store(
            repo,
            ctx(
                capped,
                presentation,
                exposure=escalating(Urgency.MODERATE, 0.5),
                version=1,
                is_new_event=False,
                previous_exposure_level=Urgency.LOW,
            ),
            hours_ago=0.1,
            reason=NotificationReason.ESCALATION,
        )
        store(
            repo,
            ctx(
                capped,
                presentation,
                exposure=escalating(Urgency.HIGH, 0.75),
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.MODERATE,
            ),
            hours_ago=0.2,
            reason=NotificationReason.ESCALATION,
        )
        result = engine.evaluate(
            ctx(
                capped,
                presentation,
                exposure=escalating(Urgency.IMMEDIATE, 0.95),
                version=3,
                is_new_event=False,
                previous_exposure_level=Urgency.HIGH,
            )
        )
        assert not result.decision.allowed
        assert result.decision.reason == "rate_limited"

    def test_old_alerts_do_not_count(self, engine, repo, user, presentation):
        capped = user.model_copy(
            update={
                "preferences": NotificationPreferences(
                    minimum_urgency=Urgency.LOW, max_alerts_per_hour=1
                )
            }
        )
        store(repo, ctx(capped, presentation, version=1), hours_ago=3.0)
        result = engine.evaluate(
            ctx(
                capped,
                presentation,
                exposure=escalating(Urgency.HIGH, 0.8),
                version=2,
                is_new_event=False,
                previous_exposure_level=Urgency.LOW,
            )
        )
        assert result.decision.allowed

    def test_official_guidance_bypasses_the_limit(self, engine, repo, user, presentation):
        capped = user.model_copy(
            update={
                "preferences": NotificationPreferences(
                    minimum_urgency=Urgency.LOW, max_alerts_per_hour=1
                )
            }
        )
        for i, (level, score) in enumerate(
            [(Urgency.MODERATE, 0.5), (Urgency.HIGH, 0.8)], start=1
        ):
            store(
                repo,
                ctx(
                    capped,
                    presentation,
                    exposure=escalating(level, score),
                    version=i,
                    is_new_event=False,
                    previous_exposure_level=Urgency.LOW,
                ),
                hours_ago=0.1 * i,
                reason=NotificationReason.ESCALATION,
            )
        state = make_state(official=True)
        result = engine.evaluate(
            ctx(
                capped,
                presentation,
                exposure=escalating(Urgency.IMMEDIATE, 0.95),
                state=state,
                version=3,
                is_new_event=False,
                previous_exposure_level=Urgency.HIGH,
            )
        )
        assert result.decision.allowed
        assert "rate_limit" in result.decision.bypassed

    def test_zero_limit_means_disabled(self, engine, repo, user, presentation):
        capped = user.model_copy(
            update={
                "preferences": NotificationPreferences(
                    minimum_urgency=Urgency.LOW, max_alerts_per_hour=0
                )
            }
        )
        for i, level in enumerate(
            [Urgency.MODERATE, Urgency.HIGH, Urgency.IMMEDIATE], start=1
        ):
            store(
                repo,
                ctx(
                    capped,
                    presentation,
                    exposure=escalating(level, 0.6),
                    version=i,
                    is_new_event=False,
                    previous_exposure_level=Urgency.LOW,
                ),
                hours_ago=0.05 * i,
                reason=NotificationReason.ESCALATION,
            )
        result = engine.evaluate(
            ctx(
                capped,
                presentation,
                exposure=escalating(Urgency.IMMEDIATE, 0.95),
                version=9,
                is_new_event=False,
                previous_exposure_level=Urgency.HIGH,
            )
        )
        assert result.decision.allowed


class TestOfficialBypassLimits:
    def test_official_about_a_distant_user_does_not_bypass(self, engine, user, presentation):
        """An official alert three counties away is still noise."""
        sleeper = user.model_copy(update={"preferences": sleeper_prefs()})
        far = make_exposure(
            level=Urgency.LOW, score=0.05, confidence=0.8, distance_m=9000.0
        )
        result = engine.evaluate(
            ctx(sleeper, presentation, exposure=far, state=make_state(official=True)),
            now=QUIET_HOUR,
        )
        assert not result.decision.allowed
        assert result.decision.reason == "quiet_hours"

    def test_official_never_bypasses_exact_duplicate(self, engine, repo, user, presentation):
        """Sending the identical official alert twice is never justified."""
        sleeper = user.model_copy(update={"preferences": sleeper_prefs()})
        state = make_state(official=True)
        c = ctx(sleeper, presentation, state=state, version=4)
        store(repo, c)
        result = engine.evaluate(c, now=QUIET_HOUR)
        assert not result.decision.allowed
        assert result.decision.reason == "duplicate_dedupe_key"


class TestNotificationContent:
    def test_reason_reflects_the_change(self, engine, user, presentation):
        from infraimpact.domain.schemas import RouteImpact

        route = RouteImpact(
            route_id="r1", route_name="Commute", intersects=True, blocked_node_ids=("n1",)
        )
        exposure = make_exposure(
            level=Urgency.MODERATE, score=0.5, confidence=0.8, routes=(route,)
        )
        # is_new_event=False so the reason reflects the user's own change
        # rather than defaulting to NEW_EVENT.
        result = engine.evaluate(
            ctx(user, presentation, exposure=exposure, version=2, is_new_event=False)
        )
        assert result.candidate.reason is NotificationReason.ROUTE_CHANGE

    def test_candidate_carries_the_presentation(self, engine, user, presentation):
        result = engine.evaluate(ctx(user, presentation))
        assert result.candidate.presentation == tuple(presentation)

    def test_evidence_ids_are_carried_over(self, engine, user, presentation):
        result = engine.evaluate(ctx(user, presentation))
        assert "obs_1" in result.candidate.evidence_ids

    def test_escalation_changes_the_headline(self, engine, user, presentation):
        high = make_exposure(level=Urgency.IMMEDIATE, score=0.9, confidence=0.9)
        result = engine.evaluate(
            ctx(
                user,
                presentation,
                exposure=high,
                previous_exposure_level=Urgency.LOW,
                is_new_event=False,
            )
        )
        assert result.candidate.headline.lower().startswith("escalated")

    def test_local_hour_is_in_the_region_timezone(self, engine):
        # 20:00 UTC is 13:00 in Seattle (PDT).
        utc_noon = datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
        assert engine.local_hour(utc_noon) == 13
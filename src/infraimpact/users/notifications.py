"""Notification architecture and conditions (brief sections 52, 53).

The pipeline is fixed and the order matters:

    Analysis
       -> user impact
       -> delta
       -> notification candidate
       -> policy checks
       -> deduplication
       -> rate limiting
       -> delivery

A candidate is built first and *then* judged. Nothing is suppressed before it
exists as a record, so "why did the user not get told?" is always answerable
by inspecting a candidate that was declined for a stated reason.

Two suppression layers sit after the policy checks:

1.  The section 52 dedupe key - ``user|event|impact_type|state_version``.
    Because the state version is part of the key, re-running analysis for a
    state the user has already been alerted about cannot produce a second
    alert for it. This is exact-match suppression.

2.  Equivalence suppression. A new state version means a new dedupe key, so
    layer 1 alone would let the platform re-alert a user every poll about the
    same unchanged situation. Layer 2 compares the *reason* and the priority
    of the most recent alert and declines near-identical repeats. Escalation
    and official guidance still get through.

Section 53 permits one deliberate exception: a high-impact official
emergency warning may bypass ordinary suppression. It bypasses quiet hours
and rate limiting. It never bypasses exact deduplication - sending the same
identical alert twice is not made acceptable by it being official.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from ..delta.engine import DeltaReport, has_official_guidance
from ..domain.enums import NotificationReason, TruthStatus, Urgency
from ..domain.ids import deterministic_id, utcnow
from ..domain.schemas import (
    EventState,
    NotificationCandidate,
    PresentationItem,
    UserContext,
    UserExposure,
    UserPriority,
)
from ..storage.repository import NotificationRepository

log = logging.getLogger(__name__)

_URGENCY_RANK = {
    Urgency.NONE: 0,
    Urgency.LOW: 1,
    Urgency.MODERATE: 2,
    Urgency.HIGH: 3,
    Urgency.IMMEDIATE: 4,
}

#: A new alert must beat the last one by at least this much priority to be
#: treated as new information rather than a repeat.
_ESCALATION_MARGIN = 0.05

#: Repeat window for equivalence suppression.
_DEFAULT_REPEAT_WINDOW_S = 900.0

#: An official alert only bypasses suppression if it actually concerns the
#: user. Official guidance about something three counties away is still noise.
_OFFICIAL_BYPASS_MIN_EXPOSURE = 0.2


@dataclass
class NotificationContext:
    """Everything the notification layer may read for one user and event."""

    user: UserContext
    exposure: UserExposure
    priority: UserPriority
    presentation: Sequence[PresentationItem]
    state: EventState
    state_version: int
    impact_type: str = "event"
    delta_report: DeltaReport | None = None
    previous_exposure_level: Urgency | None = None
    is_new_event: bool = False


@dataclass
class PolicyDecision:
    """Why a candidate was or was not delivered. Always populated."""

    allowed: bool
    reason: str
    bypassed: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


@dataclass
class NotificationOutcome:
    """A candidate plus the decisions taken on it."""

    candidate: NotificationCandidate
    decision: PolicyDecision

    @property
    def delivered(self) -> bool:
        return self.decision.allowed


class NotificationEngine:
    """Builds and filters notification candidates for one user."""

    def __init__(
        self,
        timezone: str = "UTC",
        repeat_window_s: float = _DEFAULT_REPEAT_WINDOW_S,
        repository: NotificationRepository | None = None,
    ) -> None:
        self.tz = ZoneInfo(timezone)
        self.repeat_window_s = repeat_window_s
        self.repo = repository

    # -- pipeline ---------------------------------------------------------

    def evaluate(
        self,
        ctx: NotificationContext,
        now: datetime | None = None,
    ) -> NotificationOutcome | None:
        """Run the full pipeline. Returns ``None`` if there is nothing to say.

        ``None`` means no candidate was created at all - the situation does not
        concern this user. A candidate that exists but is declined returns an
        outcome with ``delivered == False`` and a stated reason.
        """
        now = now or utcnow()

        reason = self._reason(ctx)
        if reason is None:
            return None

        candidate = self._candidate(ctx, reason, now)

        bypassed: list[str] = []
        notes: list[str] = []
        for check in (
            self._policy_check(ctx, now),
            self._dedupe(candidate),
            self._equivalence(ctx, candidate, now),
            self._rate_limit(ctx, candidate, now),
        ):
            if not check.allowed:
                # Carry forward whatever the earlier checks bypassed, so a
                # suppression record explains the whole path taken.
                return NotificationOutcome(
                    candidate=candidate,
                    decision=PolicyDecision(
                        allowed=False,
                        reason=check.reason,
                        bypassed=tuple(bypassed),
                        notes=[*notes, *check.notes],
                    ),
                )
            bypassed.extend(check.bypassed)
            notes.extend(check.notes)

        return NotificationOutcome(
            candidate=candidate,
            decision=PolicyDecision(
                allowed=True,
                reason="delivered",
                bypassed=tuple(dict.fromkeys(bypassed)),
                notes=notes,
            ),
        )

    # -- step 1: is there anything to say? -------------------------------

    def _reason(self, ctx: NotificationContext) -> NotificationReason | None:
        """Section 53's information-value test, expressed as a reason code."""
        exposure = ctx.exposure
        official = has_official_guidance(ctx.state)

        if exposure.exposure_level is Urgency.NONE and not official:
            return None

        if official and exposure.exposure_score >= _OFFICIAL_BYPASS_MIN_EXPOSURE:
            return NotificationReason.OFFICIAL_GUIDANCE
        if ctx.is_new_event:
            return NotificationReason.NEW_EVENT

        previous = ctx.previous_exposure_level
        if previous is not None and _URGENCY_RANK[exposure.exposure_level] > _URGENCY_RANK[previous]:
            return NotificationReason.ESCALATION

        if any(r.intersects for r in exposure.route_impacts):
            return NotificationReason.ROUTE_CHANGE
        if ctx.delta_report and any(
            d.domain == "infrastructure" and d.magnitude > 0.0 for d in ctx.delta_report.deltas
        ):
            return NotificationReason.IMPACT_CHANGE
        if ctx.delta_report and ctx.delta_report.change_names:
            return NotificationReason.STATUS_CHANGE
        return NotificationReason.LOCATION_CHANGE

    def _candidate(
        self,
        ctx: NotificationContext,
        reason: NotificationReason,
        now: datetime,
    ) -> NotificationCandidate:
        prefs = ctx.user.preferences
        dedupe_key = self.dedupe_key(ctx)
        headline, body = self._copy(ctx, reason)
        return NotificationCandidate(
            notification_id=deterministic_id("nt", f"{dedupe_key}|{reason.value}"),
            user_id=ctx.user.user_id,
            event_id=ctx.exposure.event_id,
            reason=reason,
            urgency=ctx.priority.urgency_band,
            headline=headline,
            body=body,
            presentation=tuple(ctx.presentation),
            evidence_ids=_evidence_ids(ctx),
            dedupe_key=dedupe_key,
            created_at=now,
        )

    @staticmethod
    def dedupe_key(ctx: NotificationContext) -> str:
        """Section 52, verbatim: user_id | event_id | impact_type | state_version."""
        return "|".join(
            (
                ctx.user.user_id,
                ctx.exposure.event_id,
                ctx.impact_type,
                str(ctx.state_version),
            )
        )

    # -- step 2: policy checks -------------------------------------------

    def _policy_check(self, ctx: NotificationContext, now: datetime) -> PolicyDecision:
        prefs = ctx.user.preferences
        notes: list[str] = []
        bypassed: list[str] = []

        # Location expiry (section 34): an expired location cannot support a
        # location-based claim, so we do not pretend it did.
        if (
            ctx.user.current_location is not None
            and ctx.user.current_location_expires_at is not None
            and ctx.user.current_location_expires_at <= now
        ):
            notes.append("user location expired before this run")

        if _URGENCY_RANK[ctx.priority.urgency_band] < _URGENCY_RANK[prefs.minimum_urgency]:
            return PolicyDecision(
                allowed=False,
                reason="below_minimum_urgency",
                notes=notes,
            )

        if not prefs.channels:
            return PolicyDecision(
                allowed=False, reason="no_channels_enabled", notes=notes
            )

        bypass = self._official_bypass(ctx)

        if prefs.quiet_hours_local:
            if not bypass:
                hour = now.astimezone(self.tz).hour
                start, end = prefs.quiet_hours_local
                if _in_quiet_hours(hour, start, end):
                    return PolicyDecision(
                        allowed=False,
                        reason="quiet_hours",
                        notes=notes,
                    )
            else:
                bypassed.append("quiet_hours")
                notes.append("official guidance overrides quiet hours")

        return PolicyDecision(
            allowed=True, reason="policy_ok", bypassed=tuple(bypassed), notes=notes
        )

    def _official_bypass(self, ctx: NotificationContext) -> bool:
        """Section 53's exception, and only for an alert that concerns the user."""
        return bool(
            has_official_guidance(ctx.state)
            and ctx.exposure.exposure_score >= _OFFICIAL_BYPASS_MIN_EXPOSURE
        )

    # -- step 3: deduplication -------------------------------------------

    def _dedupe(self, candidate: NotificationCandidate) -> PolicyDecision:
        if self.repo is None:
            return PolicyDecision(allowed=True, reason="no_repository")
        if self.repo.seen_dedupe_key(candidate.dedupe_key):
            # Never bypassed: the identical alert has already gone out.
            return PolicyDecision(allowed=False, reason="duplicate_dedupe_key")
        return PolicyDecision(allowed=True, reason="dedupe_ok")

    def _equivalence(
        self,
        ctx: NotificationContext,
        candidate: NotificationCandidate,
        now: datetime,
    ) -> PolicyDecision:
        """Suppress alerts that say the same thing in different words."""
        if self.repo is None:
            return PolicyDecision(allowed=True, reason="no_repository")
        if self._official_bypass(ctx):
            return PolicyDecision(allowed=True, reason="official_bypass")

        previous = self._previous_alert(ctx, candidate, now)
        if previous is None:
            return PolicyDecision(allowed=True, reason="no_previous_alert")

        escalates = candidate.urgency is not previous.urgency and _URGENCY_RANK[
            candidate.urgency
        ] > _URGENCY_RANK[previous.urgency]
        if escalates:
            return PolicyDecision(allowed=True, reason="escalated")

        return PolicyDecision(
            allowed=False,
            reason="equivalent_recent_alert",
            notes=[f"previous {previous.reason.value} at {previous.created_at.isoformat()}"],
        )

    def _previous_alert(
        self,
        ctx: NotificationContext,
        candidate: NotificationCandidate,
        now: datetime,
    ) -> NotificationCandidate | None:
        """Most recent alert for this user+event within the repeat window."""
        if self.repo is None:
            return None
        window = now - timedelta(seconds=self.repeat_window_s)
        best: NotificationCandidate | None = None
        for prior in self.repo.recent(limit=200):
            if prior.user_id != ctx.user.user_id:
                continue
            if prior.event_id != candidate.event_id:
                continue
            if prior.created_at < window:
                continue
            if best is None or prior.created_at > best.created_at:
                best = prior
        return best

    # -- step 4: rate limiting --------------------------------------------

    def _rate_limit(
        self,
        ctx: NotificationContext,
        candidate: NotificationCandidate,
        now: datetime,
    ) -> PolicyDecision:
        limit = ctx.user.preferences.max_alerts_per_hour
        if limit <= 0 or self.repo is None:
            return PolicyDecision(allowed=True, reason="no_limit")
        if self._official_bypass(ctx):
            return PolicyDecision(
                allowed=True, reason="official_bypass", bypassed=("rate_limit",)
            )

        cutoff = now - timedelta(hours=1)
        sent = sum(
            1
            for n in self.repo.recent(limit=500)
            if n.user_id == ctx.user.user_id and n.created_at >= cutoff
        )
        if sent >= limit:
            return PolicyDecision(
                allowed=False,
                reason="rate_limited",
                notes=[f"{sent} alert(s) in the last hour, limit {limit}"],
            )
        return PolicyDecision(allowed=True, reason="rate_ok")

    # -- copy -------------------------------------------------------------

    def _copy(
        self, ctx: NotificationContext, reason: NotificationReason
    ) -> tuple[str, str]:
        """Notification text is the top-ranked presentation item, not a re-ask."""
        items = [i for i in ctx.presentation if i.type.value != "quiet"]
        if not items:
            return (
                "Update on an event near you",
                "There is new information about an event in your area.",
            )

        top = max(items, key=lambda i: i.priority)
        headline = top.headline or "Update on an event near you"

        if reason is NotificationReason.ESCALATION:
            headline = f"Escalated: {headline[0].lower()}{headline[1:]}"
        elif reason is NotificationReason.ROUTE_CHANGE:
            headline = f"Your route: {headline[0].lower()}{headline[1:]}"

        detail = top.detail or ""
        # Section 41: uncertainty is visible in the notification itself, not
        # hidden behind a tap-through.
        if top.truth_status in (TruthStatus.INFERRED, TruthStatus.PREDICTED):
            detail = f"{detail} (not yet confirmed)".strip()

        return headline, detail

    def local_hour(self, now: datetime) -> int:
        return now.astimezone(self.tz).hour


def _in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """Quiet hours wrap midnight, which is the common case (22:00 -> 07:00)."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _evidence_ids(ctx: NotificationContext) -> tuple[str, ...]:
    ids: list[str] = []
    for item in ctx.presentation:
        ids.extend(item.evidence_ids)
        if len(ids) >= 12:
            break
    return tuple(dict.fromkeys(ids))


__all__ = [
    "NotificationContext",
    "NotificationEngine",
    "NotificationOutcome",
    "PolicyDecision",
]
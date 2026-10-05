"""Event lifecycle: deciding whether something is still happening.

This is the module that answers "has it ended?", and it is deliberately the
hardest thing in the world-state system to get right, because the failure modes
are asymmetric in a way that is easy to overlook.

Closing an event that is still happening is far worse than leaving a finished
one open. A closed event drops out of every active view, stops being
reconstructed, and quietly stops being routed around - so the platform would
stop warning people about a live situation while looking exactly as healthy as
it does when everything is working. A stale open event costs some wasted work and
a stale card. The system is built so the expensive mistake is the one it will
not make: anything it cannot positively evidence as finished stays open, and
the reason it stayed open is recorded alongside the verdict.

Nothing here knows what kind of event it is judging. There is no protest
lifecycle, no fire lifecycle, no earthquake lifecycle. There are three kinds of
evidence that a thing has ended, and they apply equally to a road closure, a
wildfire and a stadium crowd:

    an official record saying so      the authority responsible says it is over
    a resolution record               the disruption was retired - reopened,
                                      restored, cleared, expired
    extended silence                  nothing has been reported for much longer
                                      than this event's own reporting cadence
                                      says is normal

The third is the one that needs care, because "how long is too long" is not a
constant. A four-minute lane closure and a three-day wildfire are both quiet
sometimes. A fixed threshold is either too eager, and closes a fire overnight,
or too lax, and leaves a reopened road on the board for a week. So the threshold
is derived from the event's own observation cadence: the median gap between its
own reports, scaled. That is a fact about *this* event's reporting behaviour
rather than an assumption about its type, which is why it works without knowing
the type.

Closure from silence is graded rather than absolute. Silence past the quiescence
threshold makes an event quiescent - still tracked, no longer assumed live.
Silence past twice that, with enough observations to have established a cadence
at all, closes it with a confidence that says plainly it was inferred from
absence of reports rather than from a record saying so.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..domain.enums import Authority, ObservationType
from ..domain.ids import ensure_utc, utcnow
from ..domain.schemas import LifecycleAssessment, Observation

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Termination signals
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminationSignal:
    """A declarative "this is over" rule.

    Kept as data rather than code because these come from the vocabulary of
    published feeds, which is large, unstable, and event-type agnostic in
    exactly the way hardcoded logic is not. A new closure phrase is a new row
    here, not a new branch in an ``if``.
    """

    name: str
    #: Payload keys inspected for this signal.
    fields: tuple[str, ...]
    #: Value fragments that assert the thing has ended.
    tokens: tuple[str, ...]
    #: Whether the record must come from an official authority to be believed.
    requires_official: bool = False
    #: Whether this is strong enough on its own to close the event.
    terminates: bool = True


#: Phrases that mean a disruption has been retired. Every one of these is used
#: by some real feed to mean "this is over", and none of them is ambiguous in
#: that direction: "restored" never means "still broken".
RESOLUTION_TOKENS: tuple[str, ...] = (
    "reopen",
    "re-open",
    "restored",
    "resumed",
    "cleared",
    "resolved",
    "all clear",
    "all-clear",
    "back in service",
    "returned to service",
    "normal operations",
    "returned to normal",
    "lifted",
    "expired",
    "cancelled",
    "canceled",
    "recovered",
)

#: Phrases that mean the situation itself has concluded, as distinct from a
#: single disruption being retired.
END_TOKENS: tuple[str, ...] = (
    "dispersed",
    "disbanded",
    "concluded",
    "has ended",
    "have ended",
    "is over",
    "are over",
    "stood down",
    "de-escalated",
    "deescalated",
)

#: Payload keys that carry status across the feeds in the catalogue.
STATUS_FIELDS: tuple[str, ...] = (
    "status",
    "closure_type",
    "alert_type",
    "outage_type",
    "event_status",
    "state",
    "condition",
    "phase",
    "disposition",
)

#: Free-text keys, searched when no structured status field exists.
TEXT_FIELDS: tuple[str, ...] = (
    "headline",
    "description",
    "message",
    "summary",
    "title",
    "event",
)

TERMINATION_SIGNALS: tuple[TerminationSignal, ...] = (
    TerminationSignal(
        name="resolution",
        fields=STATUS_FIELDS + TEXT_FIELDS,
        tokens=RESOLUTION_TOKENS,
    ),
    TerminationSignal(
        name="concluded",
        fields=TEXT_FIELDS,
        tokens=END_TOKENS,
        # A news outlet saying a crowd dispersed is worth recording. Only an
        # authority is allowed to *end* the event on it, because an unverified
        # report of dispersal during a live police operation is exactly the kind
        # of thing that should not silence the platform.
        requires_official=True,
    ),
)

#: Observation types whose arrival past their own expiry means the event is over.
EXPIRING_TYPES = frozenset(
    {
        ObservationType.OFFICIAL_EMERGENCY_NOTICE,
        ObservationType.SEVERE_WEATHER,
        ObservationType.TRANSIT_SERVICE_ALERT,
        ObservationType.POWER_OUTAGE,
        ObservationType.WATER_OUTAGE,
    }
)

_EXPIRY_FIELDS = ("expires_at", "expires", "ends_at", "valid_until", "end_time")


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@dataclass
class LifecyclePolicy:
    """Tunable, and deliberately not per-event-type.

    The defaults are chosen so that an event with no established reporting
    cadence falls back to a conservative silence window: with one observation
    there is no cadence to derive anything from, and the correct response to
    "I have exactly one data point and then nothing" is to assume it might still
    be happening.
    """

    #: Silence below this is never enough, whatever the cadence says.
    min_silence_seconds: float = 900.0
    #: Silence is judged against this multiple of the event's own median gap.
    cadence_multiplier: float = 8.0
    #: Ceiling, so a sparse feed cannot hold every event open indefinitely.
    max_silence_seconds: float = 21600.0
    #: Multiple of the silence threshold at which closure is inferred.
    close_silence_multiple: float = 2.0
    #: Observations needed before silence means anything at all.
    min_observations_for_silence: int = 3
    #: Confidence assigned to a silence-inferred closure, before adjustment.
    silence_closure_confidence: float = 0.6
    #: Assigned when an official record ends it.
    official_confidence: float = 0.95
    #: Assigned when a resolution record retires the last disruption.
    resolution_confidence: float = 0.9
    #: Assigned to the quiescent (watching, not assuming live) state.
    quiescent_confidence: float = 0.7

    def silence_threshold(
        self, gaps: Sequence[float], observation_count: int
    ) -> tuple[float, str]:
        """Derive this event's own silence threshold.

        Returns the threshold and a human-readable account of how it was
        derived, which is stored on the assessment. A threshold nobody can
        explain is a threshold nobody should trust to close an event.

        The two cases are deliberately different, and conflating them was the
        bug. With a cadence, the threshold follows that cadence. Without one,
        there is no evidence that reports were *expected* at all, so the only
        defensible threshold is a long one: the absence of reports is not
        evidence of absence unless something said there would be reports. Using
        the short floor in that case made every freshly-opened event go
        quiescent twenty minutes after its first sighting, purely because the
        floor was tuned for events that had established a rhythm.
        """

        usable = [g for g in gaps if g > 0]
        if len(usable) < 2 or observation_count < self.min_observations_for_silence:
            return (
                self.max_silence_seconds,
                f"no established cadence ({observation_count} observations, "
                f"{len(usable)} gaps); absent evidence that reports were expected, "
                f"using the conservative ceiling {self.max_silence_seconds:.0f}s",
            )
        usable.sort()
        median = usable[len(usable) // 2]
        derived = max(self.min_silence_seconds, min(self.max_silence_seconds, median * self.cadence_multiplier))
        return (
            derived,
            f"median report gap {median:.0f}s x {self.cadence_multiplier:.0f} "
            f"= {derived:.0f}s (floor {self.min_silence_seconds:.0f}s, "
            f"cap {self.max_silence_seconds:.0f}s)",
        )


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class LifecycleEngine:
    """Decides an event's status from evidence, never from a hardcoded type."""

    def __init__(self, policy: LifecyclePolicy | None = None) -> None:
        self.policy = policy or LifecyclePolicy()

    # -- public -----------------------------------------------------------

    def assess(
        self,
        event_id: str,
        observations: Sequence[Observation],
        *,
        now: datetime | None = None,
        had_impacts: bool = False,
        unresolved_impacts: Sequence[str] = (),
        previous: LifecycleAssessment | None = None,
    ) -> LifecycleAssessment:
        """Produce the status, its basis, and the evidence behind it."""

        now = now or utcnow()

        if not observations:
            return LifecycleAssessment(
                status="candidate",
                reason="no observations linked to this event yet",
                confidence=0.0,
                termination_basis="unknown",
                assessed_at=now,
            )

        ordered = sorted(observations, key=lambda o: (o.event_time, o.observation_id))
        gaps = self._gaps(ordered)
        threshold, derivation = self.policy.silence_threshold(gaps, len(ordered))

        last_seen = max(ensure_utc(o.observed_at) for o in ordered)
        silence = max(0.0, (now - last_seen).total_seconds())
        quiet_since = (
            previous.quiet_since if previous and previous.quiet_since else last_seen
        )
        if silence > 0 and previous is None:
            quiet_since = last_seen

        official_release = self._official_release(ordered, now)
        if official_release is not None:
            obs_id, why = official_release
            return LifecycleAssessment(
                status="closed",
                reason=f"official record reports the event has ended ({why})",
                confidence=self.policy.official_confidence,
                termination_basis="official_release",
                evidence_observation_ids=(obs_id,),
                quiet_since=quiet_since,
                assessed_at=now,
                silence_threshold_seconds=round(threshold, 1),
            )

        resolution = self._resolution(ordered)
        if had_impacts and not unresolved_impacts and resolution is not None:
            obs_id, why = resolution
            return LifecycleAssessment(
                status="closed",
                reason=(
                    f"every reported disruption retired by a resolution record "
                    f"({why}); {len(ordered)} observations, last {silence:.0f}s ago"
                ),
                confidence=self.policy.resolution_confidence,
                termination_basis="resolution_record",
                evidence_observation_ids=(obs_id,),
                quiet_since=quiet_since,
                assessed_at=now,
                silence_threshold_seconds=round(threshold, 1),
            )

        if resolution is not None and not had_impacts:
            # The event's only content was a disruption that has since been
            # retired. There is nothing left to track.
            obs_id, why = resolution
            return LifecycleAssessment(
                status="closed",
                reason=f"the reported disruption was retired ({why})",
                confidence=self.policy.resolution_confidence * 0.9,
                termination_basis="resolution_record",
                evidence_observation_ids=(obs_id,),
                quiet_since=quiet_since,
                assessed_at=now,
                silence_threshold_seconds=round(threshold, 1),
            )

        close_at = threshold * self.policy.close_silence_multiple
        if (
            len(ordered) >= self.policy.min_observations_for_silence
            and silence >= close_at
        ):
            return LifecycleAssessment(
                status="closed",
                reason=(
                    f"no new information for {silence:.0f}s, which is "
                    f"{silence / threshold:.1f}x this event's derived silence "
                    f"threshold of {threshold:.0f}s ({derivation}); inferred "
                    f"from absence of reports, not from a record"
                ),
                confidence=self._silence_confidence(silence, threshold),
                termination_basis="silence",
                evidence_observation_ids=(),
                quiet_since=quiet_since,
                assessed_at=now,
                silence_threshold_seconds=round(threshold, 1),
            )

        if silence >= threshold:
            return LifecycleAssessment(
                status="quiescent",
                reason=(
                    f"no new information for {silence:.0f}s, at or past the "
                    f"derived silence threshold of {threshold:.0f}s "
                    f"({derivation}); still tracked, no longer assumed live"
                ),
                confidence=self.policy.quiescent_confidence,
                termination_basis="silence",
                evidence_observation_ids=(),
                quiet_since=quiet_since,
                assessed_at=now,
                silence_threshold_seconds=round(threshold, 1),
            )

        return LifecycleAssessment(
            status="active",
            reason=(
                f"{len(ordered)} observations, last {silence:.0f}s ago, inside "
                f"the derived silence threshold of {threshold:.0f}s ({derivation})"
            ),
            confidence=round(min(1.0, 0.5 + 0.1 * len(ordered)), 4),
            termination_basis="unknown",
            evidence_observation_ids=(ordered[-1].observation_id,),
            quiet_since=quiet_since,
            assessed_at=now,
            silence_threshold_seconds=round(threshold, 1),
        )

    # -- evidence detectors ------------------------------------------------

    def _official_release(
        self, observations: Sequence[Observation], now: datetime
    ) -> tuple[str, str] | None:
        """An authority saying the situation itself has ended.

        Checks structured expiry first, because a published ``expires_at`` is a
        stronger statement than any prose: it is the authority committing in
        advance that this will not apply past that moment.
        """

        for obs in observations:
            if obs.observation_type not in EXPIRING_TYPES:
                continue
            expired = self._expired_at(obs)
            if expired is not None and expired <= now:
                return obs.observation_id, f"{obs.observation_type} expired at {expired.isoformat()}"
            if expired is not None and expired > now:
                # Still inside its published validity window. That is an active
                # official position and outranks any silence-based reasoning.
                return None

        for obs in observations:
            if obs.provenance.authority is not Authority.OFFICIAL:
                continue
            for signal in TERMINATION_SIGNALS:
                if not signal.requires_official:
                    continue
                hit = self._match(obs, signal)
                if hit is not None:
                    return obs.observation_id, f"{signal.name}: {hit}"
        return None

    def _resolution(self, observations: Sequence[Observation]) -> tuple[str, str] | None:
        """The most recent record retiring a previously reported disruption."""

        best: tuple[str, str] | None = None
        for obs in observations:
            for signal in TERMINATION_SIGNALS:
                if signal.requires_official:
                    continue
                hit = self._match(obs, signal)
                if hit is not None:
                    best = (obs.observation_id, f"{signal.name}: {hit}")
        return best

    def _expired_at(self, obs: Observation) -> datetime | None:
        from ..domain.ids import parse_time

        payload = obs.structured_payload or {}
        for field_name in _EXPIRY_FIELDS:
            if field_name in payload:
                parsed = parse_time(payload.get(field_name))
                if parsed is not None:
                    return parsed
        return None

    @staticmethod
    def _match(obs: Observation, signal: TerminationSignal) -> str | None:
        """First token of ``signal`` present in any of ``signal``'s fields.

        Matching is on word-fragment substrings of the lowercased value rather
        than whole-token equality, because published feeds use compound forms
        ("Reopened-After-Delay", "ROAD_CLOSED_RESTORED") that token equality
        misses. Substring matching is safe here specifically because every
        token is an assertion of ending: there is no benign value containing
        "reopen" that means the opposite.
        """

        haystacks: list[str] = []
        payload = obs.structured_payload or {}
        for field_name in signal.fields:
            value = payload.get(field_name)
            if value is not None:
                haystacks.append(str(value))
        if obs.headline:
            haystacks.append(obs.headline)

        for haystack in haystacks:
            lowered = haystack.lower()
            for token in signal.tokens:
                if token in lowered:
                    return f"'{token}' in '{haystack.strip()[:80]}'"
        return None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _gaps(ordered: Sequence[Observation]) -> list[float]:
        """Inter-observation gaps in *event* time, in seconds.

        Event time rather than observation time, deliberately. This is the
        cadence of the world being reported, not the cadence of our polling. A
        snapshot feed ingested in one burst has near-zero observation-time gaps
        and would otherwise imply that any event it contains is expected to be
        silent almost immediately.
        """

        stamps = [ensure_utc(o.event_time) for o in ordered]
        return [
            (b - a).total_seconds()
            for a, b in zip(stamps, stamps[1:], strict=False)
            if (b - a).total_seconds() > 0
        ]

    def _silence_confidence(self, silence: float, threshold: float) -> float:
        """Confidence for a closure inferred from absence of reports.

        Rises with how far past the threshold the silence runs, and is capped
        below the official basis. A closure nobody announced is always less
        certain than a closure somebody announced, and the number says so.
        """

        overshoot = max(0.0, silence / max(1.0, threshold) - self.policy.close_silence_multiple)
        boosted = min(0.85, self.policy.silence_closure_confidence + 0.1 * overshoot)
        return round(boosted, 4)


# --------------------------------------------------------------------------
# Legacy predicate kept for the infrastructure layer
# --------------------------------------------------------------------------

_RESOLUTION_PATTERN = re.compile(
    "|".join(re.escape(t) for t in (*RESOLUTION_TOKENS, *END_TOKENS)), re.IGNORECASE
)


def mentions_resolution(payload: Any, headline: str | None = None) -> bool:
    """Whether a record retires a previously reported disruption.

    Used by the world-state engine when deciding whether an incoming record
    supersedes an earlier one for the same asset. Same declarative token table
    as the lifecycle engine, so the two cannot drift apart on what "reopened"
    means.
    """

    text = " ".join(
        [str(payload or ""), headline or ""]
    )
    return bool(_RESOLUTION_PATTERN.search(text))


__all__ = [
    "END_TOKENS",
    "RESOLUTION_TOKENS",
    "STATUS_FIELDS",
    "TERMINATION_SIGNALS",
    "LifecycleAssessment",
    "LifecycleEngine",
    "LifecyclePolicy",
    "TerminationSignal",
    "mentions_resolution",
]

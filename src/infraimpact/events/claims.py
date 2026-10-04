"""Claim extraction (brief section 11).

Raw observations are decomposed into atomic claims so disagreement is
preserved rather than averaged away: Source A says crowd 300, Source B says
700, and both claims survive until later analysis resolves them.

Two extractors exist behind one interface:
  * ``RuleClaimExtractor``  - deterministic, structured-payload aware
  * ``LlmClaimExtractor``   - schema-constrained LLM extraction for prose

The rule extractor is not a fallback that "guesses less"; it is the correct
path for machine-readable feeds, which need no interpretation.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Iterable

from ..domain.enums import Authority, ObservationType, TruthStatus
from ..domain.ids import deterministic_id
from ..domain.schemas import Claim, Observation
from ..llm.client import LlmClient, NullLlmClient

log = logging.getLogger(__name__)

#: predicate -> human label, used by the evidence panel and "Why?" view.
PREDICATE_LABELS: dict[str, str] = {
    "event.gathering": "public gathering reported",
    "event.movement": "event is moving",
    "event.movement_direction": "movement direction",
    "event.estimated_crowd": "estimated participation",
    "event.footprint": "event footprint",
    "event.route": "planned route",
    "event.duration_expected": "expected duration",
    "event.organized": "organised event",
    "event.status": "event status",
    "road.closure": "road closure",
    "road.reopened": "road reopened",
    "road.affected": "road affected",
    "transit.service_alert": "transit service alert",
    "transit.affected": "transit affected",
    "transit.status": "transit status",
    "utility.power_outage": "power outage",
    "utility.water_outage": "water outage",
    "emergency.official_guidance": "official emergency guidance",
    "emergency.dispatch": "emergency dispatch",
    "police.response": "police response",
    "hazard.severity": "hazard severity",
    "hazard.magnitude": "hazard magnitude",
    "hazard.depth": "hazard depth",
    "hazard.tsunami": "tsunami flag",
    "location.name": "location name",
}

DIRECTION_TERMS = {
    "north": 0.0,
    "northeast": 45.0,
    "east": 90.0,
    "southeast": 135.0,
    "south": 180.0,
    "southwest": 225.0,
    "west": 270.0,
    "northwest": 315.0,
}


class ClaimExtractor(ABC):
    @abstractmethod
    def extract(self, observation: Observation, event_id: str | None) -> list[Claim]: ...

    @staticmethod
    def _claim(
        observation: Observation,
        event_id: str | None,
        predicate: str,
        value: Any,
        *,
        method: str = "rule",
        confidence: float = 1.0,
        truth_status: TruthStatus | None = None,
        unit: str | None = None,
    ) -> Claim:
        return Claim(
            claim_id=deterministic_id(
                "clm", observation.observation_id, predicate, repr(value)
            ),
            observation_id=observation.observation_id,
            event_id=event_id,
            predicate=predicate,
            value=value,
            unit=unit,
            geometry=observation.geometry,
            valid_from=observation.event_time,
            extraction_method=method,  # type: ignore[arg-type]
            extraction_confidence=confidence,
            source_id=observation.source_id,
            truth_status=truth_status or _default_truth(observation),
        )


def _default_truth(observation: Observation) -> TruthStatus:
    """Section 41. Official structured data is CONFIRMED; media is REPORTED."""
    # Authority lives on provenance, not on the observation itself.
    authority = observation.provenance.authority
    if authority is Authority.OFFICIAL and observation.source_type.value.startswith(
        "official"
    ):
        return TruthStatus.CONFIRMED
    if authority is Authority.ESTABLISHED_MEDIA:
        return TruthStatus.REPORTED
    return TruthStatus.REPORTED


class RuleClaimExtractor(ClaimExtractor):
    """Deterministic extraction from structured payloads and light prose cues."""

    def extract(self, observation: Observation, event_id: str | None) -> list[Claim]:
        handler = {
            ObservationType.PERMIT_EVENT: self._permit,
            ObservationType.POLICE_RESPONSE: self._police,
            ObservationType.FIRE_DISPATCH: self._fire,
            ObservationType.NEWS_ARTICLE: self._news,
            ObservationType.ROAD_CLOSURE: self._road,
            ObservationType.ROAD_CONSTRUCTION: self._road,
            ObservationType.TRANSIT_SERVICE_ALERT: self._transit,
            ObservationType.TRANSIT_DELAY: self._transit,
            ObservationType.POWER_OUTAGE: self._power,
            ObservationType.WATER_OUTAGE: self._power,
            ObservationType.OFFICIAL_EMERGENCY_NOTICE: self._emergency,
            ObservationType.SEVERE_WEATHER: self._emergency,
            ObservationType.EARTHQUAKE: self._earthquake,
        }.get(observation.observation_type, self._generic)

        claims = handler(observation, event_id)
        return [c for c in claims if c is not None]

    # -- per-type extractors ---------------------------------------------

    def _permit(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        claims = [
            self._claim(o, e, "event.gathering", True, truth_status=TruthStatus.CONFIRMED),
            self._claim(o, e, "event.organized", True, truth_status=TruthStatus.CONFIRMED),
            self._claim(o, e, "event.route", p.get("route")),
            self._claim(o, e, "event.duration_expected", p.get("expected_duration_min"), unit="minutes"),
        ]
        if p.get("location"):
            claims.append(self._claim(o, e, "location.name", p["location"]))
        return claims

    def _police(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        claims = [
            self._claim(o, e, "police.response", str(p.get("call_type") or "response").lower()),
            self._claim(o, e, "event.gathering", True, confidence=0.9),
        ]
        if p.get("estimated_crowd"):
            claims.append(
                self._claim(o, e, "event.estimated_crowd", float(p["estimated_crowd"]), confidence=0.6)
            )
        if p.get("units"):
            claims.append(self._claim(o, e, "police.units", float(p["units"])))
        detail = str(p.get("detail") or o.headline)
        claims.extend(self._movement_claims(o, e, detail))
        return claims

    def _fire(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        return [
            self._claim(o, e, "emergency.dispatch", str(p.get("incident_type") or "response").lower()),
            self._claim(o, e, "event.gathering", True, confidence=0.7),
        ]

    def _news(self, o: Observation, e: str | None) -> list[Claim]:
        text = f"{o.headline} {o.structured_payload.get('body', '')}"
        claims = [self._claim(o, e, "event.gathering", True, confidence=0.85)]
        claims.extend(self._movement_claims(o, e, text))
        if re.search(r"\b(dispersed|dispersed|departed|cleared)\b", text, re.I):
            claims.append(self._claim(o, e, "event.status", "dispersed", confidence=0.8))
        crowd = re.search(r"(?:about |roughly |an estimated |~)?(\d{3,6})\s+(?:people|protesters|participants)", text, re.I)
        if crowd:
            claims.append(
                self._claim(
                    o, e, "event.estimated_crowd", float(crowd.group(1)), confidence=0.5
                )
            )
        return claims

    def _road(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        closure_type = str(p.get("closure_type") or p.get("element") or "").lower()
        reopened = "reopen" in closure_type or "restored" in closure_type
        claims = [
            self._claim(
                o,
                e,
                "road.reopened" if reopened else "road.closure",
                True,
                truth_status=TruthStatus.CONFIRMED,
            ),
            self._claim(o, e, "road.affected", p.get("street") or p.get("location")),
        ]
        if p.get("reason"):
            claims.append(self._claim(o, e, "road.reason", str(p["reason"]).lower()))
        if p.get("severity"):
            claims.append(self._claim(o, e, "road.severity", str(p["severity"]).lower()))
        # An official road-event closure is a strong event-associated signal,
        # recorded as an association claim, not as a re-statement of the fact.
        if p.get("reason") and "special event" in str(p["reason"]).lower():
            claims.append(
                self._claim(o, e, "event.associated_disruption", True, confidence=0.8)
            )
        return claims

    def _transit(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        alert = str(p.get("alert_type") or p.get("status") or "").lower()
        restored = "restored" in alert or "resumed" in alert
        claims = [
            self._claim(
                o,
                e,
                "transit.status",
                "restored" if restored else "disrupted",
                truth_status=TruthStatus.CONFIRMED,
            )
        ]
        if p.get("route"):
            claims.append(self._claim(o, e, "transit.affected", str(p["route"])))
        if p.get("severity"):
            claims.append(self._claim(o, e, "transit.severity", str(p["severity"]).lower()))
        if o.geometry:
            claims.append(self._claim(o, e, "transit.affected", str(p.get("route") or "")))
        return claims

    def _power(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        predicate = (
            "utility.water_outage"
            if o.observation_type == ObservationType.WATER_OUTAGE
            else "utility.power_outage"
        )
        return [
            self._claim(o, e, predicate, True, truth_status=TruthStatus.CONFIRMED),
            self._claim(o, e, "utility.customers_affected", p.get("customers_affected")),
        ]

    def _emergency(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        claims = [
            self._claim(
                o,
                e,
                "emergency.official_guidance",
                str(p.get("headline") or p.get("event_name") or o.headline),
                truth_status=TruthStatus.CONFIRMED,
            ),
            self._claim(o, e, "hazard.severity", str(p.get("severity") or "unknown").lower()),
        ]
        if p.get("instruction"):
            # Official instruction text is preserved verbatim, never paraphrased.
            claims.append(self._claim(o, e, "emergency.instruction", str(p["instruction"])))
        return claims

    def _earthquake(self, o: Observation, e: str | None) -> list[Claim]:
        p = o.structured_payload
        claims = []
        if p.get("magnitude") is not None:
            claims.append(
                self._claim(o, e, "hazard.magnitude", float(p["magnitude"]), truth_status=TruthStatus.CONFIRMED)
            )
        if p.get("depth_km") is not None:
            claims.append(self._claim(o, e, "hazard.depth", float(p["depth_km"]), unit="km"))
        if p.get("tsunami_flag"):
            claims.append(self._claim(o, e, "hazard.tsunami", True, truth_status=TruthStatus.CONFIRMED))
        return claims

    def _generic(self, o: Observation, e: str | None) -> list[Claim]:
        return [
            self._claim(o, e, "location.name", o.headline or o.observation_type, confidence=0.5)
        ]

    # -- shared prose cues -------------------------------------------------

    def _movement_claims(self, o: Observation, e: str | None, text: str) -> list[Claim]:
        claims: list[Claim] = []
        moved = bool(
            re.search(r"\b(moving|moved|marching|marched|advancing|proceeding|left|arrived)\b", text, re.I)
        )
        if moved:
            claims.append(self._claim(o, e, "event.movement", True, confidence=0.8))
            direction = self._direction(text)
            if direction is not None:
                claims.append(
                    self._claim(o, e, "event.movement_direction", direction, confidence=0.75)
                )
        expansion = bool(
            re.search(r"\b(expand(ed|ing)|grew|spread|gathered|larger|bigger|more people)\b", text, re.I)
        )
        if expansion:
            claims.append(self._claim(o, e, "event.expansion", True, confidence=0.7))
        return claims

    @staticmethod
    def _direction(text: str) -> str | None:
        lowered = text.lower()
        for term, bearing in DIRECTION_TERMS.items():
            if re.search(rf"\b{term}(?:ward|wards)?\b", lowered):
                return term
        compass = re.search(r"\b(\d{1,3})\s*degrees?\b", text)
        if compass:
            return f"{compass.group(1)}deg"
        _ = DIRECTION_TERMS
        return None


class LlmClaimExtractor(ClaimExtractor):
    """Schema-constrained LLM extraction for prose (brief section 18/19).

    The LLM may only *structure* text a source already wrote. It may not
    introduce new facts; anything it returns is traceable to the input
    observation id.
    """

    def __init__(self, client: LlmClient | None = None) -> None:
        self.client = client or NullLlmClient()

    def extract(self, observation: Observation, event_id: str | None) -> list[Claim]:
        result = self.client.extract_claims(
            text=f"{observation.headline}\n{observation.structured_payload.get('body', '')}",
            observation_id=observation.observation_id,
            allowed_predicates=sorted(PREDICATE_LABELS),
        )
        claims: list[Claim] = []
        for item in result.get("claims", []):
            predicate = item.get("predicate")
            if not predicate or predicate not in PREDICATE_LABELS:
                continue
            claims.append(
                Claim(
                    claim_id=deterministic_id(
                        "clm", observation.observation_id, predicate, repr(item.get("value"))
                    ),
                    observation_id=observation.observation_id,
                    event_id=event_id,
                    predicate=predicate,
                    value=item.get("value"),
                    geometry=observation.geometry,
                    valid_from=observation.event_time,
                    extraction_method="llm",
                    extraction_confidence=float(item.get("confidence", 0.7)),
                    source_id=observation.source_id,
                    truth_status=TruthStatus.REPORTED,
                )
            )
        return claims


class HybridClaimExtractor(ClaimExtractor):
    """Deterministic extraction first, LLM second, both retained.

    Why union rather than either/or
    ------------------------------
    :class:`RuleClaimExtractor` is *correct* for machine-readable feeds: a permit
    feed states a route and a duration, and no model can read them more accurately
    than a dictionary lookup. It is *insufficient* for prose, where the crowd size
    and the direction of travel are in sentences no pattern will match reliably.

    Choosing between them loses something either way. LLM-only would make every
    structured feed's accuracy depend on a completion call, which is both slower
    and strictly worse; rules-only would mean the section-18 extraction capability
    never runs. So the union is taken, deduplicated on ``(predicate, value)``,
    and each claim keeps its own ``extraction_method`` and confidence.

    A conflict is *not* resolved here. If the rules read "estimated_crowd: 300"
    and the model reads 700, both claims are appended with different ids, and
    :class:`~infraimpact.events.world_state.WorldStateEngine` records the
    contradiction in ``EvidenceSummary.contradictions``. Averaging them here would
    destroy exactly the disagreement the evidence panel exists to show.
    """

    def __init__(self, client: LlmClient | None = None, rules: ClaimExtractor | None = None) -> None:
        self.rules = rules or RuleClaimExtractor()
        self.llm = LlmClaimExtractor(client)

    def extract(self, observation: Observation, event_id: str | None) -> list[Claim]:
        claims: list[Claim] = []
        seen: set[tuple[str, str]] = set()

        def keep(items: Iterable[Claim]) -> None:
            for claim in items:
                key = (claim.predicate, repr(claim.value))
                if key in seen:
                    continue
                seen.add(key)
                claims.append(claim)

        try:
            keep(self.rules.extract(observation, event_id))
        except Exception:  # noqa: BLE001 - rules are the floor, never the failure
            log.exception("rule claim extraction failed for %s", observation.observation_id)

        if _worth_interpretation(observation):
            try:
                keep(self.llm.extract(observation, event_id))
            except Exception:  # noqa: BLE001 - an LLM failure is not an extraction failure
                log.warning("llm claim extraction failed for %s", observation.observation_id)

        return claims


#: Observation types whose text is written by a human. Only these are handed to
#: the LLM: an agency JSON payload does not become more legible through
#: interpretation, and paying a completion per transit alert to rediscover
#: ``{"route": "8"}`` would be absurd.
_PROSE_TYPES = frozenset(
    {
        ObservationType.NEWS_ARTICLE,
        ObservationType.SEVERE_WEATHER,
        ObservationType.OFFICIAL_EMERGENCY_NOTICE,
    }
)


def _worth_interpretation(observation: Observation) -> bool:
    """Is there prose here that a regex cannot read?"""

    if observation.observation_type not in _PROSE_TYPES:
        return False
    body = observation.structured_payload.get("body") or ""
    text = f"{observation.headline or ''} {body}".strip()
    # Below roughly a sentence there is nothing to structure, and an empty prompt
    # is the most common source of hallucinated claims.
    return len(text) >= 120


def build_extractor(use_llm: bool = False, client: LlmClient | None = None) -> ClaimExtractor:
    """``use_llm=True`` now means *augment*, not *replace*.

    Previously this returned :class:`LlmClaimExtractor` outright, which meant
    asking for the LLM turned the deterministic extractors off. That is the wrong
    shape for the reason spelled out on :class:`HybridClaimExtractor`: the rules
    are not a weaker version of the model, they are the correct answer for
    structured feeds.
    """

    if not use_llm:
        return RuleClaimExtractor()
    return HybridClaimExtractor(client)


__all__ = [
    "ClaimExtractor",
    "HybridClaimExtractor",
    "LlmClaimExtractor",
    "PREDICATE_LABELS",
    "RuleClaimExtractor",
    "build_extractor",
]
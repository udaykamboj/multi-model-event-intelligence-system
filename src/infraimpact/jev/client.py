"""Jev integration (brief sections 21-22).

Jev is a bounded decision model: given a state plus typed questions it returns
a choice, a score, or a probability that software can consume directly. It
belongs in the decision layer, never the forecasting layer.

    "Will traffic be disrupted in 30 minutes?"      -> predictive model
    "Is this user's route materially affected?"      -> Jev noul
    "What should be surfaced first?"                 -> Jev choice

``RuleJevClient`` implements the same contract deterministically so the
decision layer is live without the dependency. Swap in the real client by
setting ``JEV_ENDPOINT``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Sequence

import httpx

from ..domain.enums import Urgency
from ..domain.ids import utcnow

log = logging.getLogger(__name__)

Noul = Literal["probability"]
Choice = Literal["choice"]
Score = Literal["score"]

#: Impact categories Jev may choose between. Bounded on purpose.
PRIMARY_IMPACT_OPTIONS = (
    "road",
    "transit",
    "utility",
    "public_safety",
    "ferries",
    "none",
    "other",
)

URGENCY_OPTIONS = ("none", "low", "moderate", "high", "immediate")


@dataclass(frozen=True)
class JevQuestion:
    """A typed question with a bounded answer space."""

    question_id: str
    kind: Literal["noul", "choice", "score"]
    text: str
    options: Sequence[str] = ()
    state: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"id": self.question_id, "kind": self.kind, "text": self.text}
        if self.options:
            body["options"] = list(self.options)
        if self.state:
            body["state"] = self.state
        return body


class JevClient(ABC):
    @abstractmethod
    def ask(self, questions: Sequence[JevQuestion]) -> dict[str, dict[str, Any]]: ...


class RuleJevClient(JevClient):
    """Deterministic bounded decisions derived from already-computed facts.

    Each implementation is intentionally simple and inspectable: the point is
    that the decision layer consumes *measured* state, and that every decision
    has a recorded reason.
    """

    def ask(self, questions: Sequence[JevQuestion]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for question in questions:
            handler = {
                "noul": self._noul,
                "choice": self._choice,
                "score": self._score,
            }[question.kind]
            try:
                out[question.question_id] = handler(question)
            except Exception as exc:  # noqa: BLE001 - never let a decision break analysis
                log.warning("jev decision %s failed: %r", question.question_id, exc)
                out[question.question_id] = {"value": None, "error": repr(exc), "at": utcnow().isoformat()}
        return out

    # -- typed handlers ---------------------------------------------------

    def _noul(self, q: JevQuestion) -> dict[str, Any]:
        state = q.state or {}
        if "route_impacted" in state:
            value = 0.9 if state["route_impacted"] else 0.08
            reason = "saved route intersects a current infrastructure disruption"
        elif "magnitude" in state:
            value = max(0.0, min(1.0, float(state["magnitude"])))
            reason = "material state-change magnitude"
        elif "user_exposure" in state:
            value = max(0.0, min(1.0, float(state["user_exposure"])))
            reason = "user exposure score"
        else:
            value, reason = 0.5, "insufficient state; defaulting to undecided"
        return {"kind": "noul", "value": round(value, 3), "reason": reason, "at": utcnow().isoformat()}

    def _choice(self, q: JevQuestion) -> dict[str, Any]:
        state = q.state or {}
        candidates: dict[str, float] = {
            "road": float(state.get("road_impact", 0.0)),
            "transit": float(state.get("transit_impact", 0.0)),
            "utility": float(state.get("utility_impact", 0.0)),
            "public_safety": float(state.get("public_safety_impact", 0.0)),
            "ferries": float(state.get("ferries_impact", 0.0)),
        }
        if state.get("official_guidance"):
            candidates["public_safety"] = max(candidates["public_safety"], 0.95)
        best = max(candidates.items(), key=lambda kv: kv[1])
        value = "none" if best[1] < 0.15 else best[0]
        return {
            "kind": "choice",
            "value": value,
            "options_considered": list(PRIMARY_IMPACT_OPTIONS),
            "reason": f"highest measured domain impact ({best[0]}={best[1]:.2f})",
            "at": utcnow().isoformat(),
        }

    def _score(self, q: JevQuestion) -> dict[str, Any]:
        state = q.state or {}
        exposure = float(state.get("exposure_score", 0.0))
        change = float(state.get("change_magnitude", 0.0))
        confidence = float(state.get("confidence", 0.5))
        combined = max(0.0, min(1.0, 0.65 * exposure + 0.35 * change)) * (0.5 + 0.5 * confidence)
        band = _band(combined)
        return {
            "kind": "score",
            "value": round(combined, 3),
            "band": band,
            "options_considered": list(URGENCY_OPTIONS),
            "reason": "exposure/change/confidence composite",
            "at": utcnow().isoformat(),
        }


class HttpJevClient(JevClient):
    """Talks to a Jev-compatible endpoint when ``JEV_ENDPOINT`` is configured."""

    def __init__(self, endpoint: str, api_key: str = "", timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def ask(self, questions: Sequence[JevQuestion]) -> dict[str, dict[str, Any]]:
        if not questions:
            return {}
        body = {"questions": [q.payload() for q in questions]}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.endpoint, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        return data.get("answers", {})


def _band(score: float) -> str:
    if score >= 0.75:
        return "immediate"
    if score >= 0.5:
        return "high"
    if score >= 0.3:
        return "moderate"
    if score >= 0.12:
        return "low"
    return "none"


def urgency_from_band(band: str | None) -> Urgency:
    try:
        return Urgency(band) if band else Urgency.NONE
    except ValueError:
        return Urgency.NONE


def build_jev_client() -> JevClient:
    import os

    endpoint = os.environ.get("JEV_ENDPOINT", "")
    if endpoint:
        return HttpJevClient(endpoint, os.environ.get("JEV_API_KEY", ""))
    return RuleJevClient()


__all__ = [
    "Choice",
    "HttpJevClient",
    "JevClient",
    "JevQuestion",
    "Noul",
    "PRIMARY_IMPACT_OPTIONS",
    "RuleJevClient",
    "Score",
    "URGENCY_OPTIONS",
    "build_jev_client",
    "urgency_from_band",
]
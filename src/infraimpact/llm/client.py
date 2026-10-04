"""LLM port (brief sections 18-20).

The LLM is an *interpretation and orchestration* component, never the
forecasting engine. This module defines the contract; ``NullLlmClient`` is the
default so the platform runs with no API key and no network.

Prohibitions enforced by contract (section 20):
  - never declare an evacuation or invent a destination
  - never override official emergency instructions
  - never generate authoritative road closures
  - never invent current facts
  - never assign criminal intent or infer that a political group is dangerous

Every operation returns structured, schema-shaped output. Free-form prose is
never used for backend control.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

#: Operations the LLM layer is permitted to perform.
LLM_OPERATIONS = (
    "extract_claims",
    "resolve_event",
    "select_capabilities",
    "synthesize_evidence",
    "explain_to_user",
    "hypothesize_infrastructure",
)

#: Predicates an LLM may extract. Anything outside this set is rejected.
ALLOWED_EXTRACTED_PREDICATES = frozenset(
    {
        "event.gathering",
        "event.movement",
        "event.movement_direction",
        "event.estimated_crowd",
        "event.expansion",
        "event.status",
        "event.route",
        "event.footprint",
        "location.name",
    }
)

#: Categories the LLM must never emit. Checked before any write.
FORBIDDEN_LLM_CLAIMS = frozenset(
    {
        "emergency.evacuation",
        "emergency.evacuation_destination",
        "road.authoritative_closure",
        "participant.criminal_intent",
        "group.dangerousness",
    }
)


class LlmUnavailable(RuntimeError):
    pass


class LlmOperationLog:
    """What the interpretation layer was actually asked to do.

    Section 43 makes every production decision replayable, and an analysis whose
    prose was written by a language model is not replayable unless the record
    says so. ``AnalysisRun.llm_operations`` stores this log, so three states that
    would otherwise look identical stay distinguishable:

    * never asked (the LLM was not configured, or was not needed)
    * asked and answered
    * asked and refused - unavailable, over budget, or blocked by section 20

    The per-cycle budget exists because the loop fans out over events and then
    over users. Without it a hundred users on one event would mean a hundred
    near-identical completion calls, which is both slow and the fastest way to
    exhaust an API key on a platform that is supposed to be cheap to run.
    """

    #: Field names deliberately differ from every recorder method name. A log
    #: whose ``refused`` attribute shadows ``refused()`` still *reads* correctly
    #: and passes a naive test, then raises ``'list' object is not callable`` the
    #: first time something tries to record a refusal - which is exactly the path
    #: a guardrail violation takes, and therefore the path least likely to be
    #: exercised by hand.
    def __init__(self, max_calls: int = 12) -> None:
        self.max_calls = max(1, int(max_calls))
        self.attempted: list[str] = []
        self.ok: list[str] = []
        self.errors: list[str] = []
        self.blocked: list[str] = []

    def begin(self, operation: str) -> bool:
        """Record the attempt. Returns ``False`` when the budget is spent."""
        self.attempted.append(operation)
        return self.attempted.count(operation) <= self.max_calls

    def succeeded(self, operation: str) -> None:
        """Record a completed, section-20-clean response."""

        self.ok.append(operation)

    def failed(self, operation: str, reason: str = "") -> None:
        """Record an attempt that did not complete (transport or schema error)."""

        self.errors.append(f"{operation}: {reason}" if reason else operation)

    def refused(self, operation: str, reason: str) -> None:
        """Record an attempt that was deliberately not answered.

        A section-20 block lands here too, and that is deliberate: "refused" is
        the honest word for it too. The platform did not merely decline to use the
        answer; it declined to accept it.
        """

        self.blocked.append(f"{operation}: {reason}")

    def as_tuple(self) -> tuple[str, ...]:
        """Compact, stable form for ``AnalysisRun.llm_operations``.

        Repeated operations collapse to a count so a per-cycle budget cannot
        blow up the stored run, and refusals keep their reason because "the model
        tried to issue an evacuation order" is the single most important thing an
        audit of this subsystem can learn.
        """

        entries: list[str] = []
        for operation in dict.fromkeys(self.attempted):
            count = self.attempted.count(operation)
            entry = f"{operation}x{count}" if count > 1 else operation
            reasons = sorted(
                {r.split(": ", 1)[1] for r in self.blocked if r.startswith(f"{operation}: ")}
            )
            if operation in self.ok:
                entry += ":ok"
            elif reasons:
                entry += ":refused(" + ",".join(reasons) + ")"
            else:
                entry += ":failed"
            entries.append(entry)
        return tuple(entries)

    def describe(self) -> dict[str, Any]:
        return {
            "attempted": list(self.attempted),
            "succeeded": sorted(set(self.ok)),
            "failed": list(self.errors),
            "refused": list(self.blocked),
            "budget": self.max_calls,
        }


class LlmClient(ABC):
    @abstractmethod
    def extract_claims(self, text: str, observation_id: str, allowed_predicates: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def resolve_event(self, candidate: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]: ...

    @abstractmethod
    def select_capabilities(self, state_summary: dict[str, Any], delta_summary: dict[str, Any], registry: list[dict[str, Any]]) -> dict[str, Any]: ...

    @abstractmethod
    def synthesize_evidence(self, facts: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def explain_to_user(self, facts: dict[str, Any]) -> dict[str, Any]: ...

    def hypothesize_infrastructure(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Hypothesis generation. Optional: not every LLM backend offers it.

        Hypotheses are questions worth examining, never findings. A backend that
        does not implement this returns nothing rather than raising, so callers
        need no capability probe.
        """

        return {}


class NullLlmClient(LlmClient):
    """Explicit no-op implementation. Records that an operation was requested.

    Being explicit matters: the analysis run records ``llm_operations`` so an
    audit can tell the difference between "the LLM found nothing" and "no LLM
    was configured".
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract_claims(self, text: str, observation_id: str, allowed_predicates: list[str]) -> dict[str, Any]:
        self.calls.append("extract_claims")
        return {"claims": [], "reason": "llm_not_configured", "observation_id": observation_id}

    def resolve_event(self, candidate: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append("resolve_event")
        return {
            "decision": "deterministic_only",
            "reason": "llm_not_configured",
            "considered": [o.get("event_id") for o in options],
        }

    def select_capabilities(
        self, state_summary: dict[str, Any], delta_summary: dict[str, Any], registry: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.calls.append("select_capabilities")
        return {"recommended_capabilities": [], "reason": "llm_not_configured"}

    def synthesize_evidence(self, facts: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("synthesize_evidence")
        return {"summary": None, "reason": "llm_not_configured"}

    def explain_to_user(self, facts: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("explain_to_user")
        return {"headline": None, "body": None, "reason": "llm_not_configured"}

    def hypothesize_infrastructure(self, facts: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("hypothesize_infrastructure")
        return {"hypotheses": [], "reason": "llm_not_configured"}


def llm_operations(client: LlmClient | None) -> tuple[str, ...]:
    """Uniform read of "what did the LLM do", for ``AnalysisRun``.

    Both client shapes keep that record - ``BigPickleClient`` in an
    :class:`LlmOperationLog`, ``NullLlmClient`` in a plain call list - so callers
    never have to know which one they were handed.
    """

    if client is None:
        return ()
    log = getattr(client, "log", None)
    if isinstance(log, LlmOperationLog):
        return log.as_tuple()
    calls = getattr(client, "calls", None)
    return tuple(f"{name}:refused(llm_not_configured)" for name in dict.fromkeys(calls or []))


class OpenAiCompatibleClient(LlmClient):
    """Works with any OpenAI-compatible ``/chat/completions`` endpoint.

    Configured via environment:
        LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

    Responses are requested as strict JSON and validated against the allowed
    predicate set before use.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        temperature: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def _post(self, system: str, user: str, schema_hint: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        system
                        + "\nRespond with a single JSON object matching this shape:\n"
                        + json.dumps(schema_hint, indent=2)
                    ),
                },
                {"role": "user", "content": user},
            ],
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    def extract_claims(self, text: str, observation_id: str, allowed_predicates: list[str]) -> dict[str, Any]:
        result = self._post(
            system=(
                "Extract only facts explicitly stated in the text. Do not infer, "
                "do not add facts, do not assign intent or blame. Use only the "
                f"allowed predicates: {sorted(ALLOWED_EXTRACTED_PREDICATES)}"
            ),
            user=f"observation_id={observation_id}\ntext:\n{text}",
            schema_hint={
                "claims": [
                    {"predicate": "event.gathering", "value": True, "confidence": 0.0},
                    {"predicate": "event.movement_direction", "value": "north", "confidence": 0.0},
                ]
            },
        )
        claims = [
            c
            for c in result.get("claims", [])
            if isinstance(c, dict) and c.get("predicate") in ALLOWED_EXTRACTED_PREDICATES
        ]
        return {"claims": claims, "observation_id": observation_id}

    def resolve_event(self, candidate: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post(
            system="Decide which existing event, if any, a new observation belongs to. Be conservative: prefer a new event over a wrong merge.",
            user=json.dumps({"candidate": candidate, "options": options}, default=str),
            schema_hint={"decision": "existing|new|uncertain", "event_id": None, "reason": ""},
        )

    def select_capabilities(
        self, state_summary: dict[str, Any], delta_summary: dict[str, Any], registry: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._post(
            system="Select which analytical capabilities would reduce uncertainty or improve the impact assessment given the current state and what changed. Choose only from the provided registry.",
            user=json.dumps(
                {"state": state_summary, "delta": delta_summary, "registry": registry}, default=str
            ),
            schema_hint={
                "recommended_capabilities": [{"capability": "", "reason": "", "priority": 0.0}]
            },
        )

    def synthesize_evidence(self, facts: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            system="Summarise the supplied evidence. Do not add facts.",
            user=json.dumps(facts, default=str),
            schema_hint={"summary": "", "sources": []},
        )

    def explain_to_user(self, facts: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            system=(
                "Explain the supplied facts to a user: what changed, why it matters, "
                "what evidence supports it, and what remains uncertain. Never invent "
                "facts, never give safety-critical instructions."
            ),
            user=json.dumps(facts, default=str),
            schema_hint={"headline": "", "body": "", "uncertainty": ""},
        )

    def hypothesize_infrastructure(self, facts: dict[str, Any]) -> dict[str, Any]:
        return self._post(
            system=(
                "Propose which infrastructure relationships deserve examination next. "
                "These are hypotheses to test, not findings, and must be phrased as "
                "questions. Never assert an impact you have no evidence for."
            ),
            user=json.dumps(facts, default=str),
            schema_hint={
                "hypotheses": [
                    {
                        "infrastructure_type": "",
                        "target_id_or_name": "",
                        "potential_impact": "",
                        "urgency": "low|medium|high|critical",
                        "justification": "",
                    }
                ],
                "uncertainty": "",
            },
        )


class OrchestrationClient(Protocol):
    """Marker protocol for anything able to plan capability selection."""

    def select_capabilities(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


def build_llm_client() -> LlmClient:
    """Pick the strongest client the environment can actually support.

    Order matters. The supplied ``llm`` package knows about guardrails, schema
    validation and prompt engineering that this port does not duplicate, so it
    wins whenever it can be loaded. ``LLM_BASE_URL``/``LLM_API_KEY`` is the
    generic escape hatch for an operator who points the platform at their own
    endpoint instead. Only if neither is present do we fall back to the null
    client - and the caller can tell, because the log records the refusal.
    """

    import os

    try:
        from .big_pickle import build_big_pickle_client

        client = build_big_pickle_client()
        if not isinstance(client, NullLlmClient):
            return client
    except Exception as exc:  # noqa: BLE001 - a missing dependency is not fatal
        log.warning("supplied llm package unavailable: %r", exc)

    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if base_url and api_key and model:
        return OpenAiCompatibleClient(base_url, api_key, model)
    log.debug("no LLM configured; using NullLlmClient")
    return NullLlmClient()


__all__ = [
    "ALLOWED_EXTRACTED_PREDICATES",
    "FORBIDDEN_LLM_CLAIMS",
    "LLM_OPERATIONS",
    "LlmClient",
    "LlmOperationLog",
    "LlmUnavailable",
    "NullLlmClient",
    "OpenAiCompatibleClient",
    "build_llm_client",
    "llm_operations",
]
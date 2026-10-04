"""Big Pickle LLM Client for OpenRouter and OpenCode Zen.

Implements interpretation and orchestration capabilities conforming to
Sections 18-20 of the Dynamic Infrastructure Impact Intelligence Platform.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import LlmConfig, BigPickleConfig, get_default_config
from .guardrails import filter_and_validate_claims, sanitize_user_text
from .mock_responses import (
    mock_explain_to_user,
    mock_extract_claims,
    mock_infrastructure_hypothesis,
    mock_resolve_event,
    mock_select_capabilities,
    mock_synthesize_evidence,
)
from .prompts import (
    SYSTEM_EXPLAIN_TO_USER,
    SYSTEM_EXTRACT_CLAIMS,
    SYSTEM_INFRASTRUCTURE_HYPOTHESIS,
    SYSTEM_RESOLVE_EVENT,
    SYSTEM_SELECT_CAPABILITIES,
    SYSTEM_SYNTHESIZE_EVIDENCE,
    build_user_prompt,
)
from .schemas import (
    ALLOWED_EXTRACTED_PREDICATES,
    CapabilitySelectionResponse,
    ClaimExtractionResponse,
    EventResolutionResponse,
    EvidenceSynthesisResponse,
    ExtractedClaim,
    InfrastructureHypothesisResponse,
    UserExplanationResponse,
)

log = logging.getLogger("llm")


class LlmError(RuntimeError):
    """Base exception for LLM client errors."""


# Backwards compatibility alias
BigPickleError = LlmError


def _clean_json_markdown(content: str) -> str:
    """Strips markdown code fences and whitespace from LLM output."""
    stripped = content.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


class LlmClient:
    """Synchronous client for OpenRouter LLMs."""

    def __init__(self, config: LlmConfig | None = None) -> None:
        self.config = config or get_default_config()
        self.history: list[dict[str, Any]] = []

    def _post_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_hint: dict[str, Any] | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Dispatches an OpenAI-compatible /chat/completions request with fallback support."""
        if self.config.mock_mode:
            log.info("BigPickle running in MOCK mode (no remote network call).")
            return {}

        headers = self.config.get_headers()
        model_to_use = model_override or self.config.model

        messages = [{"role": "system", "content": system_prompt}]
        if schema_hint:
            messages[0]["content"] += (
                f"\n\nCRITICAL: Respond ONLY with valid JSON matching this schema:\n"
                f"{json.dumps(schema_hint, indent=2)}"
            )
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": model_to_use,
            "messages": messages,
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
        }

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        candidate_models = [model_to_use] + [
            m for m in self.config.fallback_models if m != model_to_use
        ]

        last_error: Exception | None = None
        for candidate in candidate_models:
            payload["model"] = candidate
            try:
                log.debug("Attempting LLM call to %s with model %s", url, candidate)
                with httpx.Client(timeout=self.config.timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                raw_content = data["choices"][0]["message"]["content"]
                clean_content = _clean_json_markdown(raw_content)
                parsed = json.loads(clean_content)
                self.history.append({
                    "model": candidate,
                    "prompt_tokens": data.get("usage", {}).get("prompt_tokens"),
                    "completion_tokens": data.get("usage", {}).get("completion_tokens"),
                })
                return parsed

            except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as e:
                log.warning("Call to model %s failed: %s. Trying fallback...", candidate, e)
                last_error = e
                continue

        raise LlmError(f"All candidate models failed. Last error: {last_error}")

    # --------------------------------------------------------------------------
    # 1. Unstructured Extraction (Section 18.A)
    # --------------------------------------------------------------------------
    def extract_claims(
        self,
        text: str,
        observation_id: str,
        allowed_predicates: list[str] | None = None,
    ) -> dict[str, Any]:
        """Extracts strictly verified factual claims from unstructured news/reports prose."""
        predicates = allowed_predicates or sorted(list(ALLOWED_EXTRACTED_PREDICATES))

        if self.config.mock_mode:
            raw = mock_extract_claims(text, observation_id)
        else:
            system = SYSTEM_EXTRACT_CLAIMS.format(
                allowed_predicates=json.dumps(predicates, indent=2)
            )
            raw = self._post_chat(
                system_prompt=system,
                user_prompt=f"Observation ID: {observation_id}\n\nText Content:\n{text}",
                schema_hint={
                    "claims": [
                        {
                            "predicate": "event.gathering",
                            "value": True,
                            "confidence": 0.95,
                            "evidence_span": "excerpts from text",
                        }
                    ]
                },
            )

        # Enforce Section 20 non-negotiable guardrails and whitelist
        valid_claims, rejected_claims = filter_and_validate_claims(
            raw.get("claims", []),
            allowed_predicates=set(predicates),
        )

        resp = ClaimExtractionResponse(
            observation_id=observation_id,
            claims=valid_claims,
            rejected_claims=rejected_claims,
            reason=raw.get("reason"),
        )
        return resp.model_dump()

    # --------------------------------------------------------------------------
    # 2. Semantic Event Resolution (Section 18.B)
    # --------------------------------------------------------------------------
    def resolve_event(
        self,
        candidate: dict[str, Any],
        options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Determines whether a new report describes an existing active event or a new one."""
        if self.config.mock_mode:
            raw = mock_resolve_event(candidate, options)
        else:
            prompt_data = {
                "candidate_observation": candidate,
                "existing_active_events": options,
            }
            raw = self._post_chat(
                system_prompt=SYSTEM_RESOLVE_EVENT,
                user_prompt=build_user_prompt(prompt_data),
                schema_hint={
                    "decision": "existing",
                    "event_id": "ev_123",
                    "confidence": 0.85,
                    "reason": "Matches timeline and location",
                    "matching_signals": ["road_overlap"],
                },
            )

        resp = EventResolutionResponse.model_validate(raw)
        return resp.model_dump()

    # --------------------------------------------------------------------------
    # 3. Dynamic Orchestration / Capability Selection (Section 18.D)
    # --------------------------------------------------------------------------
    def select_capabilities(
        self,
        state_summary: dict[str, Any],
        delta_summary: dict[str, Any],
        registry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Recommends which analytical capabilities should execute given the delta."""
        if self.config.mock_mode:
            raw = mock_select_capabilities(registry)
        else:
            prompt_data = {
                "event_state_summary": state_summary,
                "state_delta": delta_summary,
                "available_capabilities_registry": registry,
            }
            raw = self._post_chat(
                system_prompt=SYSTEM_SELECT_CAPABILITIES,
                user_prompt=build_user_prompt(prompt_data),
                schema_hint={
                    "recommended_capabilities": [
                        {
                            "capability": "road_network_exposure",
                            "reason": "Footprint intersects 4th Ave",
                            "priority": 0.9,
                            "cost_tier": "medium",
                        }
                    ],
                    "reason": "Prioritize roadway analysis",
                },
            )

        resp = CapabilitySelectionResponse.model_validate(raw)
        return resp.model_dump()

    # --------------------------------------------------------------------------
    # 4. Hypothesis Generation (Section 18.E)
    # --------------------------------------------------------------------------
    def hypothesize_infrastructure(
        self,
        event_facts: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generates hypotheses regarding impacted infrastructure dependencies."""
        if self.config.mock_mode:
            raw = mock_infrastructure_hypothesis(event_facts)
        else:
            prompt_data = {
                "event_facts": event_facts,
                "infrastructure_context": context or {},
            }
            raw = self._post_chat(
                system_prompt=SYSTEM_INFRASTRUCTURE_HYPOTHESIS,
                user_prompt=build_user_prompt(prompt_data),
                schema_hint={
                    "hypotheses": [
                        {
                            "infrastructure_type": "transit",
                            "target_id_or_name": "Metro Route 4",
                            "potential_impact": "Reroutes and corridor delay",
                            "urgency": "medium",
                            "justification": "March intersects stop locations",
                        }
                    ],
                    "uncertainty": "Exact progression rate unknown",
                },
            )

        resp = InfrastructureHypothesisResponse.model_validate(raw)
        return resp.model_dump()

    # --------------------------------------------------------------------------
    # 5. Evidence Synthesis (Section 18.F)
    # --------------------------------------------------------------------------
    def synthesize_evidence(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Synthesizes structured evidence from multiple sources into an objective summary."""
        if self.config.mock_mode:
            raw = mock_synthesize_evidence(facts)
        else:
            raw = self._post_chat(
                system_prompt=SYSTEM_SYNTHESIZE_EVIDENCE,
                user_prompt=build_user_prompt(facts),
                schema_hint={
                    "summary": "Demonstration in downtown Seattle",
                    "confirmed_facts": ["SDOT confirms road closure"],
                    "reported_claims": ["News reports crowd marching north"],
                    "inferred_points": ["Transit delays on 3rd Ave"],
                    "sources": ["SDOT", "King County Metro"],
                    "confidence_score": 0.9,
                },
            )

        resp = EvidenceSynthesisResponse.model_validate(raw)
        return resp.model_dump()

    # --------------------------------------------------------------------------
    # 6. User Explanation (Section 18.G)
    # --------------------------------------------------------------------------
    def explain_to_user(
        self,
        facts: dict[str, Any],
        user_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Translates validated facts and impact scores into clear, non-alarmist communication."""
        if self.config.mock_mode:
            raw = mock_explain_to_user(facts)
        else:
            prompt_data = {
                "validated_facts": facts,
                "user_context": user_context or {},
            }
            raw = self._post_chat(
                system_prompt=SYSTEM_EXPLAIN_TO_USER,
                user_prompt=build_user_prompt(prompt_data),
                schema_hint={
                    "headline": "Downtown transit delay advisory",
                    "what_changed": "Demonstration has expanded to 4th Ave",
                    "why_it_matters": "Expect 15 minute delays on route home",
                    "evidence": "SDOT sensors and Metro transit reports",
                    "uncertainty": "Event duration is unknown",
                    "suggested_posture": "delay_trip",
                    "official_guidance_reference": "Consult AlertSeattle",
                },
            )

        # Sanitize explanation to ensure no unauthorized evacuation directives are given
        if "what_changed" in raw:
            raw["what_changed"] = sanitize_user_text(raw["what_changed"])
        if "why_it_matters" in raw:
            raw["why_it_matters"] = sanitize_user_text(raw["why_it_matters"])

        resp = UserExplanationResponse.model_validate(raw)
        return resp.model_dump()


# Backwards compatibility alias
BigPickleClient = LlmClient


class AsyncLlmClient:
    """Asynchronous client for OpenRouter LLMs."""

    def __init__(self, config: LlmConfig | None = None) -> None:
        self.config = config or get_default_config()

    async def _post_chat_async(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_hint: dict[str, Any] | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Asynchronously dispatches an OpenAI-compatible /chat/completions request."""
        if self.config.mock_mode:
            log.info("AsyncLlmClient running in MOCK mode.")
            return {}

        headers = self.config.get_headers()
        model_to_use = model_override or self.config.model

        messages = [{"role": "system", "content": system_prompt}]
        if schema_hint:
            messages[0]["content"] += (
                f"\n\nCRITICAL: Respond ONLY with valid JSON matching this schema:\n"
                f"{json.dumps(schema_hint, indent=2)}"
            )
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": model_to_use,
            "messages": messages,
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
        }

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        candidate_models = [model_to_use] + [
            m for m in self.config.fallback_models if m != model_to_use
        ]

        last_error: Exception | None = None
        for candidate in candidate_models:
            payload["model"] = candidate
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                raw_content = data["choices"][0]["message"]["content"]
                clean_content = _clean_json_markdown(raw_content)
                return json.loads(clean_content)

            except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as e:
                log.warning("Async call to model %s failed: %s. Trying fallback...", candidate, e)
                last_error = e
                continue

        raise LlmError(f"All candidate models failed. Last error: {last_error}")

    async def extract_claims(
        self,
        text: str,
        observation_id: str,
        allowed_predicates: list[str] | None = None,
    ) -> dict[str, Any]:
        """Async extraction of structured claims."""
        predicates = allowed_predicates or sorted(list(ALLOWED_EXTRACTED_PREDICATES))
        if self.config.mock_mode:
            raw = mock_extract_claims(text, observation_id)
        else:
            system = SYSTEM_EXTRACT_CLAIMS.format(
                allowed_predicates=json.dumps(predicates, indent=2)
            )
            raw = await self._post_chat_async(
                system_prompt=system,
                user_prompt=f"Observation ID: {observation_id}\n\nText:\n{text}",
                schema_hint={
                    "claims": [
                        {
                            "predicate": "event.gathering",
                            "value": True,
                            "confidence": 0.95,
                            "evidence_span": "demonstration gathered",
                        }
                    ]
                },
            )

        valid_claims, rejected_claims = filter_and_validate_claims(
            raw.get("claims", []),
            allowed_predicates=set(predicates),
        )
        return ClaimExtractionResponse(
            observation_id=observation_id,
            claims=valid_claims,
            rejected_claims=rejected_claims,
            reason=raw.get("reason"),
        ).model_dump()

    async def resolve_event(
        self,
        candidate: dict[str, Any],
        options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Async event resolution."""
        if self.config.mock_mode:
            raw = mock_resolve_event(candidate, options)
        else:
            prompt_data = {"candidate": candidate, "options": options}
            raw = await self._post_chat_async(
                system_prompt=SYSTEM_RESOLVE_EVENT,
                user_prompt=build_user_prompt(prompt_data),
            )
        return EventResolutionResponse.model_validate(raw).model_dump()

    async def select_capabilities(
        self,
        state_summary: dict[str, Any],
        delta_summary: dict[str, Any],
        registry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Async capability selection."""
        if self.config.mock_mode:
            raw = mock_select_capabilities(registry)
        else:
            prompt_data = {
                "state": state_summary,
                "delta": delta_summary,
                "registry": registry,
            }
            raw = await self._post_chat_async(
                system_prompt=SYSTEM_SELECT_CAPABILITIES,
                user_prompt=build_user_prompt(prompt_data),
            )
        return CapabilitySelectionResponse.model_validate(raw).model_dump()

    async def synthesize_evidence(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Async evidence synthesis."""
        if self.config.mock_mode:
            raw = mock_synthesize_evidence(facts)
        else:
            raw = await self._post_chat_async(
                system_prompt=SYSTEM_SYNTHESIZE_EVIDENCE,
                user_prompt=build_user_prompt(facts),
            )
        return EvidenceSynthesisResponse.model_validate(raw).model_dump()

    async def explain_to_user(
        self,
        facts: dict[str, Any],
        user_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Async user explanation."""
        if self.config.mock_mode:
            raw = mock_explain_to_user(facts)
        else:
            prompt_data = {"facts": facts, "user_context": user_context or {}}
            raw = await self._post_chat_async(
                system_prompt=SYSTEM_EXPLAIN_TO_USER,
                user_prompt=build_user_prompt(prompt_data),
            )
        if "what_changed" in raw:
            raw["what_changed"] = sanitize_user_text(raw["what_changed"])
        if "why_it_matters" in raw:
            raw["why_it_matters"] = sanitize_user_text(raw["why_it_matters"])
        return UserExplanationResponse.model_validate(raw).model_dump()


# Backwards compatibility alias
AsyncBigPickleClient = AsyncLlmClient


"""LLM Package.

Standalone, production-grade LLM orchestration module powered by OpenRouter.
Implements the interpretation and orchestration requirements of Sections 18-20 of
the Dynamic Infrastructure Impact Intelligence Platform.
"""

from __future__ import annotations

from .client import (
    AsyncBigPickleClient,
    AsyncLlmClient,
    BigPickleClient,
    BigPickleError,
    LlmClient,
    LlmError,
)
from .config import BigPickleConfig, LlmConfig, get_default_config
from .guardrails import (
    GuardrailViolation,
    filter_and_validate_claims,
    sanitize_user_text,
)
from .schemas import (
    ALLOWED_EXTRACTED_PREDICATES,
    FORBIDDEN_LLM_CLAIMS,
    CapabilityRecommendation,
    CapabilitySelectionResponse,
    ClaimExtractionResponse,
    EventResolutionResponse,
    EvidenceSynthesisResponse,
    ExtractedClaim,
    InfrastructureHypothesis,
    InfrastructureHypothesisResponse,
    UserExplanationResponse,
)


def build_llm_client(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    mock_mode: bool | None = None,
) -> LlmClient:
    """Convenience factory creating a configured LlmClient instance."""
    cfg = get_default_config()
    if api_key is not None:
        cfg.api_key = api_key
    if base_url is not None:
        cfg.base_url = base_url
    if model is not None:
        cfg.model = model
    if mock_mode is not None:
        cfg.mock_mode = mock_mode
    return LlmClient(config=cfg)


# Backwards compatibility alias
build_big_pickle_client = build_llm_client


__all__ = [
    "ALLOWED_EXTRACTED_PREDICATES",
    "FORBIDDEN_LLM_CLAIMS",
    "AsyncBigPickleClient",
    "AsyncLlmClient",
    "BigPickleClient",
    "BigPickleConfig",
    "BigPickleError",
    "CapabilityRecommendation",
    "CapabilitySelectionResponse",
    "ClaimExtractionResponse",
    "EventResolutionResponse",
    "EvidenceSynthesisResponse",
    "ExtractedClaim",
    "GuardrailViolation",
    "InfrastructureHypothesis",
    "InfrastructureHypothesisResponse",
    "LlmClient",
    "LlmConfig",
    "LlmError",
    "UserExplanationResponse",
    "build_big_pickle_client",
    "build_llm_client",
    "filter_and_validate_claims",
    "get_default_config",
    "sanitize_user_text",
]

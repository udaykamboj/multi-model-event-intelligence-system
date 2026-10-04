"""Configuration management for the LLM client.

Supports OpenRouter, OpenCode Zen, and standard OpenAI-compatible endpoints.
Loads settings from environment variables or .env files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

# Automatically search for and load .env from llm directory or project root
try:
    from dotenv import load_dotenv

    local_env = Path(__file__).parent / ".env"
    root_env = Path(__file__).parent.parent / ".env"
    if local_env.exists():
        load_dotenv(local_env)
    elif root_env.exists():
        load_dotenv(root_env)
    else:
        load_dotenv()
except ImportError:
    pass


class LlmConfig(BaseModel):
    """Runtime configuration for LLM connections."""

    # Provider and endpoint settings
    base_url: str = Field(
        default_factory=lambda: os.environ.get(
            "OPENROUTER_BASE_URL",
            os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        ),
        description="Base URL for OpenAI-compatible endpoint (OpenRouter)",
    )

    api_key: str = Field(
        default_factory=lambda: os.environ.get(
            "OPENROUTER_API_KEY",
            os.environ.get("LLM_API_KEY", os.environ.get("OPENCODE_API_KEY", "")),
        ),
        description="API key for OpenRouter",
    )

    model: str = Field(
        default_factory=lambda: os.environ.get(
            "OPENROUTER_MODEL",
            os.environ.get("LLM_MODEL", "stealth/space-bunny-alpha"),
        ),
        description="Model identifier, default 'stealth/space-bunny-alpha'",
    )

    fallback_models: list[str] = Field(
        default_factory=lambda: [
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
        ],
        description="Optional fallback model list if the primary model is unavailable",
    )

    # OpenRouter specific ranking & telemetry headers
    site_url: str = Field(
        default_factory=lambda: os.environ.get("OPENROUTER_SITE_URL", "https://github.com/infraimpact"),
        description="HTTP-Referer header required by OpenRouter rankings",
    )

    app_name: str = Field(
        default_factory=lambda: os.environ.get("OPENROUTER_APP_NAME", "InfraImpact Intelligence Platform"),
        description="X-Title header for OpenRouter application identification",
    )

    # Runtime parameters
    timeout: float = Field(
        default_factory=lambda: float(os.environ.get("LLM_TIMEOUT", "35.0")),
        description="HTTP timeout in seconds",
    )

    temperature: float = Field(
        default_factory=lambda: float(os.environ.get("LLM_TEMPERATURE", "0.0")),
        description="Sampling temperature (0.0 recommended for structured analytical outputs)",
    )

    mock_mode: bool = Field(
        default_factory=lambda: (
            os.environ.get("LLM_MOCK", "").lower() in ("true", "1", "yes")
            or os.environ.get("MOCK_LLM", "").lower() in ("true", "1", "yes")
            or os.environ.get("BIG_PICKLE_MOCK", "").lower() in ("true", "1", "yes")
            or not os.environ.get("OPENROUTER_API_KEY", os.environ.get("LLM_API_KEY", os.environ.get("OPENCODE_API_KEY", "")))
        ),
        description="If True, returns deterministic mock outputs without network requests",
    )

    def is_configured(self) -> bool:
        """Returns True if a real API key and base URL are present."""
        return bool(self.api_key and self.base_url)

    def get_headers(self) -> dict[str, str]:
        """Constructs request headers including OpenRouter specific metadata."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        return headers


# Backwards compatibility alias
BigPickleConfig = LlmConfig


def get_default_config() -> LlmConfig:
    """Factory creating default configuration."""
    return LlmConfig()

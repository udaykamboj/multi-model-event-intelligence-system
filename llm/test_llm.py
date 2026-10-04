"""Automated unit and integration tests for llm package.

Can be run via pytest:
    .venv\\Scripts\\pytest llm\\test_llm.py -v
"""

from __future__ import annotations

import pytest

from llm import (
    ALLOWED_EXTRACTED_PREDICATES,
    FORBIDDEN_LLM_CLAIMS,
    AsyncLlmClient,
    LlmClient,
    LlmConfig,
    ExtractedClaim,
    build_llm_client,
    filter_and_validate_claims,
    sanitize_user_text,
)


def test_config_defaults():
    cfg = LlmConfig()
    assert cfg.base_url == "https://openrouter.ai/api/v1"
    assert cfg.model == "stealth/space-bunny-alpha"
    if cfg.api_key:
        assert "Authorization" in cfg.get_headers()
        assert cfg.get_headers()["Authorization"] == f"Bearer {cfg.api_key}"
    else:
        assert "Authorization" not in cfg.get_headers()
    assert cfg.get_headers()["HTTP-Referer"] == "https://github.com/infraimpact"
    assert cfg.get_headers()["X-Title"] == "InfraImpact Intelligence Platform"


def test_guardrails_forbidden_predicate():
    # Attempt to extract forbidden predicates
    raw_claims = [
        {"predicate": "emergency.evacuation", "value": True, "confidence": 0.99},
        {"predicate": "participant.criminal_intent", "value": "looting", "confidence": 0.9},
        {"predicate": "event.gathering", "value": True, "confidence": 0.95},
    ]
    valid, rejected = filter_and_validate_claims(raw_claims)
    assert len(valid) == 1
    assert valid[0].predicate == "event.gathering"
    assert len(rejected) == 2
    assert any(r.get("predicate") == "emergency.evacuation" for r in rejected)
    assert any(r.get("predicate") == "participant.criminal_intent" for r in rejected)


def test_guardrails_unauthorized_predicate():
    raw_claims = [
        {"predicate": "random.unregistered_metric", "value": 123, "confidence": 0.5},
        {"predicate": "event.status", "value": "active", "confidence": 0.9},
    ]
    valid, rejected = filter_and_validate_claims(raw_claims)
    assert len(valid) == 1
    assert valid[0].predicate == "event.status"
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "unauthorized_predicate"


def test_guardrails_text_sanitizer():
    dangerous = "Residents must evacuate immediately to North Shelter!"
    sanitized = sanitize_user_text(dangerous)
    assert "evacuate immediately" not in sanitized
    assert "[Refer to official Seattle emergency guidance]" in sanitized


def test_client_extract_claims_mock():
    client = build_llm_client(mock_mode=True)
    res = client.extract_claims(
        "Demonstration gathered near 4th and Pine moving north",
        observation_id="obs_001",
    )
    assert res["observation_id"] == "obs_001"
    assert len(res["claims"]) >= 1
    predicates = [c["predicate"] for c in res["claims"]]
    assert "event.gathering" in predicates or "event.status" in predicates
    for p in predicates:
        assert p in ALLOWED_EXTRACTED_PREDICATES


def test_client_resolve_event_mock():
    client = build_llm_client(mock_mode=True)
    candidate = {"observation_id": "obs_1", "location": "Seattle"}
    options = [{"event_id": "ev_01", "centroid": [-122.33, 47.60]}]
    res = client.resolve_event(candidate, options)
    assert res["decision"] in ("existing", "new", "uncertain")
    assert "reason" in res


def test_client_select_capabilities_mock():
    client = build_llm_client(mock_mode=True)
    registry = [{"capability": "road_network_exposure"}]
    res = client.select_capabilities(
        state_summary={"active": 1},
        delta_summary={"change": "movement"},
        registry=registry,
    )
    assert len(res["recommended_capabilities"]) >= 1
    assert res["recommended_capabilities"][0]["capability"] == "road_network_exposure"


def test_client_infrastructure_hypotheses_mock():
    client = build_llm_client(mock_mode=True)
    res = client.hypothesize_infrastructure({"corridor": "4th Ave"})
    assert len(res["hypotheses"]) >= 1
    assert res["hypotheses"][0]["infrastructure_type"] in ("roadway", "transit", "bridge", "facility")


def test_client_synthesize_evidence_mock():
    client = build_llm_client(mock_mode=True)
    res = client.synthesize_evidence({"traffic": "heavy"})
    assert "summary" in res
    assert isinstance(res["confirmed_facts"], list)
    assert isinstance(res["reported_claims"], list)


def test_client_explain_to_user_mock():
    client = build_llm_client(mock_mode=True)
    res = client.explain_to_user({"delta": "corridor delay"})
    assert "headline" in res
    assert "what_changed" in res
    assert "why_it_matters" in res
    assert "uncertainty" in res
    assert res["suggested_posture"] in ("monitor", "reroute", "delay_trip", "defer_to_official")


@pytest.mark.anyio
async def test_async_client_mock():
    async_client = AsyncLlmClient(config=LlmConfig(mock_mode=True))
    res = await async_client.extract_claims(
        "Demonstration gathered moving north", observation_id="obs_async_1"
    )
    assert res["observation_id"] == "obs_async_1"
    assert len(res["claims"]) >= 1

"""Deterministic, high-fidelity mock responses for Big Pickle LLM.

Used for offline development, integration tests, and environments where
API keys or network connectivity are not yet available.
"""

from __future__ import annotations

from typing import Any


def mock_extract_claims(text: str, observation_id: str) -> dict[str, Any]:
    """Generates realistic claim extraction response based on input text."""
    lower = text.lower()
    claims = []

    if "gather" in lower or "demonstrat" in lower or "crowd" in lower or "protest" in lower:
        claims.append({
            "predicate": "event.gathering",
            "value": True,
            "confidence": 0.95,
            "evidence_span": "demonstration gathered",
        })

    if "north" in lower or "south" in lower or "east" in lower or "west" in lower:
        direction = "north" if "north" in lower else ("south" if "south" in lower else "west")
        claims.append({
            "predicate": "event.movement_direction",
            "value": direction,
            "confidence": 0.88,
            "evidence_span": f"moving {direction}",
        })

    if "seattle" in lower or "pine" in lower or "pike" in lower or "4th" in lower:
        claims.append({
            "predicate": "location.name",
            "value": "Downtown Seattle / 4th Ave Corridor",
            "confidence": 0.92,
            "evidence_span": "Downtown Seattle",
        })

    if not claims:
        claims.append({
            "predicate": "event.status",
            "value": "active",
            "confidence": 0.80,
            "evidence_span": "reported incident",
        })

    return {
        "observation_id": observation_id,
        "claims": claims,
        "rejected_claims": [],
        "reason": "mock_generator",
    }


def mock_resolve_event(candidate: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
    """Mock resolution: matches if options exist and spatial/temporal bounds align, else new."""
    if options:
        first = options[0]
        return {
            "decision": "existing",
            "event_id": first.get("event_id", "ev_existing_1"),
            "confidence": 0.87,
            "reason": "High spatial overlap and matching incident timeline with existing downtown event.",
            "matching_signals": ["geographic_proximity", "timestamp_alignment"],
        }
    return {
        "decision": "new",
        "event_id": None,
        "confidence": 0.92,
        "reason": "No active events within proximity radius; creating discrete event entity.",
        "matching_signals": ["spatial_distance_threshold_exceeded"],
    }


def mock_select_capabilities(registry: list[dict[str, Any]]) -> dict[str, Any]:
    """Mock capability selection returning appropriate registered tools."""
    recommended = []
    for item in registry[:3]:
        name = item.get("capability") or item.get("name", "road_network_exposure")
        recommended.append({
            "capability": name,
            "reason": f"Triggered to validate dynamic impacts on {name}",
            "priority": 0.85,
            "cost_tier": "medium",
        })

    if not recommended:
        recommended.append({
            "capability": "road_network_exposure",
            "reason": "Assess primary arterial throughput impacts",
            "priority": 0.90,
            "cost_tier": "cheap",
        })

    return {
        "recommended_capabilities": recommended,
        "reason": "Prioritizing transportation and route delay estimation based on active movement.",
    }


def mock_infrastructure_hypothesis(facts: dict[str, Any]) -> dict[str, Any]:
    """Mock infrastructure hypotheses."""
    return {
        "hypotheses": [
            {
                "infrastructure_type": "roadway",
                "target_id_or_name": "4th Avenue Arterial",
                "potential_impact": "Traffic reduction and localized vehicle diversions",
                "urgency": "medium",
                "justification": "Event footprint spans multiple intersections along 4th Ave.",
            },
            {
                "infrastructure_type": "transit",
                "target_id_or_name": "King County Metro Routes 2, 4, 13",
                "potential_impact": "Bus route slowdowns and rolling reroutes around 4th & Pine",
                "urgency": "medium",
                "justification": "Key transit corridor overlap with demonstration activity.",
            }
        ],
        "uncertainty": "Exact crowd progression speed and planned duration remain unconfirmed.",
    }


def mock_synthesize_evidence(facts: dict[str, Any]) -> dict[str, Any]:
    """Mock evidence synthesis."""
    return {
        "summary": "Demonstration actively present in Downtown Seattle with observed localized transit delays.",
        "confirmed_facts": [
            "SDOT traffic sensors indicate vehicle speed reduction on 4th Ave.",
            "King County Metro has posted advisory reroutes for 3rd/4th Ave lines.",
        ],
        "reported_claims": [
            "Local news reports peaceful gathering moving northward.",
        ],
        "inferred_points": [
            "Transit delays likely to extend approximately 15-20 minutes during peak commute.",
        ],
        "sources": ["SDOT Live Sensors", "King County Metro Transit Alerts", "Local News Feed"],
        "confidence_score": 0.91,
    }


def mock_explain_to_user(facts: dict[str, Any]) -> dict[str, Any]:
    """Mock user-facing explanation."""
    return {
        "headline": "Downtown Seattle Transit Advisory: Expect Delays Near 4th & Pine",
        "what_changed": "A peaceful demonstration has gathered near 4th Ave and Pine St and is moving slowly north.",
        "why_it_matters": "Surface bus routes along 3rd and 4th Avenues are experiencing 15-minute delays.",
        "evidence": "Verified by SDOT traffic cameras and King County Metro route advisories.",
        "uncertainty": "Duration of event is not announced; monitoring ongoing.",
        "suggested_posture": "delay_trip",
        "official_guidance_reference": "For official real-time transit alerts, visit tripplanner.kingcounty.gov or follow @AlertSeattle.",
    }

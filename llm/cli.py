"""CLI utility for LLM client.

Usage:
  python -m llm.cli status
  python -m llm.cli test [--live]
  python -m llm.cli extract "Demonstration gathered near 4th and Pine moving north"
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client import LlmClient
from .config import LlmConfig, get_default_config


def print_json(title: str, data: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(data, indent=2, default=str))


def cmd_status(args: argparse.Namespace) -> int:
    cfg = get_default_config()
    print("\n--- LLM Configuration ---")
    print(f"Base URL:         {cfg.base_url}")
    print(f"Model ID:         {cfg.model}")
    print(f"API Key Present:  {'YES' if bool(cfg.api_key) else 'NO (Set OPENROUTER_API_KEY in .env)'}")
    print(f"Mock Mode:        {'ACTIVE (Deterministic offline)' if cfg.mock_mode else 'LIVE (Using remote API)'}")
    print(f"Fallback Models:  {', '.join(cfg.fallback_models)}")
    print(f"Timeout:          {cfg.timeout}s")
    print(f"Site URL:         {cfg.site_url}")
    print(f"App Name:         {cfg.app_name}")
    print("--------------------------\n")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    cfg = get_default_config()
    if args.live:
        if not cfg.api_key:
            print("ERROR: --live requested but neither OPENROUTER_API_KEY nor LLM_API_KEY is set in environment or .env!")
            return 1
        cfg.mock_mode = False

    client = LlmClient(config=cfg)
    print(f"\nRunning LLM verification suite (mode: {'LIVE' if not cfg.mock_mode else 'MOCK'})...")

    # 1. Claim extraction
    text_sample = (
        "SDOT reports peaceful demonstration of 500 people gathered near 4th Ave and Pine St. "
        "Crowd is moving northbound toward Westlake. Transit lines 2 and 4 experiencing slowdown."
    )
    claims = client.extract_claims(text_sample, observation_id="obs_test_001")
    print_json("1. Claim Extraction", claims)

    # 2. Event resolution
    candidate = {
        "observation_id": "obs_test_002",
        "location": "4th Ave & Olive Way",
        "timestamp": "2026-10-03T20:00:00Z",
    }
    existing = [
        {
            "event_id": "ev_seattle_downtown_01",
            "centroid": [-122.337, 47.611],
            "first_seen": "2026-10-03T19:30:00Z",
        }
    ]
    resolution = client.resolve_event(candidate, existing)
    print_json("2. Event Resolution", resolution)

    # 3. Capability selection
    registry = [
        {"capability": "road_network_exposure", "cost": "cheap"},
        {"capability": "transit_delay_propagation", "cost": "medium"},
        {"capability": "multimodal_replay_simulation", "cost": "expensive"},
    ]
    capabilities = client.select_capabilities(
        state_summary={"active_events": 1, "highest_urgency": "medium"},
        delta_summary={"movement": "northbound", "new_corridor": "4th Ave"},
        registry=registry,
    )
    print_json("3. Capability Selection", capabilities)

    # 4. Infrastructure Hypotheses
    hypotheses = client.hypothesize_infrastructure(
        event_facts={"corridor": "4th Ave", "cross_streets": ["Pike", "Pine", "Olive"]},
        context={"critical_facilities": ["Westlake Station", "Pacific Place"]},
    )
    print_json("4. Infrastructure Hypotheses", hypotheses)

    # 5. Evidence Synthesis
    synthesis = client.synthesize_evidence({
        "traffic_cameras": "Slow traffic northbound on 4th Ave",
        "transit_advisory": "Metro reroutes on 3rd & 4th Avenues",
    })
    print_json("5. Evidence Synthesis", synthesis)

    # 6. User Explanation
    explanation = client.explain_to_user({
        "event": "Downtown Demonstration",
        "location": "4th & Pine",
        "transit_impact": "15-minute delay on northbound routes",
    })
    print_json("6. User Explanation", explanation)

    print("\n[SUCCESS] All 6 Section 18-20 LLM operations executed successfully!")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    client = LlmClient()
    result = client.extract_claims(args.text, observation_id="cli_obs_1")
    print_json("Extracted Claims", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="llm",
        description="LLM client for OpenRouter (Infrastructure Impact Intelligence)",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Status command
    subparsers.add_parser("status", help="Show current configuration and API key status")

    # Test command
    test_parser = subparsers.add_parser("test", help="Run end-to-end verification")
    test_parser.add_argument("--live", action="store_true", help="Force live remote call against OpenRouter")

    # Extract command
    extract_parser = subparsers.add_parser("extract", help="Extract claims from text string")
    extract_parser.add_argument("text", help="Text to extract claims from")

    args = parser.parse_args()
    if args.command == "status":
        return cmd_status(args)
    elif args.command == "test":
        return cmd_test(args)
    elif args.command == "extract":
        return cmd_extract(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())

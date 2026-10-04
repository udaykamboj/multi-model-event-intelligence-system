"""Guardrails and safety constraints for the Big Pickle LLM client.

Implements non-negotiable architectural boundaries defined in Section 20 of
the Dynamic Infrastructure Impact Intelligence Platform specification:
  1. Never declare an evacuation or invent evacuation destinations.
  2. Never override official emergency instructions.
  3. Never generate authoritative road closures without official source.
  4. Never invent current facts or speculate on unverified events.
  5. Never assign criminal intent or characterize political groups as dangerous.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .schemas import (
    ALLOWED_EXTRACTED_PREDICATES,
    FORBIDDEN_LLM_CLAIMS,
    ExtractedClaim,
)

log = logging.getLogger("llm.guardrails")

# Keywords that indicate forbidden content generation when ungrounded
FORBIDDEN_TEXT_PATTERNS = [
    (re.compile(r"\b(evacuate immediately|you must evacuate|evacuation order issued by (ai|system|app))\b", re.I),
     "LLM cannot issue independent evacuation directives."),
    (re.compile(r"\b(evacuate to [A-Za-z0-9\s]+shelter|safe zone at [A-Za-z0-9\s]+)\b", re.I),
     "LLM cannot invent evacuation destinations."),
    (re.compile(r"\b(this group is (violent|terrorist|extremist|inherently dangerous))\b", re.I),
     "LLM cannot assign dangerousness or criminal stereotypes to groups."),
    (re.compile(r"\b(with malicious intent|protesters intend to commit crimes)\b", re.I),
     "LLM cannot infer subjective criminal intent."),
]


class GuardrailViolation(ValueError):
    """Raised when an LLM output violates non-negotiable safety rules."""


def filter_and_validate_claims(
    raw_claims: list[dict[str, Any]] | list[ExtractedClaim],
    allowed_predicates: set[str] | frozenset[str] | None = None,
) -> tuple[list[ExtractedClaim], list[dict[str, Any]]]:
    """Inspects extracted claims, enforces predicate whitelist, and drops forbidden claims.

    Returns:
        tuple of (valid_claims, rejected_claims_with_reasons)
    """
    allowed = allowed_predicates if allowed_predicates is not None else ALLOWED_EXTRACTED_PREDICATES
    valid: list[ExtractedClaim] = []
    rejected: list[dict[str, Any]] = []

    for item in raw_claims:
        if isinstance(item, ExtractedClaim):
            pred = item.predicate
            val = item.value
            conf = item.confidence
            span = item.evidence_span
        elif isinstance(item, dict):
            pred = str(item.get("predicate", "")).strip().lower()
            val = item.get("value")
            conf = float(item.get("confidence", 0.8))
            span = item.get("evidence_span")
        else:
            rejected.append({"raw": str(item), "reason": "invalid_type"})
            continue

        # Check forbidden claims
        if pred in FORBIDDEN_LLM_CLAIMS:
            log.warning("Rejected forbidden LLM claim predicate: %s", pred)
            rejected.append({"predicate": pred, "value": val, "reason": "forbidden_predicate"})
            continue

        # Check predicate whitelist
        if pred not in allowed:
            log.warning("Rejected non-whitelisted predicate: %s", pred)
            rejected.append({"predicate": pred, "value": val, "reason": "unauthorized_predicate"})
            continue

        # Text pattern safety check on value if string
        if isinstance(val, str):
            violation_found = False
            for pattern, reason in FORBIDDEN_TEXT_PATTERNS:
                if pattern.search(val):
                    log.warning("Pattern violation in claim value '%s': %s", val, reason)
                    rejected.append({"predicate": pred, "value": val, "reason": reason})
                    violation_found = True
                    break
            if violation_found:
                continue

        try:
            claim_obj = ExtractedClaim(
                predicate=pred,
                value=val,
                confidence=conf,
                evidence_span=span,
            )
            valid.append(claim_obj)
        except Exception as err:
            rejected.append({"predicate": pred, "value": val, "reason": str(err)})

    return valid, rejected


def sanitize_user_text(text: str) -> str:
    """Sanitizes user explanation text to prevent unauthorized safety instructions."""
    sanitized = text
    for pattern, reason in FORBIDDEN_TEXT_PATTERNS:
        if pattern.search(sanitized):
            log.warning("Sanitizing text due to pattern match: %s", reason)
            sanitized = pattern.sub("[Refer to official Seattle emergency guidance]", sanitized)
    return sanitized

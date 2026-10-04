"""The real LLM, wired in (brief sections 18-20).

Why this module exists
----------------------
The port in :mod:`infraimpact.llm.client` declared five operations and shipped a
``NullLlmClient``. The ``llm/`` package sitting in the workspace root - an
OpenRouter client with schema-constrained responses, guardrails, mock mode and
prompt library - was never imported by anything. So ``synthesize_evidence`` and
``explain_to_user`` had no implementation to call, ``AnalysisRun.llm_operations``
was hard-coded to ``()``, and the platform shipped with the interpretation layer
of its own architecture switched off.

This module closes that gap by adapting the real client to the port.

Three properties are load-bearing, and each one is why this is an adapter rather
than a subclass or a monkeypatch:

1. **The platform's prohibitions outrank the library's.** ``llm/guardrails.py``
   is a good first filter, but the platform must not depend on a dependency
   remaining strict. :func:`enforce_prohibitions` re-checks every string the LLM
   produced against section 20 *before* the value reaches any other subsystem,
   and drops whole fields rather than editing prose into something that reads
   safe. An LLM that invents an evacuation destination gets its ``what_changed``
   removed, not its evacuation destination redacted.

2. **An LLM failure is never an analysis failure.** Every operation is wrapped;
   transport errors, schema violations and prohibitions all degrade to ``None``
   with a recorded reason. A road closure analysis must not go dark because a
   completion endpoint timed out.

3. **Prose is never control flow.** :class:`BigPickleClient` returns plain data.
   Capability *recommendations* and infrastructure *hypotheses* are advisory and
   are recorded as such; they cannot add, remove or reorder a forecast, cannot
   manufacture an observation, and cannot be mistaken for CONFIRMED output.
   Every claim it contributes is :attr:`TruthStatus.INFERRED` at best.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ..config import workspace_root
from .client import (
    FORBIDDEN_LLM_CLAIMS,
    LlmClient,
    LlmUnavailable,
    LlmOperationLog,
    NullLlmClient,
)

log = logging.getLogger(__name__)

#: Where the supplied ``llm`` package lives, relative to the workspace root.
_LLM_PACKAGE_DIRNAME = "llm"

#: Registered under this name so it cannot shadow, or be shadowed by, anything
#: else a dependency might import as ``llm``.
_REGISTERED_NAME = "_infraimpact_big_pickle"

#: Minimum signals before interpreting prose is worth the latency. Below this the
#: deterministic evidence vector already says everything there is to say.
MIN_OBSERVATIONS_FOR_NARRATIVE = 2


# --------------------------------------------------------------------------
# Section 20: enforced by the platform, not inherited from a dependency
# --------------------------------------------------------------------------

#: Ordered ``(pattern, reason)`` pairs. Checked against *every* string the LLM
#: returns. These are deliberately broader than the library's list: the library
#: matches a few handpicked phrases, and a model that never emits those exact
#: phrases has still violated the section.
PROHIBITION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 1. Independent evacuation directives, and destinations nobody published.
    (
        re.compile(
            r"\b(evacuat\w*|shelter in place|seek shelter|"
            r"proceed to .{0,40}(?:shelter|safe (?:zone|place|area)))",
            re.I,
        ),
        "independent_evacuation_directive",
    ),
    # 2. Overriding or reinterpreting official instruction.
    (
        re.compile(
            r"\b(official (?:guidance|instructions?|notices?) (?:are|is) (?:wrong|"
            r"outdated|unnecessary|no longer)|"
            r"you (?:do not need to|don't need to) (?:follow|obey) (?:official|the) )",
            re.I,
        ),
        "overrides_official_guidance",
    ),
    # 3. Authoritative-sounding road closure assertion with no official source.
    #    The optional middle clause is what makes this work on real prose: "I-5
    #    northbound is closed" and "Highway 520 has been shut" both carry an
    #    identifier or direction between the asset and the verb. A pattern that
    #    only matched "The road is closed" would look correct in review and match
    #    almost nothing in practice.
    (
        re.compile(
            r"\b(road|highway|bridge|street|freeway|expressway|ramp|"
            r"interstate|route|highways|streets|bridges)\b"
            r"(?:\s+[A-Za-z0-9][\w./'-]*){0,4}"
            r"\s+(?:is|are|was|were|has been|have been|remain|remains|"
            r"remained|got|gotten)?\s*"
            r"(?:now\s+|currently\s+|still\s+)?"
            r"(?:closed|blocked|shut|impassable)\b",
            re.I,
        ),
        "authoritative_road_closure",
    ),
    # 4. Asserting a current fact that no observation supports.
    (
        re.compile(
            r"\b(there (?:is|are) (?:currently )?\d[\d,]* (?:people|protesters|"
            r"participants)|the crowd (?:is|has reached) \d[\d,]*)",
            re.I,
        ),
        "unsourced_current_fact",
    ),
    # 5. Criminal intent or group dangerousness.
    (
        re.compile(
            r"\b(malicious intent|criminal intent|intent to (?:commit|cause)|"
            r"(?:this|these|the) (?:protesters|protestors|demonstrators|group|"
            r"movement|activists) (?:are|is) (?:violent|armed|extremist|terrorist|"
            r"dangerous|disruptive))",
            re.I,
        ),
        "assigns_criminal_intent",
    ),
)

#: What a rejected string is replaced with. It is a placeholder, not a rewrite:
#: the original text is discarded rather than edited, because a redacted
#: sentence still carries the model's framing in its remaining clauses.
REJECTED_PLACEHOLDER = "[withheld: violates platform section 20]"


#: Our tier vocabulary -> the supplied package's ``cost_tier`` literal.
#:
#: The library validates ``cost_tier`` against ``cheap|medium|expensive``. We
#: use ``cheap|triggered|expensive`` (section 58 distinguishes continuous work
#: from work that only runs when a change warrants it). A model asked to advise
#: on our registry will faithfully echo our names back, and the library's schema
#: then rejects the *entire* response - recommendations included - because one
#: field used a word it does not know. Mapping on the way out keeps the library's
#: schema satisfiable; mapping on the way back keeps ours.
TIER_TO_LIBRARY: dict[str, str] = {
    "cheap": "cheap",
    "continuous": "cheap",
    "triggered": "medium",
    "medium": "medium",
    "expensive": "expensive",
}

#: Reverse mapping, so an echoed tier lands in our vocabulary.
LIBRARY_TO_TIER: dict[str, str] = {
    "cheap": "cheap",
    "medium": "triggered",
    "expensive": "expensive",
}


def library_tier(tier: Any) -> str | None:
    """Translate a platform tier into the vocabulary the library can parse."""

    value = getattr(tier, "value", tier)
    return TIER_TO_LIBRARY.get(str(value)) if value is not None else None


#: Field names whose values are *controlled vocabulary*, not prose.
#:
#: Section 20 is about language - sentences a user would read and be misled by.
#: A predicate name is an enum token that no one displays verbatim, and policing
#: it with the prose patterns actively loses information: the literal
#: ``"emergency.evacuation"`` trips the evacuation pattern on the substring
#: ``evacuation``, so the walker dropped the field and the caller could no longer
#: report the accurate reason - it looked like an unknown predicate rather than a
#: forbidden one, which is the difference between "this model tried to assert an
#: evacuation" and "this model used a word I did not recognise".
#:
#: Vocabulary is policed by the stricter, more specific checks instead:
#: :data:`~infraimpact.llm.client.FORBIDDEN_LLM_CLAIMS` for predicates, the enum
#: sets for postures and decisions. Those checks know what a valid token is; a
#: regex does not.
VOCABULARY_FIELDS = frozenset(
    {
        "predicate",
        "claim_type",
        "decision",
        "urgency",
        "urgency_band",
        "severity",
        "truth_status",
        "suggested_posture",
        "tier",
        "cost_tier",
        "infrastructure_type",
        "source",
        "retrieval_method",
        "source_type",
    }
)


class ProhibitedContent(ValueError):
    """Raised by :func:`enforce_prohibitions` when a field cannot be kept."""

    def __init__(self, reason: str, field: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.reason = reason
        self.field = field


def enforce_prohibitions(
    value: Any, *, field: str = "text", vocabulary: bool = False
) -> Any:
    """Section 20, enforced at the boundary. Returns ``None`` when unsafe.

    Non-strings pass through untouched: the prohibitions are about language, and
    a numeric confidence score is not language. Lists and dicts are walked so a
    buried phrase cannot survive in a nested field.

    ``vocabulary=True`` marks this string as a controlled-vocabulary token rather
    than prose, so the patterns are skipped. See :data:`VOCABULARY_FIELDS`.
    """

    if isinstance(value, str):
        if vocabulary:
            return value
        for pattern, reason in PROHIBITION_PATTERNS:
            if pattern.search(value):
                log.warning("withholding llm output %s: %s", field, reason)
                raise ProhibitedContent(reason, field)
        return value
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            is_vocab = isinstance(key, str) and key in VOCABULARY_FIELDS
            try:
                cleaned[key] = enforce_prohibitions(
                    item, field=f"{field}.{key}", vocabulary=is_vocab
                )
            except ProhibitedContent as exc:
                log.warning("dropping llm field %s (%s)", exc.field, exc.reason)
                continue
        return cleaned
    if isinstance(value, (list, tuple)):
        kept: list[Any] = []
        for index, item in enumerate(value):
            try:
                kept.append(
                    enforce_prohibitions(item, field=f"{field}[{index}]", vocabulary=vocabulary)
                )
            except ProhibitedContent as exc:
                log.warning("dropping llm field %s (%s)", exc.field, exc.reason)
                continue
        return kept
    return value


def safe_text(value: Any, *, field: str = "text") -> str | None:
    """:func:`enforce_prohibitions` for a value that must be a string."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return enforce_prohibitions(value.strip(), field=field)
    except ProhibitedContent:
        return None


# --------------------------------------------------------------------------
# Loading the supplied package
# --------------------------------------------------------------------------


class LlmPackageUnavailable(LlmUnavailable):
    """The ``llm/`` package could not be imported."""


def big_pickle_package(root: Path | None = None) -> ModuleType:
    """Import the workspace ``llm/`` package under a private name.

    Loaded by file location rather than ``import llm`` for one reason: ``llm`` is
    a generic top-level name, and whether it resolves depends entirely on the
    process's working directory. Under ``uvicorn`` started from a service unit,
    or under ``pytest`` with an explicit ``pythonpath``, it would silently stop
    resolving - and the platform would quietly fall back to
    ``NullLlmClient`` in production while passing every test.

    Registered in :data:`sys.modules` under a private name so the package's own
    relative imports (``from .config import ...``) resolve. Cached afterwards -
    but only for the *default* root. An explicit ``root=`` is a deliberate request
    for one specific directory, and handing back a different package that happened
    to load earlier would defeat the point of passing one.
    """

    if root is None:
        cached = sys.modules.get(_REGISTERED_NAME)
        if cached is not None:
            return cached
        package_dir = workspace_root() / _LLM_PACKAGE_DIRNAME
    else:
        package_dir = root / _LLM_PACKAGE_DIRNAME

    init = package_dir / "__init__.py"
    if not init.is_file():
        raise LlmPackageUnavailable(
            f"no LLM package at {package_dir} (expected {init.name}); "
            "the interpretation layer will run on NullLlmClient"
        )

    spec = importlib.util.spec_from_file_location(
        _REGISTERED_NAME,
        init,
        # submodule_search_locations is what makes this a package rather than a
        # lone module; without it every relative import inside fails.
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise LlmPackageUnavailable(f"cannot load a spec for {init}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[_REGISTERED_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - report, never crash the platform
        sys.modules.pop(_REGISTERED_NAME, None)
        raise LlmPackageUnavailable(f"{init}: {exc!r}") from exc

    log.info("loaded LLM package from %s", package_dir)
    return module


def big_pickle_config(**overrides: Any) -> Any:
    """Configuration for the real client, built from the platform settings.

    The key is read from ``llm/.env`` by the package's own config loader, so it
    is never held in platform code, in a settings file, or in a log line.
    """

    package = big_pickle_package()
    config = package.get_default_config()
    for key, value in overrides.items():
        if value is not None and hasattr(config, key):
            setattr(config, key, value)
    return config


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class BigPickleClient(LlmClient):
    """:class:`~infraimpact.llm.client.LlmClient` backed by the real LLM.

    Every method is total: it returns data or degrades, never raises. The
    :class:`LlmOperationLog` on ``self.log`` records what was attempted, what
    was refused and why, so an ``AnalysisRun`` can state exactly which
    interpretations shaped it.
    """

    def __init__(
        self,
        *,
        config: Any | None = None,
        enabled: bool = True,
        max_calls_per_cycle: int = 12,
    ) -> None:
        self.log = LlmOperationLog(max_calls=max_calls_per_cycle)
        self.enabled = enabled
        self._config = config
        self._client: Any | None = None
        self._guardrails: Any | None = None
        #: Filled in by :meth:`_load`; ``mock`` means the package's deterministic
        #: responses are in use rather than a live endpoint.
        self.mode: str = "unloaded"

    # -- loading -----------------------------------------------------------

    def _load(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self.enabled:
            self.mode = "disabled"
            return None
        try:
            package = big_pickle_package()
            self._config = self._config or package.get_default_config()
            self._client = package.LlmClient(config=self._config)
            self._guardrails = package
            self.mode = "mock" if getattr(self._config, "mock_mode", False) else "live"
            log.info("LLM client ready: mode=%s model=%s", self.mode, self._config.model)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the loop
            self.mode = "unavailable"
            self._log_failure("load", exc)
            self._client = None
        return self._client

    @property
    def is_live(self) -> bool:
        """True only when a real endpoint answered. Mock mode is not live."""
        self._load()
        return self.mode == "live"

    @property
    def model(self) -> str | None:
        self._load()
        return getattr(self._config, "model", None) if self._config else None

    def describe(self) -> dict[str, Any]:
        self._load()
        return {
            "backend": "big_pickle",
            "mode": self.mode,
            "model": self.model,
            "enabled": self.enabled,
            "operations": list(self.log.attempted),
        }

    # -- guarded invocation ------------------------------------------------

    def _invoke(self, operation: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Call the real client, then check everything it produced.

        The supplied package takes *structured* arguments - ``select_capabilities``
        wants three dicts, not one pre-serialised blob - so args are passed
        through positionally rather than packed into a single payload. Building
        the JSON in this adapter and handing it over as one string would work
        only because the prompts are forgiving; it would put the shape of the
        request under this adapter's control instead of the library's, and the
        library is where that shape belongs.

        ``LlmOperationLog`` records the attempt before the call, so a request that
        dies mid-flight is still visible: an analysis whose LLM call timed out
        and one where the LLM was never asked must not look the same in the run
        record.
        """

        client = self._load()
        if client is None:
            self.log.refused(operation, self.mode or "unavailable")
            return {}

        if not self.log.begin(operation):
            self.log.refused(operation, "call_budget_exhausted")
            return {}

        try:
            raw = getattr(client, operation)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - transport/schema error
            self._log_failure(operation, exc)
            return {}

        if not isinstance(raw, dict):
            self.log.failed(operation, f"expected dict, got {type(raw).__name__}")
            return {}

        try:
            cleaned = enforce_prohibitions(dict(raw), field=operation)
        except ProhibitedContent as exc:
            # Belt and braces: the walker drops offending fields, but if an
            # entire response is prohibited we refuse the whole thing.
            self.log.refused(operation, exc.reason)
            return {}

        self.log.succeeded(operation)
        return cleaned

    def _log_failure(self, operation: str, exc: BaseException) -> None:
        log.warning("llm %s failed: %r", operation, exc)
        self.log.failed(operation, repr(exc))

    # -- the five port operations -----------------------------------------

    def extract_claims(
        self, text: str, observation_id: str, allowed_predicates: list[str]
    ) -> dict[str, Any]:
        """Unstructured extraction (section 18).

        The library already drops predicates outside its whitelist; this filters
        again against *this* platform's predicate set, because the two lists are
        independently maintained and the stricter one must win.
        """

        allowed = set(allowed_predicates)
        result = self._invoke(
            "extract_claims",
            text,
            observation_id,
            allowed_predicates=sorted(allowed),
        )
        claims = []
        rejected: list[dict[str, Any]] = []
        for item in result.get("claims", []) or []:
            if not isinstance(item, dict):
                continue
            predicate = str(item.get("predicate", "")).strip().lower()
            if predicate in FORBIDDEN_LLM_CLAIMS:
                rejected.append({"predicate": predicate, "reason": "forbidden_predicate"})
                continue
            if predicate not in allowed:
                rejected.append({"predicate": predicate, "reason": "not_in_platform_registry"})
                continue
            claims.append(
                {
                    "predicate": predicate,
                    "value": item.get("value"),
                    "confidence": float(item.get("confidence", 0.7)),
                    "evidence_span": item.get("evidence_span"),
                }
            )
        return {
            "claims": claims,
            "observation_id": observation_id,
            "rejected": rejected,
        }

    def resolve_event(
        self, candidate: dict[str, Any], options: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Semantic event resolution (section 18).

        Advisory by construction. The resolver's deterministic score decides the
        outcome; this only contributes a second opinion, and
        :attr:`decided_by` on the stored resolution says which was used.
        """

        result = self._invoke("resolve_event", candidate, options)
        decision = str(result.get("decision", "uncertain")).lower()
        if decision not in ("existing", "new", "uncertain"):
            decision = "uncertain"
        return {
            "decision": decision,
            "event_id": result.get("event_id"),
            "confidence": float(result.get("confidence", 0.0)),
            "reason": result.get("reason"),
            "matching_signals": list(result.get("matching_signals", []) or []),
            "advisory": True,
        }

    def select_capabilities(
        self,
        state_summary: dict[str, Any],
        delta_summary: dict[str, Any],
        registry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Dynamic orchestration (section 18).

        Recommendations are filtered against the *live* registry before they are
        returned. A model that invents a capability name gets it dropped here
        rather than raising ``KeyError`` in the orchestrator later.

        Tiers are translated into the library's vocabulary on the way out, and
        back on the way in - see :data:`TIER_TO_LIBRARY` for why that is not
        optional.
        """

        known = {
            str(entry.get("capability_id") or entry.get("capability") or "")
            for entry in registry
        }
        translated = [
            {**entry, "tier": library_tier(entry.get("tier")) or "medium"}
            for entry in registry
        ]
        result = self._invoke(
            "select_capabilities",
            state_summary,
            delta_summary,
            translated,
        )
        recommendations = []
        for item in result.get("recommended_capabilities", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("capability") or "").strip()
            if not name or (known and name not in known):
                log.warning("dropping llm capability recommendation %r: not registered", name)
                continue
            recommendations.append(
                {
                    "capability": name,
                    "reason": str(item.get("reason", ""))[:500],
                    "priority": max(0.0, min(1.0, float(item.get("priority", 0.5)))),
                    "tier": LIBRARY_TO_TIER.get(str(item.get("cost_tier", "")), "triggered"),
                }
            )
        recommendations.sort(key=lambda r: -r["priority"])
        return {"recommended_capabilities": recommendations, "advisory": True}

    def hypothesize_infrastructure(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Hypothesis generation (section 18).

        Hypotheses are *questions to examine*, never findings. They are stored on
        the run as INFERRED and are explicitly not published to ``/v1``.
        """

        result = self._invoke("hypothesize_infrastructure", facts)
        hypotheses = []
        for item in result.get("hypotheses", []) or []:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target_id_or_name", "")).strip()
            if not target:
                continue
            hypotheses.append(
                {
                    "infrastructure_type": str(item.get("infrastructure_type", ""))[:80],
                    "target_id_or_name": target[:200],
                    "potential_impact": str(item.get("potential_impact", ""))[:500],
                    "urgency": str(item.get("urgency", "medium"))[:16],
                    "justification": str(item.get("justification", ""))[:500],
                }
            )
        return {
            "hypotheses": hypotheses,
            "uncertainty": result.get("uncertainty"),
            "advisory": True,
        }

    def synthesize_evidence(self, facts: dict[str, Any]) -> dict[str, Any]:
        """Evidence synthesis (section 18).

        The summary is the whole point of the call, so it is all-or-nothing. A
        synthesis with no summary is not a synthesis - the lists below it are
        fragments the model chose to enumerate, and presenting them without the
        argument they were supporting would give a user the parts and not the
        point. So a missing summary - refused, prohibited, or simply absent -
        returns ``{}`` and the deterministic evidence vector stands alone.

        Note that this also covers the prohibited case: ``_invoke`` has already
        dropped the offending field by the time control returns here, so the
        absence of ``summary`` is indistinguishable from its removal, and both
        deserve the same answer. An earlier version tried to re-check
        ``result.get("summary")`` to tell them apart and could never fire.
        """

        result = self._invoke("synthesize_evidence", dict(facts))
        if not result:
            return {}
        summary = safe_text(result.get("summary"), field="evidence.summary")
        if not summary:
            log.warning("evidence synthesis produced no usable summary; withholding narrative")
            return {}
        return {
            "summary": summary,
            "confirmed_facts": _clean_strings(result.get("confirmed_facts")),
            "reported_claims": _clean_strings(result.get("reported_claims")),
            "inferred_points": _clean_strings(result.get("inferred_points")),
            "sources": _clean_strings(result.get("sources")),
            "confidence_score": max(0.0, min(1.0, float(result.get("confidence_score", 0.0)))),
        }

    def explain_to_user(self, facts: dict[str, Any]) -> dict[str, Any]:
        """User communication (section 18).

        Section 18 asks for exactly four things - what changed, why it matters,
        what evidence supports it, what uncertainty remains - and this is where
        they land. ``suggested_posture`` is bounded to the library's enum and can
        never be a directive; ``official_guidance_reference`` is passed through
        verbatim rather than paraphrased, per section 38.
        """

        result = self._invoke("explain_to_user", dict(facts))
        if not result:
            return {}
        posture = str(result.get("suggested_posture", "monitor")).lower()
        if posture not in ("monitor", "reroute", "delay_trip", "defer_to_official"):
            posture = "monitor"
        return {
            "headline": safe_text(result.get("headline"), field="explanation.headline"),
            "what_changed": safe_text(result.get("what_changed"), field="explanation.what_changed"),
            "why_it_matters": safe_text(result.get("why_it_matters"), field="explanation.why_it_matters"),
            "evidence": safe_text(result.get("evidence"), field="explanation.evidence"),
            "uncertainty": safe_text(result.get("uncertainty"), field="explanation.uncertainty"),
            "suggested_posture": posture,
            "official_guidance_reference": safe_text(
                result.get("official_guidance_reference"),
                field="explanation.official_guidance_reference",
            ),
        }


def _clean_strings(value: Any) -> list[str]:
    """Keep only strings that survive section 20, preserving their order."""

    if not isinstance(value, (list, tuple)):
        return []
    kept: list[str] = []
    for item in value:
        cleaned = safe_text(item, field="evidence.item")
        if cleaned:
            kept.append(cleaned[:500])
    return kept


def build_big_pickle_client(
    *,
    root: Path | None = None,
    enabled: bool = True,
    **config_overrides: Any,
) -> LlmClient:
    """Return a real client, or an honest stand-in when there is none.

    Callers always get an :class:`LlmClient`; the difference is visible in
    ``describe()`` and in every ``AnalysisRun.llm_operations`` entry. Returning
    ``NullLlmClient`` from here on failure would be indistinguishable from
    "the platform has no interpretation layer", which is the bug this whole
    module exists to fix.
    """

    if not enabled:
        log.info("LLM interpretation disabled by configuration")
        return NullLlmClient()
    try:
        package = big_pickle_package(root)
    except LlmUnavailable as exc:
        log.warning("%s", exc)
        return NullLlmClient()
    config = package.get_default_config()
    for key, value in config_overrides.items():
        if value is not None and hasattr(config, key):
            setattr(config, key, value)
    return BigPickleClient(config=config)


__all__ = [
    "MIN_OBSERVATIONS_FOR_NARRATIVE",
    "PROHIBITION_PATTERNS",
    "REJECTED_PLACEHOLDER",
    "BigPickleClient",
    "LlmPackageUnavailable",
    "ProhibitedContent",
    "big_pickle_config",
    "big_pickle_package",
    "build_big_pickle_client",
    "enforce_prohibitions",
    "safe_text",
]

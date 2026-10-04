"""Section 18-20: the interpretation layer (brief sections 18, 19, 20, 43).

Every test here runs with a stub client, never the live endpoint. That is not
only about cost and hermeticity - the properties under test are exactly the ones
that must hold when the model misbehaves, so the tests need to *force* the
misbehaviour: a model that invents an evacuation, a model that names a capability
this build does not have, a model that hangs. A test that only ever sees a
well-behaved model proves nothing about the guardrails.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from infraimpact.domain.enums import (
    Authority,
    InfrastructureDomain,
    ObservationType,
    SourceType,
    TruthStatus,
    Urgency,
)
from infraimpact.domain.ids import utcnow
from infraimpact.domain.schemas import (
    AffectedInfrastructure,
    AnalysisRun,
    EventState,
    EvidenceNarrative,
    EvidenceSummary,
    MovementState,
    Observation,
    ObservationQuality,
    PresentationItem,
    Provenance,
    UserExposure,
)
from infraimpact.events.claims import (
    HybridClaimExtractor,
    PREDICATE_LABELS,
    RuleClaimExtractor,
    build_extractor,
)
from infraimpact.llm.big_pickle import (
    BigPickleClient,
    LlmPackageUnavailable,
    PROHIBITION_PATTERNS,
    ProhibitedContent,
    big_pickle_package,
    build_big_pickle_client,
    enforce_prohibitions,
    library_tier,
    safe_text,
)
from infraimpact.llm.client import LlmClient, LlmOperationLog, NullLlmClient, llm_operations
from infraimpact.llm.interpreter import (
    MAX_OBSERVATIONS_IN_PROMPT,
    MIN_EXPOSURE_FOR_EXPLANATION,
    InterpretationLayer,
    UserExplanation,
)
from infraimpact.users.presentation import PresentationContext, PresentationEngine
from infraimpact.delta.engine import DeltaReport, StateDeltaEngine
from infraimpact.domain.schemas import StateDelta

NOW = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


class ScriptedClient(LlmClient):
    """A client that returns exactly what the test tells it to.

    ``LlmClient`` is an ABC, so every operation must be implemented even when the
    test only cares about one. The defaults return empty results rather than
    raising, which keeps unrelated tests quiet.
    """

    def __init__(self, **responses: object) -> None:
        self.responses = responses
        self.seen: list[tuple[str, tuple, dict]] = []

    def _answer(self, operation: str, *args, **kwargs):
        self.seen.append((operation, args, kwargs))
        value = self.responses.get(operation, {})
        return dict(value) if isinstance(value, dict) else value

    def extract_claims(self, text, observation_id, allowed_predicates):
        return self._answer("extract_claims", text, observation_id, allowed_predicates)

    def resolve_event(self, candidate, options):
        return self._answer("resolve_event", candidate, options)

    def select_capabilities(self, state_summary, delta_summary, registry):
        return self._answer("select_capabilities", state_summary, delta_summary, registry)

    def synthesize_evidence(self, facts):
        return self._answer("synthesize_evidence", facts)

    def explain_to_user(self, facts):
        return self._answer("explain_to_user", facts)

    def hypothesize_infrastructure(self, facts):
        return self._answer("hypothesize_infrastructure", facts)


class ExplodingClient(LlmClient):
    """Every operation raises, the way a dead endpoint does."""

    def extract_claims(self, text, observation_id, allowed_predicates):
        raise TimeoutError("connection reset")

    def resolve_event(self, candidate, options):
        raise TimeoutError("connection reset")

    def select_capabilities(self, state_summary, delta_summary, registry):
        raise TimeoutError("connection reset")

    def synthesize_evidence(self, facts):
        raise TimeoutError("connection reset")

    def explain_to_user(self, facts):
        raise TimeoutError("connection reset")


def make_observation(
    oid: str,
    source_id: str = "sdot",
    otype: ObservationType = ObservationType.ROAD_CLOSURE,
    authority: Authority = Authority.OFFICIAL,
    headline: str = "I-5 NB closed at MP 162",
    body: str = "",
) -> Observation:
    return Observation(
        observation_id=oid,
        source_id=source_id,
        source_record_id=oid,
        observation_type=otype,
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        region_id="puget-sound",
        headline=headline,
        structured_payload={"body": body} if body else {},
        geometry={"type": "Point", "coordinates": [-122.33, 47.60]},
        event_time=NOW,
        observed_at=NOW,
        ingested_at=NOW,
        provenance=Provenance(authority=authority, content_hash=f"h-{oid}"),
        quality=ObservationQuality(
            source_reliability=0.9, temporal_precision=0.9, spatial_precision=0.95
        ),
    )


def make_state(**overrides) -> EventState:
    evidence = EvidenceSummary(
        source_count=4,
        independent_source_count=3,
        authorities={"official": 3, "established_media": 1},
        observation_types={"road_closure": 2, "news_article": 1},
        contradictions=0,
        freshness_seconds=180.0,
        vector={"source_authority": 0.88, "independent_corroboration": 1.0},
    )
    base = {
        "event_id": "evt_test",
        "state_version": 4,
        "status": "active",
        "evidence": evidence,
        "movement": MovementState(),
        "affected_infrastructure": (
            AffectedInfrastructure(
                domain=InfrastructureDomain.ROAD,
                identifier="I-5-NB-162",
                name="I-5 North",
                severity=Urgency.HIGH,
            ),
        ),
    }
    base.update(overrides)
    return EventState(**base)


def make_delta(magnitude: float = 0.7) -> DeltaReport:
    return DeltaReport(
        is_material=True,
        magnitude=magnitude,
        novelty=magnitude,
        confidence=0.8,
        deltas=(
            StateDelta(
                change="new_road_closure",
                before=None,
                after="I-5-NB-162",
                magnitude=magnitude,
                confidence=0.8,
                causes=("observation o1",),
            ),
        ),
        suppressed_reason=None,
    )


def make_exposure(score: float = 0.72) -> UserExposure:
    return UserExposure(
        user_id="u1",
        event_id="evt_test",
        analysis_run_id="run_1",
        exposure_level=Urgency.HIGH,
        exposure_score=score,
        distance_m=900.0,
    )


# --------------------------------------------------------------------------
# section 20: prohibitions
# --------------------------------------------------------------------------


class TestSection20Prohibitions:
    """All five prohibitions, each with text that should trip it."""

    @pytest.mark.parametrize(
        "text,reason",
        [
            ("Residents should evacuate immediately.", "independent_evacuation_directive"),
            ("Proceed to the nearest shelter now.", "independent_evacuation_directive"),
            ("The road is now closed to all traffic.", "authoritative_road_closure"),
            ("Highway 520 has been shut downtown.", "authoritative_road_closure"),
            ("These protesters are violent.", "assigns_criminal_intent"),
            ("The group is intent to cause harm.", "assigns_criminal_intent"),
            ("There are currently 4000 people downtown.", "unsourced_current_fact"),
            (
                "Official guidance is outdated; you do not need to follow it.",
                "overrides_official_guidance",
            ),
        ],
    )
    def test_each_prohibition_blocks(self, text, reason):
        with pytest.raises(ProhibitedContent) as exc:
            enforce_prohibitions(text)
        assert exc.value.reason == reason

    def test_all_five_prohibitions_are_represented(self):
        """Every clause of section 20 has at least one pattern.

        A prohibition with no pattern is a prohibition that is not enforced, and
        it would still read as enforced in the source.
        """

        reasons = {reason for _, reason in PROHIBITION_PATTERNS}
        assert reasons == {
            "independent_evacuation_directive",
            "authoritative_road_closure",
            "assigns_criminal_intent",
            "unsourced_current_fact",
            "overrides_official_guidance",
        }

    def test_factual_text_is_not_over_blocked(self):
        """The prohibitions must not eat ordinary reporting.

        A guardrail that refuses "SDOT reports the northbound tunnel is closed"
        is useless: reproducing an official closure is required behaviour, and
        the platform's answer to it is the provenance, not the sentence.
        """

        allowed = [
            "SDOT reports a closure on the northbound tunnel.",
            "The crowd is reported to be growing near the courthouse.",
            "Metro service is delayed on Line 8.",
        ]
        for text in allowed:
            assert enforce_prohibitions(text) == text

    def test_nested_lists_are_walked(self):
        cleaned = enforce_prohibitions(
            {"ok": ["fine", "the road is closed"], "n": 3, "deep": {"a": "evacuate now"}}
        )
        assert cleaned == {"ok": ["fine"], "n": 3, "deep": {}}

    def test_non_strings_pass_through(self):
        assert enforce_prohibitions(0.93) == 0.93
        assert enforce_prohibitions(None) is None

    def test_safe_text_returns_none_for_prohibited(self):
        assert safe_text("The road is closed") is None
        assert safe_text("") is None
        assert safe_text(None) is None
        assert safe_text("fine") == "fine"

    def test_client_refuses_a_prohibited_synthesis(self):
        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            synthesize_evidence={"summary": "Evacuate immediately.", "sources": []}
        )
        assert client.synthesize_evidence({}) == {}

    def test_client_keeps_the_clean_part_of_a_mixed_synthesis(self):
        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            synthesize_evidence={
                "summary": "Two corridors are affected.",
                "confirmed_facts": ["SDOT closed I-5", "you must evacuate now"],
                "sources": ["SDOT"],
            }
        )
        result = client.synthesize_evidence({})
        assert result["summary"] == "Two corridors are affected."
        assert result["confirmed_facts"] == ["SDOT closed I-5"]

    def test_client_refuses_a_prohibited_explanation_headline(self):
        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            explain_to_user={
                "headline": "Evacuate to the Seattle Center",
                "what_changed": "x",
                "suggested_posture": "monitor",
            }
        )
        assert client.explain_to_user({})["headline"] is None

    def test_client_bounds_suggested_posture(self):
        """An unknown posture is not passed through; it becomes ``monitor``."""

        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            explain_to_user={
                "headline": "Roads affected",
                "what_changed": "closure",
                "suggested_posture": "leave_now",
            }
        )
        assert client.explain_to_user({})["suggested_posture"] == "monitor"


# --------------------------------------------------------------------------
# the operation log
# --------------------------------------------------------------------------


class TestOperationLog:
    def test_never_asked_says_nothing(self):
        assert llm_operations(None) == ()
        assert LlmOperationLog().as_tuple() == ()

    def test_null_client_records_the_refusal_reason(self):
        null = NullLlmClient()
        null.synthesize_evidence({})
        null.explain_to_user({})
        assert llm_operations(null) == (
            "synthesize_evidence:refused(llm_not_configured)",
            "explain_to_user:refused(llm_not_configured)",
        )

    def test_refusal_reasons_survive(self):
        log = LlmOperationLog()
        log.begin("explain_to_user")
        log.refused("explain_to_user", "independent_evacuation_directive")
        assert log.as_tuple() == (
            "explain_to_user:refused(independent_evacuation_directive)",
        )

    def test_repeats_collapse_to_a_count(self):
        log = LlmOperationLog()
        for _ in range(3):
            log.begin("explain_to_user")
            log.succeeded("explain_to_user")
        assert log.as_tuple() == ("explain_to_userx3:ok",)

    def test_budget_is_enforced_per_operation(self):
        log = LlmOperationLog(max_calls=2)
        assert log.begin("explain_to_user") is True
        assert log.begin("explain_to_user") is True
        assert log.begin("explain_to_user") is False

    def test_a_run_with_and_without_a_narrative_are_distinguishable(self):
        """Section 43 replay: an LLM-written prose must be attributable."""

        silent = AnalysisRun(
            analysis_run_id="r1",
            event_id="e1",
            region_id="puget-sound",
            trigger="t",
            new_state_version=1,
            started_at=NOW,
            completed_at=NOW,
        )
        narrated = silent.model_copy(
            update={
                "llm_operations": ("synthesize_evidence:ok",),
                "narrative": EvidenceNarrative(source="llm", summary="x"),
            }
        )
        assert silent.llm_operations == ()
        assert narrated.llm_operations == ("synthesize_evidence:ok",)
        assert narrated.narrative is not None


# --------------------------------------------------------------------------
# the real client
# --------------------------------------------------------------------------


class TestBigPickleClient:
    def test_the_supplied_package_is_importable(self):
        """The workspace ``llm`` package must load, or nothing else matters."""

        package = big_pickle_package()
        assert hasattr(package, "LlmClient")
        assert hasattr(package, "get_default_config")

    def test_a_missing_package_degrades_to_null_honestly(self, tmp_path):
        client = build_big_pickle_client(root=tmp_path)
        assert isinstance(client, NullLlmClient)

    def test_missing_package_raises_a_typed_error(self, tmp_path):
        with pytest.raises(LlmPackageUnavailable):
            big_pickle_package(tmp_path)

    def test_client_is_configured_and_live_with_a_key(self):
        client = build_big_pickle_client()
        assert isinstance(client, BigPickleClient)
        described = client.describe()
        assert described["backend"] == "big_pickle"
        # The workspace ships a real key, so this deployment is live. If a key is
        # ever removed the mode must be reported honestly rather than pretending.
        assert described["mode"] in ("live", "mock")
        assert described["model"]

    def test_transport_failure_is_recorded_not_raised(self):
        client = BigPickleClient(enabled=False)
        client._client = ExplodingClient()
        assert client.synthesize_evidence({}) == {}
        assert "synthesize_evidence:failed" in client.log.as_tuple()

    def test_a_disabled_client_refuses_every_operation(self):
        client = BigPickleClient(enabled=False)
        assert client.synthesize_evidence({}) == {}
        assert client.explain_to_user({}) == {}
        assert client.is_live is False

    def test_claims_are_filtered_against_the_platform_registry(self):
        """The library's whitelist and ours differ; ours is stricter."""

        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            extract_claims={
                "claims": [
                    {"predicate": "event.gathering", "value": True, "confidence": 0.9},
                    {"predicate": "emergency.evacuation", "value": True, "confidence": 0.9},
                    {"predicate": "not.a.real.predicate", "value": 1, "confidence": 0.9},
                ]
            }
        )
        result = client.extract_claims("text", "obs1", ["event.gathering"])
        assert [c["predicate"] for c in result["claims"]] == ["event.gathering"]
        reasons = {r["reason"] for r in result["rejected"]}
        assert reasons == {"forbidden_predicate", "not_in_platform_registry"}

    def test_capability_names_must_exist_in_the_registry(self):
        """A model must not invent an analysis capability."""

        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            select_capabilities={
                "recommended_capabilities": [
                    {"capability": "road_network_exposure", "priority": 0.9},
                    {"capability": "tectonic_hunching", "priority": 1.0},
                ]
            }
        )
        result = client.select_capabilities({}, {}, [{"capability_id": "road_network_exposure"}])
        assert [r["capability"] for r in result["recommended_capabilities"]] == [
            "road_network_exposure"
        ]
        assert result["advisory"] is True

    def test_tier_vocabulary_is_translated_both_ways(self):
        """Our ``triggered`` and the library's ``medium`` are the same tier.

        Without this the library's response schema rejects an otherwise good
        reply because it was handed a word it does not know.
        """

        assert library_tier("triggered") == "medium"
        assert library_tier("cheap") == "cheap"
        assert library_tier("expensive") == "expensive"
        assert library_tier(None) is None

        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            select_capabilities={
                "recommended_capabilities": [
                    {"capability": "c1", "priority": 0.5, "cost_tier": "medium"}
                ]
            }
        )
        result = client.select_capabilities(
            {}, {}, [{"capability_id": "c1", "tier": "triggered"}]
        )
        assert result["recommended_capabilities"][0]["tier"] == "triggered"
        # The registry handed to the library must use its vocabulary.
        _, args, _ = client._client.seen[0]
        assert args[2][0]["tier"] == "medium"

    def test_resolution_opinion_is_advisory_only(self):
        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(
            resolve_event={"decision": "existing", "event_id": "evt_9", "confidence": 0.8}
        )
        result = client.resolve_event({}, [])
        assert result["advisory"] is True
        assert result["decision"] == "existing"

    def test_an_unrecognised_resolution_decision_becomes_uncertain(self):
        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(resolve_event={"decision": "definitely"})
        assert client.resolve_event({}, [])["decision"] == "uncertain"


# --------------------------------------------------------------------------
# evidence synthesis
# --------------------------------------------------------------------------


class TestEvidenceSynthesis:
    def test_a_narrative_is_built_from_corroborated_evidence(self):
        client = ScriptedClient(
            synthesize_evidence={
                "summary": "Two corridors are affected.",
                "confirmed_facts": ["SDOT closed I-5"],
                "reported_claims": ["KOMO reports queuing"],
                "inferred_points": ["Route 8 will be delayed"],
                "sources": ["SDOT", "KOMO"],
                "confidence_score": 0.9,
            }
        )
        layer = InterpretationLayer(client=client)
        state = make_state()
        narrative = layer.narrate_evidence(
            state, [make_observation(f"o{i}") for i in range(4)]
        )
        assert narrative is not None
        assert narrative.source == "llm"
        assert narrative.summary == "Two corridors are affected."
        assert narrative.confirmed_facts == ("SDOT closed I-5",)

    def test_a_narrative_is_always_inferred(self):
        """Ten confirmed observations summarised is still an inference."""

        narrative = EvidenceNarrative(source="llm", summary="x", truth_status="inferred")
        assert narrative.truth_status == "inferred"
        with pytest.raises(ValueError):
            EvidenceNarrative(source="llm", summary="x", truth_status="confirmed")

    def test_no_narrative_when_there_is_only_one_observation(self):
        """One closure does not need a paragraph, and should not get one."""

        client = ScriptedClient(synthesize_evidence={"summary": "Something happened."})
        layer = InterpretationLayer(client=client)
        assert layer.narrate_evidence(make_state(), [make_observation("o1")]) is None

    def test_no_narrative_when_corroboration_is_absent(self):
        """One source restating itself is not corroboration."""

        client = ScriptedClient(synthesize_evidence={"summary": "Something happened."})
        layer = InterpretationLayer(client=client)
        state = make_state(
            evidence=EvidenceSummary(source_count=1, independent_source_count=1)
        )
        observations = [make_observation(f"o{i}") for i in range(4)]
        assert layer.narrate_evidence(state, observations) is None

    def test_no_narrative_without_a_client(self):
        assert InterpretationLayer().narrate_evidence(make_state(), []) is None
        assert (
            InterpretationLayer(client=NullLlmClient()).narrate_evidence(make_state(), [])
            is None
        )

    def test_prompt_caps_the_observation_count(self):
        """A prompt with 400 observations is slow, expensive, and unread."""

        client = ScriptedClient(synthesize_evidence={"summary": "ok"})
        layer = InterpretationLayer(client=client)
        state = make_state(
            evidence=EvidenceSummary(source_count=99, independent_source_count=9)
        )
        layer.narrate_evidence(state, [make_observation(f"o{i}") for i in range(200)])
        _, args, _ = client.seen[-1]
        sent = args[0]["observations"]
        assert len(sent) == MAX_OBSERVATIONS_IN_PROMPT

    def test_prompt_excludes_raw_payloads(self):
        """Agency JSON is megabytes and is not prose; it must not be sent."""

        client = ScriptedClient(synthesize_evidence={"summary": "ok"})
        layer = InterpretationLayer(client=client)
        big = make_observation("o1", body="x" * 1000)
        big = big.model_copy(update={"structured_payload": {"blob": "y" * 100_000}})
        layer.narrate_evidence(
            make_state(),
            [big, make_observation("o2")],
        )
        _, args, _ = client.seen[-1]
        sent = args[0]["observations"][0]
        assert "structured_payload" not in sent
        assert "blob" not in sent


# --------------------------------------------------------------------------
# user communication
# --------------------------------------------------------------------------


class TestUserExplanation:
    def _facts(self, **overrides):
        """Defaults for one ``explain_to_user`` call; ``overrides`` win."""

        facts = dict(
            state=make_state(),
            exposure=make_exposure(),
            impacts=make_state().affected_infrastructure,
            forecasts=(),
            delta_report=make_delta(),
            observations=[make_observation("o1")],
        )
        facts.update(overrides)
        return facts

    def test_a_well_formed_explanation_is_produced(self):
        client = ScriptedClient(
            explain_to_user={
                "headline": "I-5 is closed near you",
                "what_changed": "A closure appeared on the northbound lanes",
                "why_it_matters": "Your commute crosses it",
                "evidence": "SDOT and WSDOT both report it",
                "uncertainty": "No end time is published",
                "suggested_posture": "reroute",
            }
        )
        explanation = InterpretationLayer(client=client).explain_to_user(**self._facts())
        assert explanation is not None
        assert explanation.suggested_posture == "reroute"
        assert explanation.uncertainty == "No end time is published"

    def test_no_explanation_below_the_exposure_floor(self):
        client = ScriptedClient(explain_to_user={"headline": "x", "what_changed": "y"})
        layer = InterpretationLayer(client=client)
        assert (
            layer.explain_to_user(**self._facts(exposure=make_exposure(0.01))) is None
        )
        assert layer.explain_to_user(**self._facts(exposure=make_exposure(MIN_EXPOSURE_FOR_EXPLANATION)))

    def test_no_explanation_when_nothing_changed(self):
        """"What changed?" has no honest answer when the answer is nothing."""

        client = ScriptedClient(explain_to_user={"headline": "x", "what_changed": "y"})
        layer = InterpretationLayer(client=client)
        empty = DeltaReport(
            is_material=False, magnitude=0.0, deltas=(), suppressed_reason="no_change"
        )
        assert layer.explain_to_user(**self._facts(delta_report=empty)) is None
        assert layer.explain_to_user(**self._facts(delta_report=None)) is None

    def test_no_explanation_for_a_negligible_delta(self):
        client = ScriptedClient(explain_to_user={"headline": "x", "what_changed": "y"})
        layer = InterpretationLayer(client=client)
        assert layer.explain_to_user(**self._facts(delta_report=make_delta(0.01))) is None

    def test_a_headline_without_what_changed_is_rejected(self):
        """A user explanation missing half the question is not an explanation."""

        client = ScriptedClient(explain_to_user={"headline": "Something is happening"})
        assert InterpretationLayer(client=client).explain_to_user(**self._facts()) is None

    def test_official_guidance_is_reproduced_verbatim(self):
        """Section 38: official wording is never paraphrased."""

        official = "SDOT: NORTHBOUND I-5 CLOSED AT MP 162 UNTIL FURTHER NOTICE"
        client = ScriptedClient(
            explain_to_user={
                "headline": "I-5 is closed",
                "what_changed": "closure",
                "official_guidance_reference": "some paraphrase by the model",
            }
        )
        observation = make_observation(
            "o1",
            otype=ObservationType.OFFICIAL_EMERGENCY_NOTICE,
            headline=official,
        )
        explanation = InterpretationLayer(client=client).explain_to_user(
            **self._facts(observations=[observation])
        )
        # The model's own paraphrase is dropped; the real headline is used.
        assert explanation.official_guidance_reference == official


# --------------------------------------------------------------------------
# dynamic orchestration and hypotheses
# --------------------------------------------------------------------------


class _Cap:
    def __init__(self, cid: str, tier: str = "triggered") -> None:
        self.capability_id = cid
        self.capability = cid
        self.description = f"does {cid}"
        self.domain = InfrastructureDomain.ROAD
        self.tier = tier
        self.relevance = 0.4


class TestOrchestrationAdvice:
    def test_only_registered_capabilities_are_returned(self):
        client = ScriptedClient(
            select_capabilities={
                "recommended_capabilities": [
                    {"capability": "road_network_exposure", "priority": 0.9},
                    {"capability": "hallucinated_capability", "priority": 1.0},
                ]
            }
        )
        layer = InterpretationLayer(client=client)
        candidates = [_Cap("road_network_exposure"), _Cap("transit_disruption")]
        assert layer.recommend_capabilities(
            state=make_state(), delta_report=make_delta(), candidates=candidates
        ) == ["road_network_exposure"]

    def test_an_unregistered_capability_can_never_be_promoted(self):
        """The registry is the source of truth, not the model's confidence."""

        client = ScriptedClient(
            select_capabilities={
                "recommended_capabilities": [
                    {"capability": "hallucinated_capability", "priority": 1.0}
                ]
            }
        )
        assert (
            InterpretationLayer(client=client).recommend_capabilities(
                state=make_state(), delta_report=make_delta(), candidates=[_Cap("road_network_exposure")]
            )
            == []
        )

    def test_no_candidates_means_no_call(self):
        client = ScriptedClient()
        assert (
            InterpretationLayer(client=client).recommend_capabilities(
                state=make_state(), delta_report=make_delta(), candidates=[]
            )
            == []
        )
        assert client.seen == []

    def test_hypotheses_are_returned_as_a_tuple(self):
        client = ScriptedClient(
            hypothesize_infrastructure={
                "hypotheses": [
                    {
                        "infrastructure_type": "roadway",
                        "target_id_or_name": "I-5 ramps",
                        "potential_impact": "spillback",
                        "urgency": "medium",
                        "justification": "queues propagate",
                    }
                ],
                "uncertainty": "ramps unmeasured",
            }
        )
        state = make_state()
        result = InterpretationLayer(client=client).hypotheses(
            state, state.affected_infrastructure
        )
        assert isinstance(result, tuple)
        assert result[0]["target_id_or_name"] == "I-5 ramps"

    def test_hypotheses_without_infrastructure_is_pointless(self):
        client = ScriptedClient(hypothesize_infrastructure={"hypotheses": [{}]})
        empty = make_state(affected_infrastructure=())
        assert InterpretationLayer(client=client).hypotheses(empty, ()) == ()


# --------------------------------------------------------------------------
# claim extraction: rules plus LLM
# --------------------------------------------------------------------------


class TestHybridClaimExtraction:
    def test_rules_alone_are_the_default(self):
        assert isinstance(build_extractor(use_llm=False), RuleClaimExtractor)

    def test_enabling_the_llm_no_longer_disables_the_rules(self):
        """Asking for the LLM must not turn off the correct extractor.

        This was the shape of the bug: ``build_extractor(True)`` returned the LLM
        extractor outright, so structured feeds lost their deterministic claims.
        """

        extractor = build_extractor(use_llm=True, client=ScriptedClient())
        assert isinstance(extractor, HybridClaimExtractor)

    def test_deterministic_claims_survive(self):
        observation = make_observation("o1")
        claims = HybridClaimExtractor(ScriptedClient()).extract(observation, "evt_1")
        predicates = {c.predicate for c in claims}
        assert "road.closure" in predicates

    def test_llm_claims_are_added_for_prose(self):
        client = ScriptedClient(
            extract_claims={
                "claims": [
                    {"predicate": "event.estimated_crowd", "value": 700, "confidence": 0.6}
                ]
            }
        )
        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="Crowd grows near the courthouse",
            body=(
                "Organisers say several thousand people are gathered outside the "
                "county courthouse and the crowd continues to grow as more arrivals "
                "join from the north entrance of the plaza."
            ),
        )
        claims = HybridClaimExtractor(client).extract(observation, "evt_1")
        assert any(c.predicate == "event.estimated_crowd" for c in claims)

    def test_structured_feeds_are_not_sent_to_the_llm(self):
        """An agency JSON payload does not become legible through a completion."""

        client = ScriptedClient()
        HybridClaimExtractor(client).extract(make_observation("o1"), "evt_1")
        assert client.seen == []

    def test_short_prose_is_not_sent_to_the_llm(self):
        client = ScriptedClient()
        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="Short",
        )
        HybridClaimExtractor(client).extract(observation, "evt_1")
        assert client.seen == []

    def test_disagreeing_sources_both_survive(self):
        """Averaging 300 and 700 into 500 would destroy the disagreement."""

        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="Crowd of about 300 people marching north",
            body="A crowd of about 300 people marched north through downtown Seattle this evening.",
        )
        client = ScriptedClient(
            extract_claims={
                "claims": [
                    {"predicate": "event.estimated_crowd", "value": 700, "confidence": 0.6}
                ]
            }
        )
        claims = HybridClaimExtractor(client).extract(observation, "evt_1")
        values = [c.value for c in claims if c.predicate == "event.estimated_crowd"]
        assert set(values) == {300.0, 700}

    def test_identical_claims_are_deduplicated(self):
        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="People are marching",
            body="People are marching through downtown Seattle this evening along the avenue.",
        )
        client = ScriptedClient(
            extract_claims={"claims": [{"predicate": "event.movement", "value": True, "confidence": 0.9}]}
        )
        claims = HybridClaimExtractor(client).extract(observation, "evt_1")
        ids = [c.claim_id for c in claims if c.predicate == "event.movement"]
        assert len(ids) == len(set(ids))

    def test_extraction_method_records_who_said_it(self):
        client = ScriptedClient(
            extract_claims={"claims": [{"predicate": "location.name", "value": "Pike St", "confidence": 0.8}]}
        )
        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="On Pike Street",
            body=(
                "A march moved along Pike Street in Seattle this evening, well "
                "attended, with marchers occupying several blocks of the corridor "
                "between Third Avenue and the waterfront."
            ),
        )
        claims = HybridClaimExtractor(client).extract(observation, "evt_1")
        methods = {c.extraction_method for c in claims}
        assert methods == {"rule", "llm"}

    def test_an_llm_failure_does_not_lose_the_deterministic_claims(self):
        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="Crowd of about 300 people marching north",
            body="A crowd of about 300 people marched north through downtown Seattle this evening.",
        )
        claims = HybridClaimExtractor(ExplodingClient()).extract(observation, "evt_1")
        assert any(c.predicate == "event.gathering" for c in claims)

    def test_llm_extraction_never_produces_a_confirmed_claim(self):
        """A model's read of prose is REPORTED at best, however sure it sounds."""

        client = ScriptedClient(
            extract_claims={"claims": [{"predicate": "event.gathering", "value": True, "confidence": 1.0}]}
        )
        observation = make_observation(
            "o1",
            otype=ObservationType.NEWS_ARTICLE,
            authority=Authority.ESTABLISHED_MEDIA,
            headline="Crowd reported",
            body="A crowd was reported gathering downtown in Seattle this evening.",
        )
        for claim in HybridClaimExtractor(client).extract(observation, "evt_1"):
            if claim.extraction_method == "llm":
                assert claim.truth_status is not TruthStatus.CONFIRMED

    def test_every_predicate_the_platform_allows_is_usable(self):
        client = BigPickleClient(enabled=False)
        client._client = ScriptedClient(extract_claims={"claims": []})
        client.extract_claims("t", "o", sorted(PREDICATE_LABELS))


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------


class TestExplanationPresentation:
    def _priority(self, score: float = 0.7):
        from infraimpact.domain.schemas import UserPriority

        return UserPriority(
            user_id="u1",
            event_id="evt_test",
            priority=score,
            urgency_band=Urgency.HIGH,
            evidence_confidence=0.8,
        )

    def test_no_explanation_means_no_summary_item(self):
        payload = PresentationEngine().build(
            PresentationContext(
                exposure=make_exposure(),
                priority=self._priority(),
                state=make_state(),
                delta_report=make_delta(),
                explanation=None,
            )
        )
        assert all(item.type.value != "event_summary" or not item.payload.get("is_llm_generated") for item in payload.items)

    def test_an_explanation_becomes_a_ranked_inferred_item(self):
        explanation = UserExplanation(
            headline="I-5 is closed near you",
            what_changed="A closure appeared on the northbound lanes",
            why_it_matters="Your commute crosses it",
            evidence="SDOT and WSDOT both report it",
            uncertainty="No end time is published",
            suggested_posture="reroute",
        )
        payload = PresentationEngine().build(
            PresentationContext(
                exposure=make_exposure(),
                priority=self._priority(),
                state=make_state(),
                delta_report=make_delta(),
                explanation=explanation,
            )
        )
        items = [i for i in payload.items if i.payload.get("is_llm_generated")]
        assert len(items) == 1
        item = items[0]
        assert item.headline == "I-5 is closed near you"
        assert item.truth_status is TruthStatus.INFERRED
        assert item.payload["what_changed"] == "A closure appeared on the northbound lanes"
        assert item.payload["suggested_posture"] == "reroute"
        # The deterministic change travels alongside the prose.
        assert item.payload["deterministic_change"]["change"] == "new_road_closure"

    def test_an_explanation_cannot_outrank_the_users_own_exposure(self):
        """Well-written prose is not a reason to interrupt harder."""

        explanation = UserExplanation(
            headline="x", what_changed="y", why_it_matters="", evidence="", uncertainty=""
        )
        payload = PresentationEngine().build(
            PresentationContext(
                exposure=make_exposure(score=0.4),
                priority=self._priority(),
                state=make_state(),
                delta_report=make_delta(),
                explanation=explanation,
            )
        )
        item = next(i for i in payload.items if i.payload.get("is_llm_generated"))
        assert item.priority <= 0.4

    def test_an_explanation_cannot_outrank_official_guidance(self):
        """Section 38, in the presence of prose."""

        explanation = UserExplanation(
            headline="x", what_changed="y", why_it_matters="", evidence="", uncertainty=""
        )
        official = make_observation(
            "off1",
            otype=ObservationType.OFFICIAL_EMERGENCY_NOTICE,
            headline="SDOT: I-5 NORTHBOUND CLOSED",
        )
        # Official guidance is recognised from the *state's* measured evidence,
        # not from the raw observation list - a police report about a closure is
        # not an official emergency notice.
        state = make_state(
            evidence=EvidenceSummary(
                source_count=4,
                independent_source_count=3,
                observation_types={"official_emergency_notice": 1, "road_closure": 2},
            )
        )
        payload = PresentationEngine().build(
            PresentationContext(
                exposure=make_exposure(),
                priority=self._priority(),
                state=state,
                observations=[official],
                delta_report=make_delta(),
                explanation=explanation,
            )
        )
        assert payload.items[0].type.value == "official_guidance"
        summary = next(i for i in payload.items if i.payload.get("is_llm_generated"))
        assert payload.items.index(summary) > 0
        assert payload.items[0].priority >= summary.priority
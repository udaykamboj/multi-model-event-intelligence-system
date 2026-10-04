"""Tests for historical similarity engine and capability (section 31)."""

import pytest

from infraimpact.analysis.capabilities import CapabilityContext
from infraimpact.analysis.similarity import (
    HistoricalSimilarityCapability,
    HistoricalSimilarityEngine,
)
from infraimpact.domain.enums import Authority, ObservationType, SourceType
from infraimpact.domain.geo import point
from infraimpact.domain.schemas import EventState, FeatureValue, Observation, ObservationQuality, Provenance


def test_historical_similarity_engine_retrieval():
    engine = HistoricalSimilarityEngine()

    analogues = engine.find_analogues(
        target_features={
            "crowd_estimate": 1000,
            "arterial_overlap_count": 2,
            "transit_route_overlap": 3,
        },
        centroid=(-122.338, 47.609),  # Westlake / Downtown Seattle
        top_k=3,
    )

    assert len(analogues) == 3
    assert analogues[0].similarity_score > 0.5
    assert analogues[0].roads_affected >= 0
    assert len(analogues[0].observed_trajectory) >= 1
    assert len(analogues[0].subsequent_consequences) >= 1


def test_historical_similarity_capability():
    cap = HistoricalSimilarityCapability()
    obs = Observation(
        observation_id="obs_sim_1",
        source_id="news.rss",
        source_record_id="rec_sim_1",
        source_type=SourceType.ESTABLISHED_NEWS,
        observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
        observed_at="2026-10-04T15:00:00Z",
        ingested_at="2026-10-04T15:00:00Z",
        event_time="2026-10-04T15:00:00Z",
        geometry=point(-122.338, 47.609),
        provenance=Provenance(authority=Authority.COMMUNITY, content_hash="hash_sim"),
        quality=ObservationQuality(source_reliability=0.8),
    )
    state = EventState(
        event_id="evt_test_sim",
        state_version=1,
        event_type_distribution={"demonstration": 0.9},
        status="active",
        geometry=point(-122.338, 47.609),
        first_observed="2026-10-04T15:00:00Z",
        last_observed="2026-10-04T15:00:00Z",
    )
    ctx = CapabilityContext(
        region_id="puget_sound",
        state=state,
        observations=[obs],
        claims=[],
        features={
            "estimated_crowd": FeatureValue(name="estimated_crowd", value=1200),
            "arterial_overlap_count": FeatureValue(name="arterial_overlap_count", value=2),
        },
    )

    result = cap.run(ctx)
    assert "historical_analogues_count" in result.features
    assert result.features["historical_analogues_count"].value >= 1
    assert "historical_similarity_score" in result.features
    assert len(result.notes) >= 1

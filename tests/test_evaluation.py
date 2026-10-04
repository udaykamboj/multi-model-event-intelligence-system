"""Tests for model evaluation, point-in-time dataset generation, and continuous learning (sections 45, 46, 48)."""

from datetime import datetime, timedelta
import pytest

from infraimpact.domain.enums import Authority, ObservationType, SourceType
from infraimpact.domain.geo import point
from infraimpact.domain.schemas import EventState, Observation, ObservationQuality, Provenance
from infraimpact.evaluation.learning import ContinuousLearningPipeline
from infraimpact.evaluation.metrics import (
    brier_score,
    expected_calibration_error,
    evaluate_forecasts,
    mean_absolute_error,
)
from infraimpact.evaluation.training import PointInTimeDatasetBuilder
from infraimpact.models.base import ModelDeploymentMode, ModelTaskType
from infraimpact.models.portfolio import BaselineInfrastructureImpactModel
from infraimpact.models.registry import ModelRegistry
from infraimpact.storage.sqlite_driver import SqlitePlatformRepository


def test_evaluation_metrics_calculation():
    # Predictions and binary outcomes
    preds = [0.9, 0.8, 0.2, 0.1, 0.7]
    actuals = [1.0, 1.0, 0.0, 0.0, 1.0]

    brier = brier_score(preds, actuals)
    assert 0.0 <= brier <= 0.1  # Excellent predictions have low Brier score

    ece = expected_calibration_error(preds, actuals)
    assert 0.0 <= ece <= 0.3

    report = evaluate_forecasts(list(zip(preds, actuals, [30] * 5)))
    assert report.brier_score == brier
    assert report.expected_calibration_error == ece
    assert report.sample_count == 5
    assert report.precision > 0.8


def test_point_in_time_dataset_builder(tmp_path):
    repo = SqlitePlatformRepository(f"sqlite:///{tmp_path / 'ledger.db'}")

    t0 = datetime.fromisoformat("2026-10-04T12:00:00+00:00")
    t1 = t0 + timedelta(minutes=10)
    t2 = t0 + timedelta(minutes=45)

    # Ingest observations over time
    obs1 = Observation(
        observation_id="eval_obs_1",
        source_id="wsdot.alerts",
        source_record_id="rec_wsdot_1",
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=ObservationType.ROAD_CLOSURE,
        observed_at=t0,
        ingested_at=t0,
        event_time=t0,
        event_id="evt_eval_1",
        geometry=point(-122.33, 47.60),
        provenance=Provenance(authority=Authority.OFFICIAL, content_hash="hash1", upstream_id="r1"),
        quality=ObservationQuality(source_reliability=0.9),
    )
    obs2 = Observation(
        observation_id="eval_obs_2",
        source_id="metro.alerts",
        source_record_id="rec_metro_1",
        source_type=SourceType.OFFICIAL_MACHINE_READABLE,
        observation_type=ObservationType.TRANSIT_SERVICE_ALERT,
        observed_at=t1,
        ingested_at=t1,
        event_time=t1,
        event_id="evt_eval_1",
        geometry=point(-122.33, 47.60),
        provenance=Provenance(authority=Authority.OFFICIAL, content_hash="hash2", upstream_id="r2"),
        quality=ObservationQuality(source_reliability=0.9),
    )
    obs3 = Observation(
        observation_id="eval_obs_3",
        source_id="news.feed",
        source_record_id="rec_news_1",
        source_type=SourceType.ESTABLISHED_NEWS,
        observation_type=ObservationType.PUBLIC_GATHERING_REPORT,
        observed_at=t2,
        ingested_at=t2,
        event_time=t2,
        event_id="evt_eval_1",
        geometry=point(-122.33, 47.60),
        provenance=Provenance(authority=Authority.COMMUNITY, content_hash="hash3", upstream_id="r3"),
        quality=ObservationQuality(source_reliability=0.7),
    )
    repo.observations.append(obs1)
    repo.observations.append(obs2)
    repo.observations.append(obs3)

    repo.events.ensure("evt_eval_1", t0, "puget_sound")
    repo.events.link_observation("evt_eval_1", obs1.observation_id)
    repo.events.link_observation("evt_eval_1", obs2.observation_id)
    repo.events.link_observation("evt_eval_1", obs3.observation_id)

    state = EventState(
        event_id="evt_eval_1",
        state_version=1,
        status="active",
        first_observed=t0,
        last_observed=t2,
    )
    repo.states.append_state(state)

    builder = PointInTimeDatasetBuilder(repo)
    examples = builder.build_examples_for_event(
        event_id="evt_eval_1",
        horizons_minutes=(15, 30),
        sample_step_minutes=15,
    )

    assert len(examples) >= 1
    # Verify no future leakage: an example at t0 must NOT contain transit observations from t1
    first_ex = examples[0]
    assert first_ex.as_of == t0
    assert first_ex.features["transit_alert_count"] == 0


def test_continuous_learning_pipeline_evaluation(tmp_path):
    repo = SqlitePlatformRepository(f"sqlite:///{tmp_path / 'learning.db'}")

    reg = ModelRegistry()
    champ = BaselineInfrastructureImpactModel(mode=ModelDeploymentMode.CHAMPION, version="v1.0")
    challenger = BaselineInfrastructureImpactModel(mode=ModelDeploymentMode.CHALLENGER, version="v2.0")
    reg.register(champ)
    reg.register(challenger)

    pipeline = ContinuousLearningPipeline(repo, reg)
    comparison = pipeline.compare_champion_challenger(
        task_type=ModelTaskType.INFRASTRUCTURE_IMPACT,
        dataset=[],  # Empty test dataset handles gracefully
    )
    assert comparison["task_type"] == "infrastructure_impact"
    assert "candidate_promoted" in comparison

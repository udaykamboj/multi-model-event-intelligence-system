"""Tests for predictive ML models, registry, and portfolio (sections 23-27, 44, 47)."""

from datetime import datetime
import pytest

from infraimpact.domain.enums import InfrastructureDomain, TruthStatus
from infraimpact.domain.schemas import FeatureValue, ModelOutput
from infraimpact.models.base import (
    ModelContext,
    ModelDeploymentMode,
    ModelTaskType,
)
from infraimpact.models.portfolio import (
    BaselineEventClassifier,
    BaselineInfrastructureImpactModel,
    BaselineTimeToImpactModel,
    BaselineTrafficPredictionModel,
    BaselineTransitDisruptionModel,
    build_default_model_registry,
)
from infraimpact.models.registry import ModelRegistry


def test_model_registry_lifecycle():
    reg = ModelRegistry()
    champ = BaselineEventClassifier(mode=ModelDeploymentMode.CHAMPION, version="v1")
    shadow = BaselineEventClassifier(mode=ModelDeploymentMode.SHADOW, version="v2")

    reg.register(champ)
    reg.register(shadow)

    assert reg.get_champion(ModelTaskType.EVENT_CLASSIFICATION).model_id == "event-classifier-v1"
    assert len(reg.get_shadows(ModelTaskType.EVENT_CLASSIFICATION)) == 1
    assert reg.get_shadows(ModelTaskType.EVENT_CLASSIFICATION)[0].model_id == "event-classifier-v2"

    # Promote shadow to champion
    promoted = reg.promote("event-classifier-v2", ModelDeploymentMode.CHAMPION)
    assert promoted is True
    assert reg.get_champion(ModelTaskType.EVENT_CLASSIFICATION).model_id == "event-classifier-v2"
    # Old champion demoted to challenger
    assert reg.get("event-classifier-v1").deployment_mode == ModelDeploymentMode.CHALLENGER


def test_baseline_event_classifier():
    model = BaselineEventClassifier()
    ctx = ModelContext(
        event_id="evt_test",
        state_version=1,
        features={
            "permit_event_present": FeatureValue(name="permit_event_present", value=True),
            "estimated_crowd": FeatureValue(name="estimated_crowd", value=1500),
        },
    )
    pred = model.predict(ctx)
    assert pred.task_type == ModelTaskType.EVENT_CLASSIFICATION
    assert "dominant_class" in pred.outputs
    assert pred.confidence > 0.0
    assert pred.uncertainty is not None

    # Test Section 44 ModelOutput conversion
    out = model.to_model_output(pred, state_version=1)
    assert isinstance(out, ModelOutput)
    assert out.model_id == model.model_id
    assert out.input_state_version == 1
    assert out.probability == pred.confidence


def test_baseline_infrastructure_impact_model():
    model = BaselineInfrastructureImpactModel()
    ctx = ModelContext(
        event_id="evt_test",
        state_version=2,
        features={
            "arterial_overlap_count": FeatureValue(name="arterial_overlap_count", value=2),
            "transit_route_overlap": FeatureValue(name="transit_route_overlap", value=3),
        },
    )
    pred = model.predict(ctx)
    assert pred.task_type == ModelTaskType.INFRASTRUCTURE_IMPACT
    assert len(pred.forecasts) >= 1
    for f in pred.forecasts:
        assert f.truth_status == TruthStatus.PREDICTED
        assert 0.0 <= f.probability <= 1.0
        assert f.horizon_minutes in (5, 15, 30, 60)


def test_baseline_transit_disruption_model():
    model = BaselineTransitDisruptionModel()
    ctx = ModelContext(
        event_id="evt_transit",
        state_version=1,
        features={
            "transit_route_overlap": FeatureValue(name="transit_route_overlap", value=4),
            "road_closure_active": FeatureValue(name="road_closure_active", value=True),
        },
    )
    pred = model.predict(ctx)
    assert pred.task_type == ModelTaskType.TRANSIT_DISRUPTION
    assert pred.outputs["expected_delay_minutes"] > 0.0
    assert 0.0 <= pred.outputs["cancellation_probability"] <= 1.0


def test_default_model_registry_contains_portfolio():
    reg = build_default_model_registry(require_trained=True)
    assert len(reg) >= 7
    summary = reg.list_models()
    assert len(summary) >= 7
    tasks = {m["task_type"] for m in summary}
    assert "event_classification" in tasks
    assert "infrastructure_impact" in tasks
    assert "time_to_impact" in tasks
    assert "traffic_prediction" in tasks
    assert "transit_disruption" in tasks
    assert "spatial_propagation" in tasks
    assert "user_priority_ranking" in tasks


def test_untrained_models_require_weights_without_fabrication():
    """Verify Section 23-27 boundary: untrained models never fabricate forecasts."""
    from infraimpact.models.base import PredictionStatus
    from infraimpact.models.portfolio import (
        EventClassificationModel,
        InfrastructureImpactModel,
        SpatialPropagationModel,
        TimeToImpactModel,
        TrafficPredictionModel,
        TransitDisruptionModel,
        UserPriorityRankingModel,
    )

    models = [
        EventClassificationModel(is_trained=False),
        InfrastructureImpactModel(is_trained=False),
        TimeToImpactModel(is_trained=False),
        TrafficPredictionModel(is_trained=False),
        TransitDisruptionModel(is_trained=False),
        SpatialPropagationModel(),
        UserPriorityRankingModel(),
    ]

    ctx = ModelContext(
        event_id="evt_test",
        state_version=1,
        features={"arterial_overlap_count": FeatureValue(name="arterial_overlap_count", value=2)},
    )

    for model in models:
        assert model.is_trained is False
        pred = model.predict(ctx)

        # Must report MODEL_REQUIRED and is_placeholder
        assert pred.status == PredictionStatus.MODEL_REQUIRED
        assert pred.is_placeholder is True
        assert pred.confidence == 0.0
        assert pred.uncertainty == 1.0

        # Must NOT fabricate fake forecasts
        assert len(pred.forecasts) == 0

        # Must link to specification doc
        assert "specification" in pred.outputs
        assert pred.outputs["status"] == "MODEL_REQUIRED"
        # Each model must state its own actual reason for not answering. "No
        # weights yet" and "no implementation exists" call for different work,
        # so a single required phrasing would flatten a distinction the reader
        # needs. Models F and G have no inference at all and say so.
        if "reason" in pred.outputs:
            assert pred.outputs["reason"] == "not_implemented"
            assert any("no implemented inference" in note for note in pred.notes)
        else:
            assert any("requires trained weights" in note for note in pred.notes)


def test_models_f_and_g_cannot_be_switched_into_claiming_success():
    """Models with no inference must not offer a flag that fakes one.

    Their previous ``is_trained=True`` paths returned COMPLETED with empty
    results - a cascade across zero edges, and a ranking of nothing, both at
    high confidence. The flag is gone rather than honoured, because with no
    inference there is no honest value it could take.
    """

    from infraimpact.models.base import PredictionStatus
    from infraimpact.models.portfolio import (
        SpatialPropagationModel,
        UserPriorityRankingModel,
    )

    ctx = ModelContext(event_id="evt_test", state_version=1, features={})

    for model, empty_key in (
        (SpatialPropagationModel(), "propagated_edges"),
        (UserPriorityRankingModel(), "ranked_item_ids"),
    ):
        assert model.is_trained is False

        with pytest.raises(TypeError):
            model.__class__(is_trained=True)

        pred = model.predict(ctx)
        assert pred.status == PredictionStatus.MODEL_REQUIRED
        assert pred.is_placeholder is True
        assert pred.confidence == 0.0
        # The empty list is reported as an absence of a prediction, not as one.
        assert pred.outputs[empty_key] == []
        assert pred.outputs["reason"] == "not_implemented"


def test_model_specifications_exist_and_complete():
    """Verify all 7 predictive ML model specifications exist with all 11 required sections."""
    from pathlib import Path

    docs_dir = Path("docs/models")
    assert docs_dir.is_dir(), "docs/models directory must exist"

    expected_specs = [
        "MODEL_A_EVENT_CLASSIFICATION.md",
        "MODEL_B_INFRASTRUCTURE_IMPACT.md",
        "MODEL_C_TIME_TO_IMPACT.md",
        "MODEL_D_TRAFFIC_PREDICTION.md",
        "MODEL_E_TRANSIT_DISRUPTION.md",
        "MODEL_F_SPATIAL_PROPAGATION.md",
        "MODEL_G_USER_PRIORITY_RANKING.md",
    ]

    required_keywords = [
        "What the Model Predicts",
        "Why the System Needs It",
        "Required Inputs and Features",
        "Expected Output Contract",
        "Prediction Horizons",
        "Training Data Requirements",
        "Target / Ground Truth",
        "Evaluation Metrics",
        "Uncertainty & Calibration Requirements",
        "Where It Connects to the System",
        "Interface the Eventual Model Must Implement",
    ]

    for filename in expected_specs:
        spec_path = docs_dir / filename
        assert spec_path.is_file(), f"Specification file {filename} is missing"
        content = spec_path.read_text(encoding="utf-8")
        for kw in required_keywords:
            assert kw.lower() in content.lower(), f"Missing section '{kw}' in {filename}"


def test_untrained_models_cannot_be_promoted():
    """Promotion of a model with no weights is refused, not quietly allowed.

    ``promote`` used to accept any registered model. Promoting an untrained one
    sent production traffic to a model that answers MODEL_REQUIRED every time,
    which presents as a healthy deployment that happens to be silent - the
    monitoring meant to catch a broken model ends up pointed at the model that
    has none.
    """
    from infraimpact.models.base import ModelDeploymentMode
    from infraimpact.models.portfolio import (
        BaselineEventClassifier,
        build_default_model_registry,
    )

    registry = build_default_model_registry()
    untrained_id = next(iter(registry.list_models()))["model_id"]

    assert registry.promote(untrained_id, ModelDeploymentMode.CHAMPION) is False
    assert registry.promote("no-such-model", ModelDeploymentMode.CHAMPION) is False

    # The champion is unchanged after the refusal.
    task = registry.get(untrained_id).task_type
    assert registry.get_champion(task).model_id == untrained_id

    # A model with real weights behind it still promotes.
    registry.register(BaselineEventClassifier())
    assert registry.promote(untrained_id, ModelDeploymentMode.CHALLENGER) is True
    assert registry.get(untrained_id).deployment_mode == ModelDeploymentMode.CHALLENGER

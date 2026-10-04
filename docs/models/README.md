# Predictive Machine Learning Model Portfolio Specifications

This directory contains the definitive engineering specifications for the specialized predictive machine learning models required by the **Dynamic Infrastructure Impact Intelligence Platform** as specified in `Design_Infrastructure_Platform.md`.

---

## 1. Architectural Principles & Boundaries

In accordance with `Design_Infrastructure_Platform.md` (§§20, 23–27, 44–48):

1. **No Fake Heuristics or Fabricated Predictions:**
   Untrained model slots explicitly report `status = "MODEL_REQUIRED"`, `is_placeholder = True`, `confidence = 0.0`, and output zero fabricated forecasts. The platform never pretends a heuristic is a trained statistical model.
2. **Clear Separation of Truth Levels:**
   * `CONFIRMED`: Directly observed and verified ground-truth data (e.g., official WSDOT sensor closures, GTFS-RT cancellation feeds).
   * `INFERRED`: Rule-based spatial intersections and deterministic network graph traversal.
   * `PREDICTED`: Quantitative forecasts produced strictly by trained, calibrated machine learning models.
3. **Point-in-Time Correctness (§46):**
   Training datasets are constructed from the immutable observation ledger using point-in-time state reconstruction ($t$) and forward-looking outcome evaluation ($t \to t + H$) to eliminate lookahead bias and target leakage.
4. **Champion / Challenger / Shadow Lifecycle (§47):**
   * `CHAMPION`: Active production model driving live forecasts.
   * `CHALLENGER`: Candidate model evaluated against champion on live and offline benchmarks.
   * `SHADOW`: Evaluation model receiving production inference context side-by-side without contaminating user presentation.
5. **Calibrated Probabilities (§45):**
   Every classification or hazard model must output calibrated probabilities with Expected Calibration Error (ECE) $\le 0.08$ and valid Brier score tracking.

---

## 2. Model Portfolio Catalog

| Identifier | Model Name | Primary Task | Target Horizons | Core Methodology | Specification Document |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Model A** | **Event Classification** | Multiclass event distribution over incoming signals | Real-time | Calibrated Multi-class GBDT / Softmax | [MODEL_A_EVENT_CLASSIFICATION.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_A_EVENT_CLASSIFICATION.md) |
| **Model B** | **Infrastructure Impact** | Multivariate impact probabilities ($P(\text{road}), P(\text{transit}), P(\text{utility}), P(\text{facility}), P(\text{delay})$) | 5, 15, 30, 60 min | Multi-target LightGBM / XGBoost | [MODEL_B_INFRASTRUCTURE_IMPACT.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_B_INFRASTRUCTURE_IMPACT.md) |
| **Model C** | **Time-to-Impact** | Temporal survival hazard and onset probability | $T+5, T+15, T+30, T+60$ min | Cox Proportional Hazards / Survival Forests | [MODEL_C_TIME_TO_IMPACT.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_C_TIME_TO_IMPACT.md) |
| **Model D** | **Traffic Prediction** | Counterfactual baseline speeds and non-causal congestion anomalies | 15, 30, 60 min | Spatiotemporal Residual GBDT | [MODEL_D_TRAFFIC_PREDICTION.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_D_TRAFFIC_PREDICTION.md) |
| **Model E** | **Transit Disruption** | GTFS-RT delay propagation, cancellation probability, stop accessibility loss | 15, 30, 60 min | Gradient Boosting / Quantile Regression | [MODEL_E_TRANSIT_DISRUPTION.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_E_TRANSIT_DISRUPTION.md) |
| **Model F** | **Spatial Propagation** | Disruption cascade spread across road/transit network topology | 15, 30, 60 min | Spatiotemporal Graph Neural Network (ST-GNN) | [MODEL_F_SPATIAL_PROPAGATION.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_F_SPATIAL_PROPAGATION.md) |
| **Model G** | **User Priority Ranking** | Presentation item personalization score based on exposure and user outcomes | Real-time | Learning-to-Rank (LambdaMART / Pairwise) | [MODEL_G_USER_PRIORITY_RANKING.md](file:///Users/udaykamboj/multi-model-event-intelligence-system/docs/models/MODEL_G_USER_PRIORITY_RANKING.md) |

---

## 3. How to Plug in a Trained Model

Every model in the portfolio implements the base class `PredictiveModel` located at `infraimpact.models.base`.

### Step 1: Subclass `PredictiveModel` and Implement `predict()`

```python
from infraimpact.models.base import (
    ModelContext,
    ModelDeploymentMode,
    ModelMetadata,
    ModelPrediction,
    ModelTaskType,
    PredictiveModel,
)
from infraimpact.domain.enums import InfrastructureDomain, TruthStatus
from infraimpact.domain.schemas import Forecast

class TrainedInfrastructureImpactModel(PredictiveModel):
    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHALLENGER):
        metadata = ModelMetadata(
            model_id="infra-impact-lgbm-v2",
            version="2.0.0",
            task_type=ModelTaskType.INFRASTRUCTURE_IMPACT,
            deployment_mode=mode,
            description="Trained LightGBM model for multi-target infrastructure disruption.",
            feature_schema=["crowd_estimate", "arterial_overlap_count", "transit_route_overlap"],
            calibration_model="platt-scaling-v2",
        )
        super().__init__(metadata)
        self.is_trained = True
        # Load your PyTorch / ONNX / LightGBM weights artifact here
        # self.booster = lgb.Booster(model_file=weights_path)

    def predict(self, context: ModelContext) -> ModelPrediction:
        # 1. Extract point-in-time features from context
        features = [context.get_feature(f, 0.0) for f in self.metadata.feature_schema]
        
        # 2. Run model forward pass
        # raw_preds = self.booster.predict([features])[0]
        p_road = 0.72
        
        # 3. Construct Section 44 Forecast contracts
        forecasts = [
            Forecast(
                target="road_disruption",
                domain=InfrastructureDomain.ROAD,
                horizon_minutes=30,
                probability=p_road,
                lower=max(0.0, p_road - 0.1),
                upper=min(1.0, p_road + 0.1),
                model_id=self.model_id,
                model_version=self.version,
                truth_status=TruthStatus.PREDICTED,
            )
        ]
        
        return ModelPrediction(
            model_id=self.model_id,
            model_version=self.version,
            task_type=self.task_type,
            deployment_mode=self.deployment_mode,
            outputs={"p_road": p_road},
            forecasts=forecasts,
            confidence=p_road,
            uncertainty=round(4.0 * p_road * (1.0 - p_road), 4),
        )
```

### Step 2: Register in `ModelRegistry`

```python
from infraimpact.models.registry import ModelRegistry
from infraimpact.models.base import ModelDeploymentMode

registry = ModelRegistry()
trained_model = TrainedInfrastructureImpactModel("artifacts/model_b_v2.lgb", mode=ModelDeploymentMode.CHALLENGER)
registry.register(trained_model)
```

### Step 3: Evaluate and Promote via `ContinuousLearningPipeline`

```python
from infraimpact.evaluation.learning import ContinuousLearningPipeline

pipeline = ContinuousLearningPipeline(repo, registry)
result = pipeline.evaluate_promotion(
    candidate_model=trained_model,
    champion_model=registry.get_champion(ModelTaskType.INFRASTRUCTURE_IMPACT),
    min_brier_improvement=0.03,
)

if result.ready_for_promotion:
    registry.promote(trained_model.model_id, ModelDeploymentMode.CHAMPION)
```

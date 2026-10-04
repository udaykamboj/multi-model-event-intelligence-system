# Model Specification: Model A — Event Classification

**System Role:** Event Classification & Categorization  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§23, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.EVENT_CLASSIFICATION`  

---

## 1. What the Model Predicts
Model A predicts the **multiclass probability distribution** over candidate event types for an incoming cluster of raw observations and LLM-extracted claims:
$$P(\text{Event Type} = c \mid \mathcal{X}_t) \quad \text{for } c \in \mathcal{C}$$
where candidate classes $\mathcal{C}$ include:
* `demonstration_planned` (permitted gatherings, rallies)
* `demonstration_unplanned` (spontaneous public assemblies, marches)
* `transportation_incident` (arterial collision, stall, drawbridge failure)
* `official_emergency` (fire 2-alarm+, hazardous material, structural compromise)
* `severe_weather` (winter storm, atmospheric river, localized flooding)
* `earthquake` (seismic shake report, structural risk)
* `infrastructure_failure` (power outage, water main break, signal malfunction)
* `unknown` (unresolved early signal)

Outputs are mutually exclusive and sum to 1.0, paired with a designated dominant class and overall classification confidence.

---

## 2. Why the System Needs It
Raw observations arrive from heterogeneous sources (CAD 911 calls, SDOT street use notices, police blotters, news headlines, NWS weather alerts). 
A single incident might begin as a "traffic hazard" CAD dispatch before evolving into an unpermitted march or severe collision.
The platform requires a probabilistic classifier to:
1. Distinguish physical event archetypes early in their lifecycle without brittle regex keyword matching.
2. Select appropriate downstream analytical capabilities (e.g., triggering crowd dynamics for demonstrations vs. structural propagation for earthquakes).
3. Provide model-driven prior probabilities for infrastructure consequence estimation.

---

## 3. Required Inputs and Features
The input vector $\mathcal{X}_t$ consists of tabular, temporal, and spatial signals computed at point-in-time $t$:

| Feature Key | Type | Description | Source Feeds |
| :--- | :--- | :--- | :--- |
| `observation_types` | `list[str]` | Set of observed signal categories in cluster | Multi-source |
| `source_count` | `int` | Total number of distinct observations linked to event | Ingestion Ledger |
| `source_authority_max` | `float` | Highest authority score among reporting sources (0.0 to 1.0) | `Provenance` |
| `has_permit` | `bool` | True if a matching special-event permit exists | Special Events API |
| `police_active` | `bool` | True if SPD CAD / blotter reports active units dispatched | SPD CAD |
| `fire_hazmat_active` | `bool` | True if SFD 911 reports engine/ladder/hazmat units | SFD Realtime 911 |
| `road_closure_active` | `bool` | True if SDOT/WSDOT confirms an active roadway closure | SDOT/WSDOT |
| `estimated_crowd_log` | `float` | $\log(1 + \text{crowd})$ from news/blotter claims | Claims Ledger |
| `is_moving` | `float` | Binary flag (0.0/1.0) indicating detected movement vector | World State |
| `time_of_day_sin` / `cos` | `float` | Cyclical time of day encoding | Temporal Context |
| `day_of_week` | `int` | Day of week (0 = Monday, 6 = Sunday) | Temporal Context |
| `spatial_density_score` | `float` | Density of critical facilities within 500m radius | GIS Baseline |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `ModelOutput` (§44):
```json
{
  "model_id": "event-classifier-v1",
  "model_version": "1.0.0",
  "task_type": "event_classification",
  "deployment_mode": "champion",
  "confidence": 0.88,
  "uncertainty": 0.12,
  "outputs": {
    "dominant_class": "demonstration_unplanned",
    "distribution": {
      "demonstration_planned": 0.05,
      "demonstration_unplanned": 0.88,
      "transportation_incident": 0.04,
      "official_emergency": 0.02,
      "unknown": 0.01
    }
  },
  "forecasts": []
}
```

---

## 5. Prediction Horizons
* Real-time ($T+0$): Continuous evaluation upon arrival of new observations.
* Latency target: $\le 20 \text{ ms}$ per inference pass.

---

## 6. Training Data Requirements
* **Data Sources:** 
  1. Historical ACLED Washington event records (2018–2024).
  2. Historical Seattle SPD Significant Incident and Call Data.
  3. Historical platform observation ledger snapshots built using `PointInTimeDatasetBuilder`.
* **Sample Size:** Minimum 5,000 labeled event clusters.
* **Leakage Prevention:** Features must be reconstructed strictly from observations recorded with `observed_at <= T`. Future updates must never be included in feature generation.

---

## 7. Target / Ground Truth Definition
* **Ground Truth Source:** Post-event official confirmation records (e.g., validated emergency dispatch logs, verified ACLED event typing, city after-action reports).
* **Target Label:** Categorical integer index $y \in [0, |\mathcal{C}| - 1]$.

---

## 8. Evaluation Metrics
* **Multi-class Log-Loss / Cross-Entropy:** Penalizes overconfident misclassifications.
* **Macro and Micro F1-Score:** Target Macro $F_1 \ge 0.85$.
* **Expected Calibration Error (ECE):** ECE $\le 0.05$ across 10 confidence bins.
* **Brier Score (Multi-class):** Target Multi-class Brier $\le 0.15$.

---

## 9. Uncertainty & Calibration Requirements
* The model must not output uncalibrated raw logits.
* Post-hoc calibration using **Temperature Scaling** or **Dirichlet Calibration** is mandatory.
* Epistemic uncertainty is quantified via normalized entropy:
$$H(p) = -\frac{1}{\log |\mathcal{C}|} \sum_{c \in \mathcal{C}} p_c \log p_c$$

---

## 10. Where It Connects to the System
* **Capability:** `EventClassificationCapability` in `src/infraimpact/analysis/prediction.py`.
* **Orchestration:** Invoked during the initial phase of `AnalysisOrchestrator.analyze()`.
* **State Impact:** Populates `state.event_type` and `features["event_class_distribution"]`.
* **Relevance Engine:** Used by `RelevanceEngine` to determine which domain capabilities (transit, spatial, routing) are scheduled for downstream evaluation.

---

## 11. Interface the Eventual Model Must Implement

```python
from infraimpact.models.base import (
    ModelContext,
    ModelDeploymentMode,
    ModelMetadata,
    ModelPrediction,
    ModelTaskType,
    PredictiveModel,
)

class EventClassificationModel(PredictiveModel):
    """Production implementation of Model A."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="event-classifier-gbdt-v1",
                version="1.0.0",
                task_type=ModelTaskType.EVENT_CLASSIFICATION,
                deployment_mode=mode,
                description="Trained LightGBM multiclass event classifier.",
                feature_schema=[
                    "observation_types",
                    "source_count",
                    "source_authority_max",
                    "has_permit",
                    "police_active",
                    "fire_hazmat_active",
                    "road_closure_active",
                    "estimated_crowd_log",
                    "is_moving",
                    "spatial_density_score",
                ],
                calibration_model="temperature-scaling-v1",
            )
        )
        self.is_trained = True
        # Load weights from weights_path

    def predict(self, context: ModelContext) -> ModelPrediction:
        # 1. Extract features from context
        # 2. Forward pass through classifier
        # 3. Apply temperature scaling
        # 4. Return ModelPrediction
        ...
```

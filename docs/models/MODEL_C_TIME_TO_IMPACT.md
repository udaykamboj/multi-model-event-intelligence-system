# Model Specification: Model C — Time-to-Impact Model

**System Role:** Temporal Survival Analysis & Hazard Forecasting  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§24, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.TIME_TO_IMPACT`  

---

## 1. What the Model Predicts
Model C predicts the **survival hazard and probability of infrastructure disruption onset** as a continuous function of lead time $t$:
$$S(t) = P(T_{\text{impact}} > t \mid \mathcal{X}_0) \implies F(t) = 1 - S(t)$$
Specifically, it provides discrete hazard probabilities at standard operational decision intervals:
$$F(5), \quad F(15), \quad F(30), \quad F(60) \quad \text{(minutes)}$$
along with the estimated **median time-to-impact** $T_{\text{median}}$ where $F(T_{\text{median}}) = 0.50$.

---

## 2. Why the System Needs It
Traditional binary models answer *"Will transit be disrupted?"* but cannot answer *"When will it happen?"*
For an actionable user notification, lead time is the single most valuable dimension:
* Disruption at $T+4 \text{ min}$: Driver must divert immediately.
* Disruption at $T+45 \text{ min}$: Commuter has time to finish their meeting and take their planned bus.
Because observations undergo right-censoring (an event may disperse before disrupting infrastructure), standard regression models yield biased estimates. Survival analysis correctly handles censored trajectories.

---

## 3. Required Inputs and Features
The covariate vector $\mathbf{z}$ incorporates event dynamics, proximity, and network elasticity:

| Feature Key | Type | Description | Source |
| :--- | :--- | :--- | :--- |
| `distance_to_nearest_arterial_m` | `float` | Euclidean distance from event boundary to major arterial | Geospatial Engine |
| `distance_to_transit_hub_m` | `float` | Distance to nearest light-rail station or major transit center | GIS Facilities |
| `event_speed_mps` | `float` | Estimated rate of advance of movement vector (meters/second) | World State |
| `crowd_acceleration` | `float` | Second derivative of crowd estimate over last 3 updates | State History |
| `network_edge_density` | `float` | Number of street intersections per square kilometer | Road Graph |
| `police_containment_level` | `float` | Score (0.0 to 1.0) derived from police blotter dispatch | Claims Ledger |
| `p_road_initial` | `float` | Base road impact probability from Model B | Model B Output |
| `p_transit_initial` | `float` | Base transit impact probability from Model B | Model B Output |
| `traffic_level_baseline` | `float` | Pre-existing background congestion index (0 to 1) | Historical Baseline |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `Forecast` schema (§44):
```json
{
  "model_id": "time-to-impact-cox-v1",
  "model_version": "1.0.0",
  "task_type": "time_to_impact",
  "deployment_mode": "champion",
  "confidence": 0.86,
  "uncertainty": 0.14,
  "outputs": {
    "median_time_road_min": 14.2,
    "median_time_transit_min": 26.5,
    "hazard_rate_per_min": 0.048
  },
  "forecasts": [
    {
      "target": "road_impact_present",
      "domain": "road",
      "horizon_minutes": 5,
      "probability": 0.21,
      "lower": 0.12,
      "upper": 0.32,
      "model_id": "time-to-impact-cox-v1",
      "truth_status": "PREDICTED"
    },
    {
      "target": "road_impact_present",
      "domain": "road",
      "horizon_minutes": 15,
      "probability": 0.52,
      "lower": 0.40,
      "upper": 0.64,
      "model_id": "time-to-impact-cox-v1",
      "truth_status": "PREDICTED"
    },
    {
      "target": "road_impact_present",
      "domain": "road",
      "horizon_minutes": 30,
      "probability": 0.76,
      "lower": 0.65,
      "upper": 0.85,
      "model_id": "time-to-impact-cox-v1",
      "truth_status": "PREDICTED"
    }
  ]
}
```

---

## 5. Prediction Horizons
* Temporal horizons: 5, 15, 30, and 60 minutes.
* Real-time survival curve generation target: $\le 25 \text{ ms}$.

---

## 6. Training Data Requirements
* **Methodology:** Right-censored survival data format:
  $$\mathcal{D} = \{(t_i, \delta_i, \mathbf{z}_i)\}_{i=1}^N$$
  where $t_i = \min(T_{\text{event}}, T_{\text{end}})$, and event indicator $\delta_i = 1$ if disruption occurred, $\delta_i = 0$ if censored.
* **Corpus Sources:** Historical Seattle road closure timelines (SDOT), WSDOT incident logs, and Metro transit alert duration logs.
* **Minimum Records:** 8,000 recorded incident trajectories with millisecond-accurate timestamps.

---

## 7. Target / Ground Truth Definition
* **Event Time ($T_{\text{impact}}$):** The exact minute when an intersecting roadway is declared closed or when transit route delay exceeds 10 minutes.
* **Censoring Time:** When the demonstration or event disperses without producing infrastructure closure.

---

## 8. Evaluation Metrics
* **Harrell's Concordance Index (C-index):** Measures ranking consistency of survival times. Target $C\text{-index} \ge 0.82$.
* **Integrated Brier Score (IBS):** Evaluates calibrated probability across the continuous time interval $[0, 60\text{ min}]$. Target $\text{IBS} \le 0.10$.
* **Mean Absolute Error (MAE) on uncensored events:** Target $\text{MAE} \le 6.5 \text{ minutes}$.

---

## 9. Uncertainty & Calibration Requirements
* Must provide cumulative hazard confidence bounds derived from Greenwood's formula or parametric bootstrap.
* Predicted survival curves must be monotonically decreasing: $S(t_2) \le S(t_1)$ for $t_2 > t_1$.

---

## 10. Where It Connects to the System
* **Capability:** `TimeToImpactCapability` in `src/infraimpact/analysis/prediction.py`.
* **Urgency Scoring:** Feeds into `UserPriorityEngine._urgency_score()` to compute notification urgency ($T_{\text{median}} < 15\text{m} \implies \text{HIGH/IMMEDIATE}$).
* **User Presentation:** Surfaces as *"Expected impact within ~14 minutes"* on presentation items.

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
from infraimpact.domain.enums import InfrastructureDomain, TruthStatus
from infraimpact.domain.schemas import Forecast

class TimeToImpactModel(PredictiveModel):
    """Production implementation of Model C (Survival Analysis)."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="time-to-impact-cox-v1",
                version="1.0.0",
                task_type=ModelTaskType.TIME_TO_IMPACT,
                deployment_mode=mode,
                description="Trained Cox Proportional Hazards time-to-impact model.",
                feature_schema=[
                    "distance_to_nearest_arterial_m",
                    "distance_to_transit_hub_m",
                    "event_speed_mps",
                    "crowd_acceleration",
                    "network_edge_density",
                    "police_containment_level",
                    "p_road_initial",
                    "p_transit_initial",
                ],
                calibration_model="survival-conformal-v1",
            )
        )
        self.is_trained = True
        # Load survival model baseline hazard and coefficients

    def predict(self, context: ModelContext) -> ModelPrediction:
        # 1. Compute individual hazard: h(t|z) = h_0(t) * exp(beta * z)
        # 2. Derive cumulative survival S(t) = exp(-H(t|z))
        # 3. Derive cumulative disruption F(t) = 1 - S(t) at 5, 15, 30, 60m
        # 4. Compute median onset time
        # 5. Return ModelPrediction with Forecast contracts
        ...
```

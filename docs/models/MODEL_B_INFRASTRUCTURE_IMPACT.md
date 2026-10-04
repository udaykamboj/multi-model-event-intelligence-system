# Model Specification: Model B — Infrastructure Impact

**System Role:** Multivariate Infrastructure Disruption Forecasting  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§23, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.INFRASTRUCTURE_IMPACT`  

---

## 1. What the Model Predicts
Model B predicts the **multivariate probability of disruption** across five infrastructure domains over forward-looking time horizons:
$$\mathbf{P}_{\text{impact}}(H) = \begin{bmatrix}
P(\text{road disruption} \mid H) \\
P(\text{transit disruption} \mid H) \\
P(\text{utility disruption} \mid H) \\
P(\text{public facility impact} \mid H) \\
P(\text{severe corridor delay} \mid H)
\end{bmatrix} \quad \text{for } H \in \{5, 15, 30, 60\} \text{ minutes}$$

Each prediction includes a point probability $p \in [0, 1]$ and a 90% calibrated confidence interval $[p_{\text{lower}}, p_{\text{upper}}]$.

---

## 2. Why the System Needs It
An evolving event does not simply exist in isolation; it impacts physical and municipal infrastructure networks. 
Rather than hardcoding arbitrary disruption assumptions (e.g. "if 500 people, block 2 roads"), the platform requires a calibrated statistical model to forecast:
1. Which specific infrastructure domains will experience material degradation.
2. The likelihood of secondary effects (e.g. road closure cascading into bus cancellations).
3. The lead time available to dispatch proactive user notifications before disruption occurs.

---

## 3. Required Inputs and Features
The model consumes point-in-time features from the observation ledger, current world state, and geospatial layers:

| Feature Key | Type | Description | Source Feeds |
| :--- | :--- | :--- | :--- |
| `crowd_estimate` | `float` | Estimated crowd size (fused from claims) | Claims Ledger |
| `moving` | `float` | Binary flag (1.0 = moving march, 0.0 = static gathering) | World State |
| `expansion_rate` | `float` | Rate of convex hull area expansion ($\text{m}^2/\text{hr}$) | Geospatial Engine |
| `arterial_overlap_count` | `int` | Number of major arterials intersecting event hull | GIS / Road Graph |
| `road_overlap_count` | `int` | Total roadway segments intersecting event hull | GIS / Road Graph |
| `transit_route_overlap` | `int` | Number of scheduled bus/light-rail lines in area | GTFS Static / RT |
| `transit_stop_count` | `int` | Number of transit stops within 250m buffer | GTFS Static |
| `duration_hours` | `float` | Elapsed time since first observation | World State |
| `rush_hour` | `float` | Binary flag indicating peak commute window | Temporal Context |
| `source_authority` | `float` | Mean discounted reliability of reporting sources | Dependency Graph |
| `propagation_reach` | `int` | Network graph BFS hop-count reach | Network Graph |
| `route_redundancy` | `float` | Local detour capacity score (0.0 = choke point, 1.0 = grid) | Graph Routing |
| `historical_similarity_score` | `float` | Nearest-neighbor similarity to past disruption profiles | Historical Engine |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `Forecast` schema (§44):
```json
{
  "model_id": "infra-impact-lgbm-v1",
  "model_version": "1.0.0",
  "task_type": "infrastructure_impact",
  "deployment_mode": "champion",
  "confidence": 0.84,
  "uncertainty": 0.16,
  "outputs": {
    "p_road": 0.84,
    "p_transit": 0.65,
    "p_utility": 0.04,
    "p_public_facility": 0.12,
    "p_route_delay": 0.89
  },
  "forecasts": [
    {
      "target": "road_disruption",
      "domain": "road",
      "horizon_minutes": 30,
      "probability": 0.84,
      "lower": 0.72,
      "upper": 0.94,
      "model_id": "infra-impact-lgbm-v1",
      "model_version": "1.0.0",
      "truth_status": "PREDICTED"
    },
    {
      "target": "transit_disruption",
      "domain": "transit",
      "horizon_minutes": 30,
      "probability": 0.65,
      "lower": 0.52,
      "upper": 0.78,
      "model_id": "infra-impact-lgbm-v1",
      "model_version": "1.0.0",
      "truth_status": "PREDICTED"
    }
  ]
}
```

---

## 5. Prediction Horizons
* Multi-horizon output: $H \in \{5, 15, 30, 60\}$ minutes forward.
* Latency target: $\le 30 \text{ ms}$ per evaluation.

---

## 6. Training Data Requirements
* **Data Sources:** 
  1. `PointInTimeDatasetBuilder` over historical observation ledger.
  2. WSDOT highway alert logs + SDOT street closure history.
  3. King County Metro GTFS-RT delay and cancellation archives.
* **Dataset Structure:** Leak-free point-in-time slices. Each record represents features $\mathcal{X}(T)$ and ground-truth outcomes evaluated strictly in interval $[T, T + H]$.
* **Volume:** Minimum 15,000 historical state transitions.

---

## 7. Target / Ground Truth Definition
* **Road Disruption Target:** An official SDOT or WSDOT closure or severe congestion incident verified on the network edge within $H$ minutes.
* **Transit Disruption Target:** A King County Metro service alert or GTFS-RT trip cancellation/delay $>10 \text{ min}$ on intersecting routes within $H$ minutes.
* **Utility Disruption Target:** Seattle City Light outage report affecting the target grid cell within $H$ minutes.

---

## 8. Evaluation Metrics
* **Brier Score:** Primary metric. Target Brier Score $\le 0.12$ across 15m and 30m horizons.
* **Expected Calibration Error (ECE):** Target ECE $\le 0.06$.
* **Area Under ROC Curve (AUC-ROC):** Target AUC $\ge 0.88$ for road and transit targets.
* **Precision / Recall / F1:** At decision threshold $\tau = 0.50$, target $F_1 \ge 0.80$.

---

## 9. Uncertainty & Calibration Requirements
* Outputs must be calibrated using **Platt Scaling** (logistic sigmoid on logits) or **Isotonic Regression** fitted on a dedicated temporal validation split.
* Prediction intervals $[p_{\text{lower}}, p_{\text{upper}}]$ must achieve 90% empirical coverage under non-parametric conformal calibration.

---

## 10. Where It Connects to the System
* **Capability:** `InfrastructureImpactPredictionCapability` in `src/infraimpact/analysis/prediction.py`.
* **State Integration:** Populates `AnalysisOutcome.forecasts` and updates `state.forecasts`.
* **Delta Engine:** Feeds into `StateDeltaEngine` to trigger material change detection when disruption probability increases by $>0.15$.
* **Personalization:** Feeds into `ExposureEngine` to weight personal commute risk.

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

class InfrastructureImpactModel(PredictiveModel):
    """Production implementation of Model B."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="infra-impact-lgbm-v1",
                version="1.0.0",
                task_type=ModelTaskType.INFRASTRUCTURE_IMPACT,
                deployment_mode=mode,
                description="Trained LightGBM multivariate infrastructure impact model.",
                feature_schema=[
                    "crowd_estimate",
                    "moving",
                    "expansion_rate",
                    "arterial_overlap_count",
                    "road_overlap_count",
                    "transit_route_overlap",
                    "transit_stop_count",
                    "duration_hours",
                    "rush_hour",
                    "source_authority",
                    "propagation_reach",
                    "route_redundancy",
                    "historical_similarity_score",
                ],
                calibration_model="platt-scaling-v1",
            )
        )
        self.is_trained = True
        # Load LightGBM booster / model weights from weights_path

    def predict(self, context: ModelContext) -> ModelPrediction:
        # Extract features from context.get_feature(...)
        # Predict multi-target probabilities
        # Return ModelPrediction with Forecast contracts
        ...
```

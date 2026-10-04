# Model Specification: Model D — Traffic Prediction & Anomaly Model

**System Role:** Counterfactual Baseline Traffic & Non-Causal Anomaly Estimation  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§25, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.TRAFFIC_PREDICTION`  

---

## 1. What the Model Predicts
Model D predicts two distinct transportation quantities for target roadway segments:
1. **Counterfactual Baseline Speed / Travel Time ($\hat{V}_{\text{baseline}}$):**
   The expected traffic speed under unperturbed normal conditions given temporal, seasonal, and environmental context:
   $$\hat{V}_{\text{baseline}} = f(\text{road segment}, \text{day of week}, \text{time of day}, \text{weather}, \text{season})$$
2. **Attributed Traffic Anomaly ($\Delta V$):**
   The quantitative deviation between real-time observed speed $V_{\text{obs}}$ and expected baseline:
   $$\text{Anomaly} = V_{\text{obs}} - \hat{V}_{\text{baseline}}$$
   Paired with a congestion probability $P(\text{congested}) \in [0, 1]$.

**Crucial Section 25 Boundary:** A detected traffic anomaly is *never* asserted to be causally caused by a demonstration without validated causal inference. The model reports measured deviation without speculative blame.

---

## 2. Why the System Needs It
Urban traffic in metropolitan regions like Seattle/Puget Sound experiences routine daily rush-hour congestion on corridors such as I-5 and the Aurora Bridge.
A platform that sounds an alarm every time I-5 slows down at 5:15 PM on a Tuesday trains users to ignore alerts.
The system needs Model D to:
1. Establish what traffic *should* look like right now in the absence of any event.
2. Isolate genuine statistical anomalies from expected recurring rush-hour slowdowns.
3. Compute expected delay minutes added to a user's specific travel itinerary.

---

## 3. Required Inputs and Features

| Feature Key | Type | Description | Source Feeds |
| :--- | :--- | :--- | :--- |
| `segment_id` | `str` | Roadway network edge identifier | Road Graph |
| `road_classification` | `str` | Interstate, Principal Arterial, Minor Arterial, Collector | WSDOT / SDOT GIS |
| `observed_speed_mph` | `float` | Real-time traffic speed from loop detectors or cameras | WSDOT Traffic Flow |
| `speed_limit_mph` | `float` | Posted legal speed limit | Road Graph |
| `time_of_day_minute` | `int` | Minute of day (0 to 1439) | Clock |
| `day_of_week` | `int` | Day of week (0 to 6) | Clock |
| `is_holiday` | `bool` | True if federal/state holiday | Calendar |
| `precipitation_intensity_mm` | `float` | Real-time rainfall rate | NWS Observations |
| `visibility_miles` | `float` | Atmospheric visibility | NWS Observations |
| `active_construction_workzone` | `bool` | True if scheduled construction is active on segment | WSDOT Work Zones |
| `bridge_opening_active` | `bool` | True if drawbridge opening is in progress | SDOT Drawbridges |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `ModelOutput` schema (§44):
```json
{
  "model_id": "traffic-anomaly-gbdt-v1",
  "model_version": "1.0.0",
  "task_type": "traffic_prediction",
  "deployment_mode": "champion",
  "confidence": 0.89,
  "uncertainty": 0.11,
  "outputs": {
    "segment_id": "road_edge_aurora_bridge_nb",
    "observed_speed_mph": 12.4,
    "expected_baseline_mph": 42.1,
    "speed_ratio": 0.29,
    "speed_anomaly_mph": -29.7,
    "congestion_severity": "severe",
    "is_anomalous": true,
    "additional_travel_time_sec": 480
  },
  "notes": [
    "Attribution to specific event not asserted without validated causal model (Section 25)"
  ]
}
```

---

## 5. Prediction Horizons
* Baseline forecasting: $H \in \{15, 30, 60\}$ minutes forward.
* Real-time anomaly computation target: $\le 15 \text{ ms}$ per roadway corridor.

---

## 6. Training Data Requirements
* **Data Sources:** 
  1. Multi-year historical WSDOT loop detector 90-second traffic sensor archives.
  2. Historical NWS weather observations (METAR logs at Sea-Tac and Boeing Field).
  3. City of Seattle official holiday and construction calendar history.
* **Temporal Window:** Minimum 12 consecutive months to capture seasonal variations, school calendar changes, and weather patterns.
* **Volume:** 250,000+ segment-hour records across Puget Sound highway corridors.

---

## 7. Target / Ground Truth Definition
* **Ground Truth:** Continuous physical sensor measurement $V_{\text{actual}}$ from WSDOT station detectors or verified probe telemetry.
* **Residual Calculation:** $e_i = V_{\text{actual}} - \hat{V}_{\text{baseline}}$.

---

## 8. Evaluation Metrics
* **Baseline Regression MAE / RMSE:** Target $\text{MAE} \le 3.5 \text{ mph}$ on unperturbed holdout test days.
* **Mean Absolute Percentage Error (MAPE):** Target $\text{MAPE} \le 7.0\%$.
* **Anomaly Detection F1:** Evaluated on known incident ground-truth closures vs non-incident rush hours: target $F_1 \ge 0.85$.

---

## 9. Uncertainty & Calibration Requirements
* The model must output prediction intervals $[V_{\text{low}}, V_{\text{high}}]$ using **Quantile Regression** ($q = 0.05$ and $q = 0.95$).
* Segments with missing or corrupted loop detector feeds must output `confidence < 0.30` and clearly identify missing sensor data.

---

## 10. Where It Connects to the System
* **Capability:** `TrafficPredictionCapability` in `src/infraimpact/analysis/prediction.py`.
* **Routing Engine:** Feeds dynamic edge penalty weights to `GraphRoutingEngine.route()` and `travel_time()`.
* **User Presentation:** Drives the UI card *"Expected additional travel: ~12 minutes"*.

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

class TrafficPredictionModel(PredictiveModel):
    """Production implementation of Model D."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="traffic-anomaly-gbdt-v1",
                version="1.0.0",
                task_type=ModelTaskType.TRAFFIC_PREDICTION,
                deployment_mode=mode,
                description="Trained LightGBM counterfactual traffic baseline and anomaly model.",
                feature_schema=[
                    "segment_id",
                    "road_classification",
                    "observed_speed_mph",
                    "speed_limit_mph",
                    "time_of_day_minute",
                    "day_of_week",
                    "is_holiday",
                    "precipitation_intensity_mm",
                    "visibility_miles",
                    "active_construction_workzone",
                ],
                calibration_model="conformal-quantile-v1",
            )
        )
        self.is_trained = True
        # Load weights artifact from weights_path

    def predict(self, context: ModelContext) -> ModelPrediction:
        # 1. Compute expected counterfactual baseline speed
        # 2. Compare against observed speed
        # 3. Calculate anomaly magnitude and congestion probability
        # 4. Return ModelPrediction with Section 25 causal restraint note
        ...
```

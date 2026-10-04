# Model Specification: Model E — Transit Disruption Model

**System Role:** GTFS-RT Transit Delay, Cancellation & Accessibility Forecasting  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§26, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.TRANSIT_DISRUPTION`  

---

## 1. What the Model Predicts
Model E predicts public transportation service degradation across three specific targets:
1. **Downstream Delay Minutes ($\hat{D}_{\text{transit}}$):**
   Expected operational delay in minutes for transit vehicles currently en route or scheduled to traverse the affected corridor.
2. **Service Cancellation Probability ($P(\text{cancellation}) \in [0, 1]$):**
   The likelihood that King County Metro or Sound Transit will issue a trip cancellation or emergency reroute.
3. **Stop Accessibility Loss Ratio ($\mathcal{A}_{\text{loss}} \in [0, 1]$):**
   Fraction of transit stops along the route rendered inaccessible to boarding passengers.

---

## 2. Why the System Needs It
Urban demonstrators frequently march along transit corridors (e.g. 3rd Avenue, Pine Street, Broadway).
A bus route operating 2 miles away can experience cascading gridlock if its corridor is blocked downtown.
The platform needs Model E to:
1. Track delay propagation down the transit schedule sequence before the bus physically arrives at the blockage.
2. Alert bus and light-rail commuters that their scheduled trip will be canceled or rerouted before they walk to the stop.
3. Supply verified alternative transit options (e.g., recommend Link Light Rail when surface bus routes 40 and 70 are obstructed).

---

## 3. Required Inputs and Features

| Feature Key | Type | Description | Source Feeds |
| :--- | :--- | :--- | :--- |
| `route_id` | `str` | GTFS route identifier | GTFS Static |
| `trip_id` | `str` | Active scheduled GTFS trip identifier | GTFS Static |
| `current_delay_sec` | `float` | Real-time delay reported by vehicle GPS | GTFS-RT TripUpdates |
| `vehicle_speed_mps` | `float` | Instantaneous vehicle velocity | GTFS-RT VehiclePositions |
| `stops_remaining` | `int` | Number of stops remaining before route termination | GTFS Static |
| `corridor_road_closure_active` | `bool` | True if road segment on bus route is officially closed | SDOT / WSDOT |
| `active_service_alert` | `bool` | True if an agency alert is published for this route | GTFS-RT Alerts |
| `headway_scheduled_sec` | `int` | Scheduled interval between buses | GTFS Static |
| `historical_cancellation_rate` | `float` | Baseline route cancellation frequency under congestion | Historical Archive |
| `event_intersection_ratio` | `float` | Percentage of route geometry intersecting active event polygon | Geospatial Engine |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `Forecast` schema (§44):
```json
{
  "model_id": "transit-disruption-lgbm-v1",
  "model_version": "1.0.0",
  "task_type": "transit_disruption",
  "deployment_mode": "champion",
  "confidence": 0.85,
  "uncertainty": 0.15,
  "outputs": {
    "route_id": "metro_route_40",
    "expected_delay_minutes": 18.5,
    "cancellation_probability": 0.62,
    "accessibility_reduction": 0.35,
    "alternate_transit_recommended": true,
    "suggested_alternatives": ["sound_transit_link_1_line"]
  },
  "forecasts": [
    {
      "target": "transit_service_cancellation",
      "domain": "transit",
      "horizon_minutes": 30,
      "probability": 0.62,
      "lower": 0.48,
      "upper": 0.76,
      "model_id": "transit-disruption-lgbm-v1",
      "truth_status": "PREDICTED"
    }
  ]
}
```

---

## 5. Prediction Horizons
* Forecast windows: 15, 30, and 60 minutes ahead of scheduled stop arrival.
* Inference throughput: $\le 20 \text{ ms}$ per route evaluation.

---

## 6. Training Data Requirements
* **Data Sources:** 
  1. King County Metro GTFS-RT archive (trip updates + vehicle positions).
  2. Sound Transit GTFS-RT archives.
  3. Historical platform observation ledger (road closures and demonstration locations).
* **Volume:** Minimum 20,000 recorded transit trips across Puget Sound transit lines.

---

## 7. Target / Ground Truth Definition
* **Cancellation Ground Truth:** GTFS-RT `TripUpdate.ScheduleRelationship == CANCELED` or official Metro Transit Alert declaring route suspended.
* **Delay Ground Truth:** Real-time delay measurement recorded when the vehicle reaches the downstream target stop.

---

## 8. Evaluation Metrics
* **Cancellation Brier Score:** Target $\le 0.11$.
* **Cancellation AUC-ROC:** Target $\ge 0.89$.
* **Delay MAE:** Target $\text{MAE} \le 3.0 \text{ minutes}$ for 30-minute delay forecasts.
* **Stop Accessibility Precision/Recall:** Target $F_1 \ge 0.84$.

---

## 9. Uncertainty & Calibration Requirements
* Cancellation probability must be calibrated using **Platt Scaling** on an independent temporal validation set.
* Delay predictions must include 10th and 90th percentile prediction intervals via **Quantile Regression**.

---

## 10. Where It Connects to the System
* **Capability:** `TransitDisruptionCapability` in `src/infraimpact/analysis/transit.py`.
* **Exposure Engine:** Updates `ExposureEngine` for transit commuters (e.g. `demo-commuter-ferry`).
* **Presentation Layer:** Triggers the UI card *"Metro Route 40 reporting severe delays (~18m); Link Light Rail recommended as alternative"*.

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

class TransitDisruptionModel(PredictiveModel):
    """Production implementation of Model E."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="transit-disruption-lgbm-v1",
                version="1.0.0",
                task_type=ModelTaskType.TRANSIT_DISRUPTION,
                deployment_mode=mode,
                description="Trained LightGBM transit delay and cancellation model.",
                feature_schema=[
                    "route_id",
                    "current_delay_sec",
                    "vehicle_speed_mps",
                    "corridor_road_closure_active",
                    "active_service_alert",
                    "headway_scheduled_sec",
                    "historical_cancellation_rate",
                    "event_intersection_ratio",
                ],
                calibration_model="platt-scaling-v1",
            )
        )
        self.is_trained = True
        # Load weights from weights_path

    def predict(self, context: ModelContext) -> ModelPrediction:
        # Predict delay, cancellation probability, and stop accessibility
        # Return ModelPrediction with Forecast contracts
        ...
```

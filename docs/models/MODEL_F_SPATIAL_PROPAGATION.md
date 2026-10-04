# Model Specification: Model F — Spatial Propagation Model

**System Role:** Network Topology Disruption Cascade & Spillover Forecasting  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§27, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.SPATIAL_PROPAGATION`  

---

## 1. What the Model Predicts
Model F predicts the **spatiotemporal propagation and spillover of disruption across infrastructure graph edges**:
$$P(\text{Edge } e_j \text{ disrupted at } t + H \mid \mathcal{G}, \mathbf{S}_t) \in [0, 1] \quad \text{for } e_j \in \mathcal{E}, \; H \in \{15, 30, 60\}\text{ min}$$
where $\mathcal{G} = (\mathcal{V}, \mathcal{E})$ is the directed infrastructure graph encompassing roadway segments, intersections, bridges, tunnels, and transit corridors.

**Section 27 Architectural Rule:** Disruption must *never* be modeled as a simple radial Euclidean circle. It propagates strictly through topological network connections.

---

## 2. Why the System Needs It
When a demonstration blocks 4th Avenue at Pine Street:
* The blockage does not merely affect that single 200-meter segment.
* Traffic diverts to 2nd Avenue and 6th Avenue, creating secondary bottleneck congestion.
* The I-5 off-ramp onto Seneca Street backs up onto the freeway mainline.
* Downstream bus routes that never cross 4th Avenue stall in the spillover gridlock.
The platform requires a Spatiotemporal Graph Neural Network (ST-GNN) or diffusion model to forecast the topological cascade before secondary corridors seize up.

---

## 3. Required Inputs and Features
Graph structure and dynamic node/edge feature matrices:

| Input Entity | Type | Description | Source |
| :--- | :--- | :--- | :--- |
| $\mathcal{G}$ Adjacency Matrix | `SparseTensor` | Weighted directed connectivity between road segments | Infrastructure Graph |
| Edge Capacity | `float` | Lane count $\times$ design vehicle capacity per hour | GIS Roadway Data |
| Initial Disruption State | `float` | Binary or continuous closure score on seed nodes ($t=0$) | SDOT / WSDOT |
| Edge Flow Redundancy | `float` | Local network betweenness centrality | Graph Topology |
| Distance to Bottleneck | `int` | Topological shortest-path hops to bridges or highway ramps | Road Graph |
| Event Direction Vector | `tuple[float, float]` | Unit vector $(\Delta x, \Delta y)$ of event movement | World State |
| Current Inflow Volume | `float` | Normalized vehicle inflow rate at boundary intersections | WSDOT Flow Feeds |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `Forecast` schema (§44):
```json
{
  "model_id": "spatial-propagation-gnn-v1",
  "model_version": "1.0.0",
  "task_type": "spatial_propagation",
  "deployment_mode": "champion",
  "confidence": 0.83,
  "uncertainty": 0.17,
  "outputs": {
    "seed_edges_count": 2,
    "propagated_edges_count": 11,
    "max_propagation_hops": 4,
    "top_affected_edges": [
      {"edge_id": "edge_2nd_ave_corridor", "p_disruption": 0.88},
      {"edge_id": "edge_i5_seneca_ramp", "p_disruption": 0.74},
      {"edge_id": "edge_6th_ave_arterial", "p_disruption": 0.65}
    ]
  },
  "forecasts": [
    {
      "target": "cascade_propagation_reach",
      "domain": "road",
      "horizon_minutes": 30,
      "probability": 0.88,
      "lower": 0.75,
      "upper": 0.95,
      "model_id": "spatial-propagation-gnn-v1",
      "truth_status": "PREDICTED"
    }
  ]
}
```

---

## 5. Prediction Horizons
* Propagation horizons: 15, 30, and 60 minutes after initial blockage.
* Graph forward-pass latency budget: $\le 45 \text{ ms}$ over the Puget Sound regional subgraph ($\sim 10,000$ active edges).

---

## 6. Training Data Requirements
* **Architecture:** Spatiotemporal Graph Convolutional Network (ST-GCN) or Diffusion Convolutional Recurrent Neural Network (DCRNN).
* **Data Sources:** 
  1. Multi-month historical time-series of sensor speed and volume across WSDOT / SDOT detector networks.
  2. Recorded incident timeline logs tracking the propagation of congestion queues from known incident origins.
* **Volume:** 5,000+ incident cascade sequences.

---

## 7. Target / Ground Truth Definition
* **Ground Truth:** Physical speed drop below $50\%$ of free-flow speed on edge $e_j$ at time $t + H$, verified by sensor detection or secondary road closure notices.

---

## 8. Evaluation Metrics
* **Edge-wise Mean Squared Error (MSE) / MAE:** On predicted edge speeds across the graph. Target $\text{MAE} \le 4.0 \text{ mph}$.
* **Cascade Reach Precision / Recall:** Target $F_1 \ge 0.81$ identifying which downstream edges enter congested state within 30 minutes.
* **Topological Hop Distance Accuracy:** Target $\le 1 \text{ hop}$ error on congestion front advance.

---

## 9. Uncertainty & Calibration Requirements
* Edge disruption probabilities must be calibrated with **Platt Scaling** or **Isotonic Regression**.
* The model must satisfy physical conservation constraints (flow cannot propagate across completely severed or disconnected topological components).

---

## 10. Where It Connects to the System
* **Capability:** `SpatialPropagationCapability` in `src/infraimpact/analysis/geospatial.py`.
* **Graph Routing:** Supplies dynamic penalty weights $w(e_j) = w_{\text{base}} / (1.0 - p_{\text{disrupt}})$ to `GraphRoutingEngine`.
* **Exposure Engine:** Expands user exposure risk if a user's planned itinerary passes within the forecasted propagation cone.

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

class SpatialPropagationModel(PredictiveModel):
    """Production implementation of Model F (Spatiotemporal GNN)."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="spatial-propagation-gnn-v1",
                version="1.0.0",
                task_type=ModelTaskType.SPATIAL_PROPAGATION,
                deployment_mode=mode,
                description="Trained Spatiotemporal Graph Neural Network for infrastructure cascade propagation.",
                feature_schema=[
                    "adjacency_matrix",
                    "seed_edge_disruptions",
                    "edge_capacities",
                    "movement_direction_vector",
                    "background_inflow_rates",
                ],
                calibration_model="graph-conformal-v1",
            )
        )
        self.is_trained = True
        # Load PyTorch Geometric / DGL weights

    def predict(self, context: ModelContext) -> ModelPrediction:
        # Run graph message-passing over network topology
        # Return ModelPrediction with edge disruption probabilities
        ...
```

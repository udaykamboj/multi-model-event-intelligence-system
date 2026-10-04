# Model Specification: Model G — Personalized Priority & Presentation Ranking Model

**System Role:** Learning-to-Rank Presentation Personalization  
**Architecture Reference:** `Design_Infrastructure_Platform.md` (§36, §37, §44)  
**Status:** Model Specification / Awaiting Trained Weights  
**Task Type:** `ModelTaskType.USER_PRIORITY_RANKING`  

---

## 1. What the Model Predicts
Model G predicts the **personalized priority ranking score** for candidate presentation items displayed to a specific user:
$$\text{Score}(u, i \mid \mathcal{S}_t) = f(\text{User } u, \text{Presentation Item } i, \text{Event State } \mathcal{S}_t) \in [0.0, 1.0]$$
The score establishes the strict descending order in which information cards, alternative route recommendations, and warnings are presented in the user interface.

**Non-Negotiable Safety Constraint (§36, §38):** Official emergency guidance (AlertSeattle evacuation orders, flash flood warnings) always bypasses learned ranking and is pinned to the top position with an absolute priority floor $\ge 0.90$.

---

## 2. Why the System Needs It
A full cycle of backend analysis evaluates 40+ variables and may generate dozens of technical observations (e.g. 8 road closures, 14 bus delays, 2 drawbridge statuses, seismic background reports).
Dumping 40 data points onto a user during their morning commute produces cognitive overload and alert fatigue.
The platform requires a learned ranking model to:
1. Dynamically select the **top 3 to 6 reasons** that actually matter to *this* user right now.
2. Balance urgency against novelty (suppressing redundant updates while elevating sudden route blockages).
3. Learn from implicit and explicit user interaction feedback (e.g., whether the user adopted the suggested alternate route).

---

## 3. Required Inputs and Features
Features are computed per (User, Presentation Item) pair:

| Feature Key | Type | Description | Source |
| :--- | :--- | :--- | :--- |
| `impact_magnitude` | `float` | Physical severity of event and infrastructure disruption | State Engine |
| `user_exposure` | `float` | Direct spatial overlap with user's saved route or active location | Exposure Engine |
| `urgency` | `float` | Time-to-impact urgency band (0.0 = none, 1.0 = immediate) | Model C / Delta |
| `change_magnitude` | `float` | Numerical magnitude of state delta ($\Delta S = S(t) - S(t-1)$) | Delta Engine |
| `evidence_confidence` | `float` | Corroborated source reliability score | Dependency Graph |
| `user_transport_mode` | `str` | Commute preference (drive, bus, light_rail, ferry, walk) | User Context |
| `item_type` | `str` | Type of card (route_disruption, alternative_route, transit_alert) | Presentation |
| `historical_engagement_rate` | `float` | User's click/action rate on this alert category | User Feedback Log |
| `is_route_completely_blocked` | `bool` | True if user's only primary path has no bypass | Routing Engine |

---

## 4. Expected Output Contract
Conforming to `ModelPrediction` and `PresentationItem` schema (§37, §44):
```json
{
  "model_id": "priority-ranker-lambdamart-v1",
  "model_version": "1.0.0",
  "task_type": "user_priority_ranking",
  "deployment_mode": "champion",
  "confidence": 0.92,
  "outputs": {
    "user_id": "demo-commuter-bridge",
    "ranked_items": [
      {
        "type": "route_disruption",
        "priority": 0.96,
        "headline": "Your usual route is affected",
        "evidence_ids": ["obs_sdot_closure_aurora"],
        "confidence": 0.92
      },
      {
        "type": "alternative_route",
        "priority": 0.88,
        "headline": "Take Route B (Fremont Bridge detour) - adds ~12 min",
        "evidence_ids": ["route_alt_fremont"],
        "confidence": 0.85
      },
      {
        "type": "transit_alert",
        "priority": 0.42,
        "headline": "Route 40 experiencing delays downtown",
        "confidence": 0.70
      }
    ]
  }
}
```

---

## 5. Prediction Horizons
* Real-time presentation scoring per active user request or notification dispatch.
* Latency budget: $\le 10 \text{ ms}$ per user session.

---

## 6. Training Data Requirements
* **Architecture:** Learning-to-Rank (LTR) framework (LambdaMART, CatBoost Ranking, or RankNet).
* **Data Sources:** Logged historical presentation instances paired with user engagement outcomes (view duration, detail expansion, alternative route adoption, dismissals).
* **Sample Size:** 50,000+ ranked presentation interaction sessions.

---

## 7. Target / Ground Truth Definition
* **Relevance Grade ($y \in \{0, 1, 2, 3, 4\}$):**
  * $4$: User took recommended alternative route or avoided disrupted zone.
  * $3$: User clicked "Why?" evidence panel to inspect corroboration.
  * $2$: User expanded alert details.
  * $1$: User viewed card without interaction.
  * $0$: User dismissed card or marked alert as irrelevant/noise.

---

## 8. Evaluation Metrics
* **Normalized Discounted Cumulative Gain (NDCG@3 and NDCG@5):** Primary metric. Target $\text{NDCG@3} \ge 0.86$.
* **Mean Reciprocal Rank (MRR):** Target $\text{MRR} \ge 0.90$.
* **Alert Precision@3:** Fraction of top-3 items rated relevant by user. Target $\ge 85\%$.

---

## 9. Uncertainty & Calibration Requirements
* Predicted item scores must maintain monotonicity with respect to user exposure:
$$\text{Exposure}(A) > \text{Exposure}(B) \implies \text{Score}(A) \ge \text{Score}(B) \quad \text{ceteris paribus}$$
* Score variance across retraining cycles must not cause rapid "flickering" of UI card positions.

---

## 10. Where It Connects to the System
* **Engine:** `UserPriorityEngine` in `src/infraimpact/users/priority.py`.
* **Presentation Contract:** Powers `PresentationEngine.build()` to construct ordered `PresentationItem` lists (§37).
* **Notification Policy:** Sets threshold gating in `NotificationEngine` for dispatching push alerts.

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

class UserPriorityRankingModel(PredictiveModel):
    """Production implementation of Model G (Learning-to-Rank)."""

    def __init__(self, weights_path: str, mode: ModelDeploymentMode = ModelDeploymentMode.CHAMPION):
        super().__init__(
            ModelMetadata(
                model_id="priority-ranker-lambdamart-v1",
                version="1.0.0",
                task_type=ModelTaskType.USER_PRIORITY_RANKING,
                deployment_mode=mode,
                description="Trained LambdaMART model for personalized presentation item ranking.",
                feature_schema=[
                    "impact_magnitude",
                    "user_exposure",
                    "urgency",
                    "change_magnitude",
                    "evidence_confidence",
                    "user_transport_mode",
                    "item_type",
                    "is_route_completely_blocked",
                ],
                calibration_model="monotonic-rank-v1",
            )
        )
        self.is_trained = True
        # Load ranking model weights

    def predict(self, context: ModelContext) -> ModelPrediction:
        # Score and rank candidate presentation items
        # Enforce Section 38 official guidance override floor >= 0.90
        # Return ModelPrediction with sorted ranked_items
        ...
```

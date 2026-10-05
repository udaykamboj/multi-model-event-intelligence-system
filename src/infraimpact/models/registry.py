"""Model registry (brief section 47).

Section 47 requirements:
  - Formal registry storing metadata: training data range, feature schema,
    hyperparameters, metrics, calibration model, code commit, artifact checksum,
    deployment date.
  - Production supports champion, challenger, and shadow models.
  - New models operate in shadow mode initially.
  - Shadow predictions are recorded into ModelOutput for retrospective
    evaluation, but do NOT affect active user exposure or alerts.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from .base import (
    ModelContext,
    ModelDeploymentMode,
    ModelMetadata,
    ModelPrediction,
    ModelTaskType,
    PredictiveModel,
)

log = logging.getLogger(__name__)


class ModelRegistry:
    """Manages the lifecycle, versions, and deployment modes of predictive ML models."""

    def __init__(self) -> None:
        self._models: dict[str, PredictiveModel] = {}
        # task_type -> model_id for the champion
        self._champions: dict[ModelTaskType, str] = {}

    def register(self, model: PredictiveModel) -> PredictiveModel:
        """Register a model instance in the registry.

        The first model registered for a task type becomes its champion, because
        something has to answer for that task. If it has no trained weights that
        is logged loudly rather than treated as normal: an untrained champion is
        a legitimate configuration for a fresh deployment, but it is a fact
        about the deployment worth surfacing, not a detail.
        """

        model_id = model.model_id
        task_type = model.task_type
        self._models[model_id] = model

        if model.deployment_mode == ModelDeploymentMode.CHAMPION or task_type not in self._champions:
            self._champions[task_type] = model_id
            if not model.is_trained:
                log.warning(
                    "model %s is the champion for %s but has no trained weights; "
                    "predictions for this task will report MODEL_REQUIRED until "
                    "it is trained. This is expected on a fresh deployment and "
                    "not expected to persist.",
                    model_id,
                    task_type.value,
                )

        log.info(
            "registered model: id=%s task=%s version=%s mode=%s trained=%s",
            model_id,
            task_type,
            model.version,
            model.deployment_mode,
            model.is_trained,
        )
        return model

    def get(self, model_id: str) -> PredictiveModel | None:
        return self._models.get(model_id)

    def get_champion(self, task_type: ModelTaskType) -> PredictiveModel | None:
        model_id = self._champions.get(task_type)
        if model_id and model_id in self._models:
            return self._models[model_id]
        # Fallback to any model of that task type
        for model in self._models.values():
            if model.task_type == task_type:
                return model
        return None

    def get_challengers(self, task_type: ModelTaskType) -> list[PredictiveModel]:
        return [
            m for m in self._models.values()
            if m.task_type == task_type and m.deployment_mode == ModelDeploymentMode.CHALLENGER
        ]

    def get_shadows(self, task_type: ModelTaskType) -> list[PredictiveModel]:
        return [
            m for m in self._models.values()
            if m.task_type == task_type and m.deployment_mode == ModelDeploymentMode.SHADOW
        ]

    def promote(self, model_id: str, new_mode: ModelDeploymentMode) -> bool:
        """Promote or switch a model's deployment mode (e.g. shadow -> challenger -> champion).

        Refuses to promote a model that has no trained weights, and says why.
        A model without weights returns ``MODEL_REQUIRED`` from every call, so
        promoting one does not improve anything - it only routes production
        traffic to a model that is guaranteed to answer nothing. It also makes
        the absence invisible: a champion that returns a placeholder looks
        exactly like a champion that is merely quiet, and the monitoring meant
        to catch a broken model is now pointed at the model that has none.

        ``is_trained`` is a caller-supplied flag, so this is a guard against
        accidental promotion rather than proof. The deeper protection is that no
        model in the portfolio can claim ``is_trained=True`` without an
        implemented inference behind it.
        """

        model = self._models.get(model_id)
        if not model:
            return False

        if not model.is_trained:
            log.warning(
                "refusing to promote %s to %s: no trained weights. It would return "
                "MODEL_REQUIRED for every prediction. Train and evaluate the model "
                "first, then promote.",
                model_id,
                new_mode.value,
            )
            return False

        # Create updated metadata
        updated_meta = ModelMetadata(
            model_id=model.metadata.model_id,
            version=model.metadata.version,
            task_type=model.metadata.task_type,
            deployment_mode=new_mode,
            description=model.metadata.description,
            feature_schema=model.metadata.feature_schema,
            hyperparameters=model.metadata.hyperparameters,
            metrics=model.metadata.metrics,
            training_data_range=model.metadata.training_data_range,
            calibration_model=model.metadata.calibration_model,
            code_commit=model.metadata.code_commit,
            artifact_checksum=model.metadata.artifact_checksum,
            deployed_at=model.metadata.deployed_at,
            specification_doc=model.metadata.specification_doc,
        )
        # Update metadata on model
        object.__setattr__(model, "metadata", updated_meta) if hasattr(model, "metadata") else None

        if new_mode == ModelDeploymentMode.CHAMPION:
            # Demote existing champion to challenger
            old_champ_id = self._champions.get(model.task_type)
            if old_champ_id and old_champ_id != model_id and old_champ_id in self._models:
                old_model = self._models[old_champ_id]
                demoted_meta = ModelMetadata(
                    model_id=old_model.metadata.model_id,
                    version=old_model.metadata.version,
                    task_type=old_model.metadata.task_type,
                    deployment_mode=ModelDeploymentMode.CHALLENGER,
                    description=old_model.metadata.description,
                    feature_schema=old_model.metadata.feature_schema,
                    hyperparameters=old_model.metadata.hyperparameters,
                    metrics=old_model.metadata.metrics,
                    training_data_range=old_model.metadata.training_data_range,
                    calibration_model=old_model.metadata.calibration_model,
                    code_commit=old_model.metadata.code_commit,
                    artifact_checksum=old_model.metadata.artifact_checksum,
                    deployed_at=old_model.metadata.deployed_at,
                    specification_doc=old_model.metadata.specification_doc,
                )
                object.__setattr__(old_model, "metadata", demoted_meta)
            self._champions[model.task_type] = model_id

        log.info("promoted model %s to %s", model_id, new_mode)
        return True

    def run_inference(
        self, task_type: ModelTaskType, context: ModelContext
    ) -> tuple[ModelPrediction | None, list[ModelPrediction]]:
        """Run champion model and all shadow/challenger models.

        Returns (champion_prediction, shadow_and_challenger_predictions).
        """
        champ = self.get_champion(task_type)
        champ_pred = champ.predict(context) if champ else None

        side_preds: list[ModelPrediction] = []
        for side_model in [*self.get_challengers(task_type), *self.get_shadows(task_type)]:
            if side_model.model_id == (champ.model_id if champ else None):
                continue
            try:
                pred = side_model.predict(context)
                side_preds.append(pred)
            except Exception as exc:  # noqa: BLE001
                log.warning("side inference failed for %s: %r", side_model.model_id, exc)

        return champ_pred, side_preds

    def list_models(self) -> list[dict[str, Any]]:
        """Summary of all registered models."""
        out = []
        for m in self._models.values():
            meta = m.metadata
            is_champion = self._champions.get(meta.task_type) == meta.model_id
            out.append(
                {
                    "model_id": meta.model_id,
                    "version": meta.version,
                    "task_type": meta.task_type.value,
                    "deployment_mode": meta.deployment_mode.value,
                    "is_champion": is_champion,
                    "description": meta.description,
                    "feature_schema": list(meta.feature_schema),
                    "metrics": meta.metrics,
                    "calibration_model": meta.calibration_model,
                    "training_data_range": meta.training_data_range,
                    "deployed_at": meta.deployed_at.isoformat(),
                }
            )
        return sorted(out, key=lambda x: (x["task_type"], not x["is_champion"], x["model_id"]))

    def __len__(self) -> int:
        return len(self._models)


__all__ = ["ModelRegistry"]

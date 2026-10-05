"""Statistical analysis capability (brief sections 25, 32; design: "Statistical Analysis").

Change points, trends and rates of change are computed with established
libraries - ``ruptures`` (PELT change-point detection) and ``numpy`` - not
hand-written heuristics and never by an LLM.

Series are built only from real observation payloads. When there is not enough
data the capability says so (``statistics_status = insufficient_data``) instead
of inventing a result.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import ruptures as rpt

from ..domain.enums import CapabilityTier, ObservationType
from ..domain.schemas import FeatureValue
from .capabilities import AnalysisCapability, CapabilityContext, CapabilityResult

#: Minimum points before a change-point search is statistically meaningful.
MIN_POINTS = 8


def detect_change_points(series: list[float], penalty: float | None = None) -> list[int]:
    """Indices where the mean shifts (PELT, L2 cost). Empty if none/too short."""
    if len(series) < MIN_POINTS:
        return []
    signal = np.asarray(series, dtype=float).reshape(-1, 1)
    sigma = float(np.std(signal)) or 1e-9
    # BIC-style penalty scaled to the signal's own variance.
    pen = penalty if penalty is not None else 2.0 * np.log(len(series)) * sigma**2
    bkps = rpt.Pelt(model="l2", min_size=2, jump=1).fit(signal).predict(pen=pen)
    return [b for b in bkps if b < len(series)]


def linear_trend(times_s: list[float], values: list[float]) -> float | None:
    """Least-squares slope in value-units per hour, or None if undefined."""
    if len(values) < 3 or len(set(times_s)) < 2:
        return None
    slope_per_s = np.polyfit(np.asarray(times_s), np.asarray(values), 1)[0]
    return float(slope_per_s * 3600.0)


class StatisticalChangeCapability(AnalysisCapability):
    capability_id = "statistical_change_analysis"
    tier = CapabilityTier.CHEAP
    description = (
        "Change-point detection (ruptures/PELT), trend and observation-rate "
        "analysis over the event's observation history."
    )
    estimated_latency_ms = 10
    estimated_cost = 0.02
    model_version = "ruptures-pelt-l2"

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        result = CapabilityResult()
        obs = sorted(ctx.observations, key=lambda o: o.observed_at)

        # 1. Traffic speed ratio series (real sensor/probe values only).
        traffic = []
        for o in obs:
            if o.observation_type in {ObservationType.TRAFFIC_CONDITION, ObservationType.TRAFFIC_FLOW}:
                payload = getattr(o, "structured_payload", None) or {}
                val = payload.get("speed_ratio") or payload.get("speed_mph")
                if val is not None:
                    traffic.append((o.observed_at, float(val)))
        values = [v for _, v in traffic]
        cps = detect_change_points(values)
        result.features["traffic_change_points"] = FeatureValue(
            name="traffic_change_points",
            value=[traffic[i][0].isoformat() for i in cps],
            confidence=0.8 if cps else 0.0,
        )
        if cps:
            i = cps[0]
            result.features["traffic_regime_shift"] = FeatureValue(
                name="traffic_regime_shift",
                value={
                    "at": traffic[i][0].isoformat(),
                    "mean_before": round(float(np.mean(values[:i])), 4),
                    "mean_after": round(float(np.mean(values[i:])), 4),
                },
            )
            result.notes.append(
                f"traffic regime shift detected at {traffic[i][0].isoformat()} (PELT)"
            )

        t0 = traffic[0][0] if traffic else None
        slope = linear_trend(
            [(t - t0).total_seconds() for t, _ in traffic], values
        ) if t0 else None
        if slope is not None:
            result.features["traffic_trend_per_hour"] = FeatureValue(
                name="traffic_trend_per_hour", value=round(slope, 4), unit="speed_ratio/hour"
            )

        # 2. Observation arrival rate: is information accelerating?
        if len(obs) >= 3:
            span_h = max((obs[-1].observed_at - obs[0].observed_at).total_seconds() / 3600.0, 1e-6)
            result.features["observation_rate_per_hour"] = FeatureValue(
                name="observation_rate_per_hour", value=round(len(obs) / span_h, 3)
            )
            mid = obs[len(obs) // 2].observed_at
            first = [o for o in obs if o.observed_at < mid]
            second = [o for o in obs if o.observed_at >= mid]
            h1 = max((first[-1].observed_at - first[0].observed_at).total_seconds() / 3600.0, 1e-6) if len(first) > 1 else None
            h2 = max((second[-1].observed_at - second[0].observed_at).total_seconds() / 3600.0, 1e-6) if len(second) > 1 else None
            if h1 and h2:
                r1, r2 = len(first) / h1, len(second) / h2
                trend = "accelerating" if r2 > 1.25 * r1 else "decelerating" if r2 < 0.8 * r1 else "steady"
                result.features["observation_rate_trend"] = FeatureValue(
                    name="observation_rate_trend", value=trend
                )

        result.features["statistics_status"] = FeatureValue(
            name="statistics_status",
            value="computed" if len(values) >= MIN_POINTS else "insufficient_data",
        )
        return result


__all__ = ["StatisticalChangeCapability", "detect_change_points", "linear_trend"]

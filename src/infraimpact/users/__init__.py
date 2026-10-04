"""User impact layer (brief sections 34-38, 52-54).

The order below is the order of dependency, and it is also the order the
runtime calls them in:

    exposure  -> is this user geometrically affected?   (deterministic, geospatial)
    priority  -> how much does this user care right now? (section 36)
    presentation -> what may a renderer be told?         (sections 37, 38)
    notification -> should the user be interrupted?      (sections 52, 53)

Exposure answers first and is the only step allowed to say "you are
affected". Everything downstream may raise the urgency of an already-real
exposure, but none of them can create one that geometry does not support.
"""

from .exposure import (
    EXPOSURE_RADIUS_M,
    ROUTE_BLOCKED_M,
    ExposureContext,
    ExposureEngine,
    exposure_band,
)
from .notifications import (
    NotificationContext,
    NotificationEngine,
    NotificationOutcome,
    PolicyDecision,
)
from .presentation import (
    INTERRUPT_FLOOR,
    OFFICIAL_PRIORITY,
    PresentationContext,
    PresentationEngine,
    PresentationPayload,
    ScreenInstructionError,
)
from .priority import PriorityContext, UserPriorityEngine, rank_priorities

__all__ = [
    "EXPOSURE_RADIUS_M",
    "INTERRUPT_FLOOR",
    "OFFICIAL_PRIORITY",
    "ROUTE_BLOCKED_M",
    "ExposureContext",
    "ExposureEngine",
    "NotificationContext",
    "NotificationEngine",
    "NotificationOutcome",
    "PolicyDecision",
    "PresentationContext",
    "PresentationEngine",
    "PresentationPayload",
    "PriorityContext",
    "ScreenInstructionError",
    "UserPriorityEngine",
    "exposure_band",
    "rank_priorities",
]
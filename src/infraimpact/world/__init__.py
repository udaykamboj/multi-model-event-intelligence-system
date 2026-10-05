"""Cross-event world projection.

Where :mod:`infraimpact.events.world_state` answers "what is the state of *this*
event", this package answers "what is the state of the world right now" -
one read, one snapshot, every event.

Keeping the two separate is deliberate. Per-event reconstruction is pure,
deterministic and replayable: same observations in, same state version out, and
it stays that way no matter how many events exist. The world view is a
convenience read that fans out over events, so it must stay as thin as
possible, and every field in it has to be derivable from state versions that
already exist. If the projection had to invent information to answer the
question, the two views would be able to disagree, and the system's core claim -
that every answer is traceable to immutable evidence - would be false.
"""

from __future__ import annotations

from .projection import WorldProjection

__all__ = ["WorldProjection"]

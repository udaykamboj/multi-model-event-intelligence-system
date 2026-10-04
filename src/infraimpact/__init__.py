"""Dynamic Infrastructure Impact Intelligence Platform.

The backend is organised as five cooperating systems (brief section 72):

1. sensor network       -> ``infraimpact.sources``
2. world-state engine   -> ``infraimpact.events``
3. intelligence engine  -> ``infraimpact.analysis``
4. personalisation      -> ``infraimpact.users``
5. temporal change      -> ``infraimpact.delta``

Nothing in this package assumes an event is a protest, that roads matter, or
that evacuation is necessary. The data decides that.
"""

__version__ = "0.1.0"
"""HTTP surface for the platform (brief section 50/51).

``create_app`` is the factory; ``app`` is the module-level instance uvicorn
loads via ``uvicorn infraimpact.api.app:app``.
"""

from .app import app, create_app

__all__ = ["app", "create_app"]
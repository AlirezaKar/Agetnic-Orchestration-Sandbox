"""
backend.api — HTTP layer of the test harness.

``app``    FastAPI application factory (static UI + /api)
``routes`` REST endpoints (chat, documents, traces, health)
"""

from .app import create_app

__all__ = ["create_app"]

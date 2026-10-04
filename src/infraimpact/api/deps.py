"""Dependency accessors for the API layer.

The API reads the same repositories the runtime writes, so there is exactly one
source of truth. Everything is pulled off ``app.state`` rather than imported as
module globals, because the repository and bus are per-process resources.

The repo and bus are normally created by the lifespan handler, but they are
resolved lazily here too. That keeps the app usable under an ASGI transport
that skips lifespan (plain ``httpx.ASGITransport``, several test harnesses),
where ``app.state`` would otherwise be empty and every route would 500.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..bus.event_bus import EventBus, build_bus
from ..storage.repository import PlatformRepository


def get_repository(request: Request) -> PlatformRepository:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        from .app import build_repository

        repo = request.app.state.repo = build_repository(request.app.state.settings)
    return repo


def get_bus(request: Request) -> EventBus:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        bus = request.app.state.bus = build_bus()
    return bus


def get_runtime(request: Request):
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        from ..runtime import Runtime

        runtime = request.app.state.runtime = Runtime(
            get_repository(request), get_bus(request), request.app.state.settings
        )
    return runtime


def get_region_id(request: Request) -> str:
    return request.app.state.region_id


RepoDep = Annotated[PlatformRepository, Depends(get_repository)]
BusDep = Annotated[EventBus, Depends(get_bus)]
RuntimeDep = Annotated["object", Depends(get_runtime)]
RegionDep = Annotated[str, Depends(get_region_id)]


__all__ = [
    "BusDep",
    "RegionDep",
    "RepoDep",
    "RuntimeDep",
    "get_bus",
    "get_region_id",
    "get_repository",
    "get_runtime",
]
"""FastAPI application (brief section 50).

The API is a *read* surface over the same repositories the runtime writes. It
never runs analysis itself; ``/internal/*`` routes hand work to the runtime's
own pipeline and orchestrator so there is exactly one implementation of the
section 71 loop.

By default the API also runs the loop as a background task, so
``GET /v1/stream`` actually has traffic in a local dev process. Set
``INFRAIMPACT_API_RUN_LOOP=0`` to serve read-only against an already-running
worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..bus.event_bus import EventBus, build_bus
from ..config import Settings, get_region, get_settings
from ..domain.ids import utcnow
from ..domain.schemas import SCHEMA_VERSION
from ..storage.sqlite_driver import SqlitePlatformRepository
from .routes import events, internal, observations, regions, sources, stream, user, world
from ..ui.routes import router as ui_router

log = logging.getLogger(__name__)

DESCRIPTION = """
Consumer API for the Dynamic Infrastructure Impact Intelligence Platform.

* `/v1` - read-only consumer surface. Model internals are never exposed
  (section 51).
* `/v1/stream` - server-sent events, the section 50 real-time channel.
* `/internal` - operator surface: ingest, request analysis, replay, evaluate.
"""


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_repository(settings: Settings):
    """Storage factory. SQLite now, Postgres/PostGIS when configured."""
    if settings.is_sqlite:
        return SqlitePlatformRepository(settings.database_url)
    # From ``postgres_driver``, not ``postgres``. The latter holds only the DDL
    # and the repository *protocol*; importing the class from there raised
    # ImportError at the exact moment an operator pointed the platform at a
    # production database - the one configuration that had never been run.
    from ..storage.postgres_driver import PostgresPlatformRepository  # pragma: no cover

    return PostgresPlatformRepository(settings.database_url)  # pragma: no cover


def create_app(
    settings: Settings | None = None,
    repository: Any | None = None,
    bus: EventBus | None = None,
    *,
    run_loop: bool | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    region = get_region(settings.region_id)
    should_run = (
        _bool_env("INFRAIMPACT_API_RUN_LOOP", True) if run_loop is None else run_loop
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        repo = repository if repository is not None else build_repository(settings)
        the_bus = bus if bus is not None else build_bus()
        from ..runtime import Runtime

        runtime = Runtime(repo, the_bus, settings)

        app.state.repo = repo
        app.state.bus = the_bus
        app.state.runtime = runtime

        await runtime.start()
        app.state.started_at = utcnow()
        log.info(
            "api ready: region=%s driver=%s bus=%s loop=%s",
            region.region_id,
            settings.driver,
            type(the_bus).__name__,
            "on" if should_run else "off",
        )

        task: asyncio.Task[None] | None = None
        if should_run:
            task = asyncio.create_task(_loop_supervisor(runtime, app), name="runtime-loop")

        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await runtime.stop()
            with contextlib.suppress(Exception):
                await the_bus.close()
            with contextlib.suppress(Exception):
                repo.close()

    app = FastAPI(
        title="Infrastructure Impact Intelligence Platform",
        version=SCHEMA_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    # Set immediately, not just in the lifespan: state that does not depend on
    # startup should exist before the first request is served.
    app.state.settings = settings
    app.state.region = region
    app.state.region_id = region.region_id
    app.state.last_cycle = None
    app.state.started_at = None

    app.include_router(events.router)
    app.include_router(user.router)
    app.include_router(regions.router)
    app.include_router(stream.router)
    app.include_router(sources.router)
    app.include_router(observations.router)
    app.include_router(ui_router)
    app.include_router(world.router)
    app.include_router(internal.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request) -> JSONResponse:
        """Liveness plus a cheap read, so a dead DB shows up here not in a 500."""
        from .deps import get_repository

        healthy = True
        counts: dict[str, int] = {}
        try:
            repo = get_repository(request)
            counts["observations"] = repo.observations.count()
            counts["events"] = repo.events.count() if hasattr(repo.events, "count") else len(repo.events.all_events())
            counts["users"] = repo.users.count() if hasattr(repo.users, "count") else len(repo.users.all())
        except Exception as exc:  # noqa: BLE001 - health must not raise
            log.exception("healthz probe failed")
            healthy = False
            counts["error"] = repr(exc)
        runtime = getattr(app.state, "runtime", None)
        collector_active = (
            runtime.collector.is_running()
            if runtime and getattr(runtime, "collector", None)
            else False
        )
        loop_active = (
            not runtime._stopping.is_set()
            if runtime and hasattr(runtime, "_stopping")
            else False
        )
        cycle_count = getattr(runtime, "_cycle", 0) if runtime else 0

        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "schema_version": SCHEMA_VERSION,
                "region_id": app.state.region_id,
                "autonomous_runtime": {
                    "loop_active": loop_active,
                    "collector_active": collector_active,
                    "cycle_count": cycle_count,
                    "last_cycle": app.state.last_cycle,
                },
                "counts": counts,
            },
        )

    return app


async def _loop_supervisor(runtime: Any, app: FastAPI) -> None:
    """Run the section 71 loop, recording the last cycle for the status route."""
    log.info("autonomous runtime loop supervisor active")
    while not runtime._stopping.is_set():
        try:
            result = await runtime.cycle()
            app.state.last_cycle = result.summary()
            if result.persisted > 0 or result.events:
                log.info(
                    "autonomous cycle %d: new=%d dirty_events=%d analyses=%d notifications=%d",
                    result.cycle,
                    result.persisted,
                    len(result.events),
                    result.analyses,
                    result.notifications,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad cycle
            log.exception("runtime cycle failed")
            app.state.last_cycle = {"error": repr(exc), "at": utcnow().isoformat()}
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                runtime._stopping.wait(), timeout=runtime.settings.loop_interval_s
            )


def app_state(request: Request) -> Any:
    """Convenience accessor used by the ops routes."""
    return request.app.state


#: Conventional ASGI target for ``uvicorn infraimpact.api.app:app``.
#: Cheap to build: no database or bus is touched until the lifespan runs.
app = create_app()


__all__ = ["app", "app_state", "build_repository", "create_app"]
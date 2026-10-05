from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ...config import get_settings
from ...domain.schemas import SCHEMA_VERSION, SourceHealth
from ...sources.registry import SourceRegistry
from ..deps import RegionDep, RepoDep

router = APIRouter(prefix="/v1/sources", tags=["sources"])


class SourceList(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int
    sources: list[SourceHealth]


@router.get("", response_model=SourceList)
async def list_sources(request: Request, repo: RepoDep, region: RegionDep) -> SourceList:
    runtime = getattr(request.app.state, "runtime", None)
    live_adapters = runtime.registry.adapters if runtime and hasattr(runtime, "registry") else []
    persisted = {h.source_id: h for h in repo.source_health.all()}

    if live_adapters:
        sources_map: dict[str, SourceHealth] = {}
        for a in live_adapters:
            h = a.health()
            # If adapter has not run in this process yet or is unknown, blend with persisted health
            if (h.records_received == 0 or h.last_success is None) and a.source_id in persisted:
                h = persisted[a.source_id]
            sources_map[a.source_id] = h
        for sid, ph in persisted.items():
            if sid not in sources_map:
                sources_map[sid] = ph
        sources = list(sources_map.values())
    elif persisted:
        sources = list(persisted.values())
    else:
        settings = get_settings()
        reg = SourceRegistry(region, settings)
        sources = [a.health() for a in reg.adapters]

    return SourceList(count=len(sources), sources=sources)


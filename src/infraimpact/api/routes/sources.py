from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ...config import get_settings
from ...domain.schemas import SCHEMA_VERSION, SourceHealth
from ...sources.registry import SourceRegistry
from ..deps import RegionDep, RepoDep

from ..schemas import CoverageDomain, CoverageResponse

router = APIRouter(prefix="/v1/sources", tags=["sources"])


class SourceList(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int
    sources: list[SourceHealth]


def _infer_source_domain(source_id: str) -> str:
    sid = source_id.lower()
    if any(k in sid for k in ("sdot", "wsdot", "traffic", "bridge", "camera", "closure")):
        return "roads"
    if any(k in sid for k in ("transit", "metro", "bus", "ferry")):
        return "transit"
    if any(k in sid for k in ("spd", "sfd", "fire", "police", "cad", "blotter")):
        return "public_safety"
    if any(k in sid for k in ("weather", "nws")):
        return "weather"
    if any(k in sid for k in ("usgs", "quake", "seismic")):
        return "seismic"
    if any(k in sid for k in ("power", "light", "water", "utility", "outage")):
        return "utilities"
    if any(k in sid for k in ("king5", "komo", "news", "capitol_hill", "media")):
        return "media"
    return "other"


@router.get("/coverage", response_model=CoverageResponse)
async def get_source_coverage(request: Request, repo: RepoDep, region: RegionDep) -> CoverageResponse:
    """Stage 1 section 11 & 12: Coverage panel by domain."""
    src_list = await list_sources(request, repo, region)
    sources = src_list.sources

    from collections import defaultdict
    by_domain: dict[str, list[SourceHealth]] = defaultdict(list)
    for s in sources:
        domain = _infer_source_domain(s.source_id)
        by_domain[domain].append(s)

    domain_order = ["roads", "transit", "public_safety", "weather", "seismic", "utilities", "media", "other"]
    domains: list[CoverageDomain] = []

    for d in domain_order:
        items = by_domain.get(d, [])
        if not items:
            continue
        healthy_count = sum(1 for s in items if str(s.state).lower() in ("healthy", "active"))
        producing_count = sum(1 for s in items if s.records_received > 0)
        domains.append(
            CoverageDomain(
                domain=d,
                total_sources=len(items),
                healthy_sources=healthy_count,
                producing_data_sources=producing_count,
                sources=[s.model_dump(mode="json") for s in items],
            )
        )

    total_h = sum(1 for s in sources if str(s.state).lower() in ("healthy", "active"))
    return CoverageResponse(
        domains=domains,
        total_sources=len(sources),
        healthy_sources=total_h,
    )


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


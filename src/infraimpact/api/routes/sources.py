from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...config import get_settings
from ...domain.schemas import SCHEMA_VERSION, SourceHealth
from ...sources.registry import SourceRegistry
from ..deps import RegionDep

router = APIRouter(prefix="/v1/sources", tags=["sources"])


class SourceList(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int
    sources: list[SourceHealth]


@router.get("", response_model=SourceList)
async def list_sources(region: RegionDep) -> SourceList:
    settings = get_settings()
    reg = SourceRegistry(region, settings)
    sources = [a.health() for a in reg.adapters]
    return SourceList(count=len(sources), sources=sources)

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ...domain.schemas import SCHEMA_VERSION
from ..deps import RepoDep
from ..schemas import Observation

router = APIRouter(prefix="/v1/observations", tags=["observations"])


class ObsList(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int
    observations: list[Observation]


@router.get("", response_model=ObsList)
async def list_observations(repo: RepoDep, limit: int = Query(default=50, ge=1, le=500)) -> ObsList:
    obs = repo.observations.recent(limit)
    return ObsList(count=len(obs), observations=obs)

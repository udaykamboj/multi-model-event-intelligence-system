from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .app import get_template

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("/", response_class=HTMLResponse)
async def ui_home(request: Request) -> str:
    return get_template()

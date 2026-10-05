from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .app import get_template

router = APIRouter(tags=["ui"])


@router.api_route("/", methods=["GET", "HEAD"], response_class=RedirectResponse, include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@router.api_route("/ui", methods=["GET", "HEAD"], response_class=RedirectResponse, include_in_schema=False)
async def ui_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@router.api_route("/ui/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def ui_home(request: Request) -> str:
    return get_template()


"""
routes/admin_system.py — ``/admin/system``: host, models, queue, latency, DB, network, clients.

``GET /admin/system`` renders the page; ``GET /admin/system/snapshot`` is the HTMX partial
it polls every 5 s (``?format=json`` returns the raw snapshot). Admin-only, read-only:
no form, so neither response mints or rotates the CSRF cookie.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..auth import require_admin
from ..ops import monitoring
from ..web.templates_env import templates

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request) -> HTMLResponse:
    snap = await monitoring.snapshot(request.app)
    return templates.TemplateResponse("system.html.j2", {"request": request, "s": snap})


@router.get("/system/snapshot")
async def system_snapshot(request: Request, format: str = "html") -> Response:  # noqa: A002
    snap = await monitoring.snapshot(request.app)
    if format == "json":
        return JSONResponse(snap)
    return templates.TemplateResponse("_system_body.html.j2", {"request": request, "s": snap})

"""Routes for remote machine control + the homepage panel partial."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.models import User
from app.orchestrator.remote_machines import (
    configured_machines,
    find_machine,
    machine_status,
    shutdown_machine,
    wake_machine,
)

router = APIRouter(tags=["remote-machines"])
api_router = APIRouter(prefix="/api/machines", tags=["remote-machines"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _build_rows() -> list[dict]:
    rows = []
    for m in configured_machines():
        st = await machine_status(m)
        rows.append({
            "name": m.name,
            "display_name": m.display_name,
            "mac": m.mac,
            "ssh_host": m.ssh_host,
            "hardware_class": m.hardware_class,
            "online": st.get("online", False),
            "source": st.get("source", "unknown"),
        })
    return rows


# ── HTMX partial used by the homepage panel ────────────────────────


@router.get("/partials/remote-machines", response_class=HTMLResponse, response_model=None)
async def partial(
    request: Request, user: User = Depends(get_current_user)
) -> HTMLResponse:
    rows = await _build_rows()
    return templates.TemplateResponse(
        request, "_remote_machines.html",
        {"machines": rows, "user": user},
    )


# ── form-action endpoints (HTMX swaps the panel back in) ───────────


@router.post("/machines/{name}/wake", response_class=HTMLResponse, response_model=None)
async def wake_form(
    name: str, request: Request, user: User = Depends(get_current_user)
) -> HTMLResponse:
    m = find_machine(name)
    if not m:
        raise HTTPException(404, "machine not configured")
    await wake_machine(m)
    rows = await _build_rows()
    return templates.TemplateResponse(
        request, "_remote_machines.html", {"machines": rows, "user": user}
    )


@router.post("/machines/{name}/shutdown", response_class=HTMLResponse, response_model=None)
async def shutdown_form(
    name: str, request: Request, user: User = Depends(get_current_user)
) -> HTMLResponse:
    m = find_machine(name)
    if not m:
        raise HTTPException(404, "machine not configured")
    await shutdown_machine(m)
    rows = await _build_rows()
    return templates.TemplateResponse(
        request, "_remote_machines.html", {"machines": rows, "user": user}
    )


# ── JSON API for scripting / external tools ────────────────────────


@api_router.get("")
async def list_machines_json() -> dict:
    return {"machines": await _build_rows()}


@api_router.post("/{name}/wake")
async def wake_json(name: str) -> dict:
    m = find_machine(name)
    if not m:
        raise HTTPException(404, "machine not configured")
    return await wake_machine(m)


@api_router.post("/{name}/shutdown")
async def shutdown_json(name: str) -> dict:
    m = find_machine(name)
    if not m:
        raise HTTPException(404, "machine not configured")
    return await shutdown_machine(m)

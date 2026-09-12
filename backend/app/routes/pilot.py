"""Pilot module — a chat surface over the worker fleet.

Pilot is the Operator module, renamed and reduced to one decision: how much
thought a thread should get. Pools, model tags and the search-vs-tools
mechanism are configuration now, not chat UI — see orchestrator/pilot.py.

Routes:
  GET  /pilot                                  conversation list + new form
  POST /pilot/conversations                    create a conversation
  GET  /pilot/conversations/{id}               thread view
  POST /pilot/conversations/{id}/settings      mode / web / site for a thread
  POST /pilot/conversations/{id}/messages      post a turn (dispatches to a worker)
  POST /pilot/conversations/{id}/delete        delete a conversation
  GET  /pilot/settings                         map modes to pools + models
  POST /pilot/settings                         save that mapping
  GET  /pilot/pools/{pool}/models              JSON, for the settings page

The streaming half lives in routes/streams.py (SSE) + orchestrator/chat.py.
Old /operator/* URLs redirect at the bottom of this file.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_session
from app.models import Conversation, ConversationMessage, MessageRole, User
from app.orchestrator import pilot, web_search
from app.orchestrator.chat import dispatch_turn, web_enabled
from app.orchestrator.pools import KNOWN_POOLS
from app.orchestrator.registry import registry

router = APIRouter(tags=["pilot"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/pilot/pools/{pool}/models", response_model=None)
async def pool_models(
    pool: str,
    mode: str = pilot.DEFAULT_MODE,
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """The pool's models, annotated and ordered for a mode.

    Only the settings page needs this — the chat UI never names a model. It
    takes `mode` because "appropriate" differs: Thoughtful wants the biggest
    tool-capable model, Lightweight the fastest one.
    """
    if pool not in KNOWN_POOLS:
        raise HTTPException(400, f"unknown pool: {pool}")
    workers = await registry.by_pool(pool)
    installed = {m for w in workers for m in (w.installed_models or ())}
    coverage = {
        tag: sum(1 for w in workers if tag in (w.installed_models or ()))
        for tag in installed
    }
    options, recommended = pilot.rank_models(
        pilot.normalize_mode(mode), sorted(installed), coverage, len(workers)
    )
    return JSONResponse({
        "models": sorted(installed),
        "options": options,
        "recommended": recommended,
        "worker_count": len(workers),
    })


@router.get("/pilot", response_class=HTMLResponse, response_model=None)
async def pilot_home(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    conversations = list((await session.execute(
        select(Conversation).order_by(desc(Conversation.updated_at))
    )).scalars().all())
    return templates.TemplateResponse(
        request,
        "pilot.html",
        {
            "user": user,
            "conversations": conversations,
            "modes": await pilot.all_mode_statuses(),
            "mode_labels": pilot.MODE_LABELS,
            "default_mode": pilot.DEFAULT_MODE,
        },
    )


@router.post("/pilot/conversations", response_class=HTMLResponse, response_model=None)
async def create_conversation(
    request: Request,
    title: str = Form("New conversation"),
    mode: str = Form(pilot.DEFAULT_MODE),
    system_prompt: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    convo = Conversation(
        title=(title.strip() or "New conversation")[:255],
        mode=pilot.normalize_mode(mode),
        system_prompt=(system_prompt.strip() or None),
    )
    session.add(convo)
    await session.commit()
    await session.refresh(convo)
    return RedirectResponse(f"/pilot/conversations/{convo.id}", status_code=303)


@router.get("/pilot/conversations/{conversation_id}", response_class=HTMLResponse, response_model=None)
async def conversation_thread(
    request: Request,
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    convo = await session.get(Conversation, conversation_id)
    if not convo:
        raise HTTPException(404, "conversation not found")
    messages = list((await session.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.created_at.asc())
    )).scalars().all())

    settings = get_settings()
    web_search_available = web_search.get_provider() is not None
    web_search_usage_today = (
        await web_search.get_usage_today() if web_search_available else 0
    )

    # Live status for both modes: the composer uses the current one to warn
    # BEFORE the user types, and offers the other as the one-click way out.
    statuses = {s["mode"]: s for s in await pilot.all_mode_statuses()}
    current_mode = pilot.normalize_mode(convo.mode)
    other_mode = pilot.DEEP if current_mode == pilot.LIGHT else pilot.LIGHT

    return templates.TemplateResponse(
        request,
        "pilot_thread.html",
        {
            "user": user,
            "conversation": convo,
            "messages": messages,
            "mode": current_mode,
            "mode_labels": pilot.MODE_LABELS,
            "mode_blurbs": pilot.MODE_BLURBS,
            "status": statuses[current_mode],
            "other_mode": other_mode,
            "other_status": statuses[other_mode],
            "web_on": web_enabled(convo),
            "web_search_available": web_search_available,
            "web_search_usage_today": web_search_usage_today,
            "web_search_daily_budget": settings.web_search_daily_budget,
            "web_search_backend": settings.web_search_backend,
        },
    )


@router.post(
    "/pilot/conversations/{conversation_id}/settings",
    response_class=HTMLResponse, response_model=None,
)
async def update_conversation_settings(
    conversation_id: uuid.UUID,
    mode: str = Form(""),
    web: str = Form(""),
    search_site: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Per-thread controls: mode, web access on/off, optional site restriction.

    Mode is editable here precisely because resolution is late — switching a
    thread from Lightweight to Thoughtful changes the next turn and nothing
    else. Nothing about the thread's history needs to move.
    """
    convo = await session.get(Conversation, conversation_id)
    if not convo:
        raise HTTPException(404, "conversation not found")
    if mode:
        convo.mode = pilot.normalize_mode(mode)
    on = web.strip().lower() in ("on", "1", "true", "yes")
    convo.web_mode = "on" if on else "off"
    # The legacy boolean stays in sync for any old code path still reading it.
    convo.web_search_enabled = on
    convo.search_site = web_search.normalize_site(search_site)
    await session.commit()
    return RedirectResponse(
        f"/pilot/conversations/{conversation_id}#composer", status_code=303
    )


@router.post("/pilot/conversations/{conversation_id}/messages", response_class=HTMLResponse, response_model=None)
async def post_message(
    request: Request,
    conversation_id: uuid.UUID,
    content: str = Form(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    convo = await session.get(Conversation, conversation_id)
    if not convo:
        raise HTTPException(404, "conversation not found")
    text = content.strip()
    if not text:
        return RedirectResponse(f"/pilot/conversations/{conversation_id}", status_code=303)

    # Persist the user turn, then an empty assistant row the worker will fill.
    user_msg = ConversationMessage(
        conversation_id=conversation_id,
        role=MessageRole.USER,
        content=text,
        complete=True,
    )
    assistant_msg = ConversationMessage(
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT,
        content="",
        complete=False,
    )
    session.add_all([user_msg, assistant_msg])

    # First user message becomes the conversation title if it's still default.
    if convo.title == "New conversation":
        convo.title = text[:60]

    await session.flush()
    assistant_id = assistant_msg.id

    # Build the history to feed the model: optional system prompt, then the
    # full turn sequence including the message we just added.
    history: list[dict[str, str]] = []
    if convo.system_prompt:
        history.append({"role": "system", "content": convo.system_prompt})
    # User + system rows only. TOOL rows are audit-trail UI metadata — the
    # search results are folded into the prompt fresh each turn inside
    # dispatch_turn, never replayed from history.
    prior = list((await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.role.in_([MessageRole.USER, MessageRole.SYSTEM]),
        )
        .order_by(ConversationMessage.created_at.asc())
    )).scalars().all())
    # Also include completed assistant replies so the model has the back-and-forth.
    completed_assistants = list((await session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.role == MessageRole.ASSISTANT,
            ConversationMessage.complete.is_(True),
            ConversationMessage.content != "",
        )
        .order_by(ConversationMessage.created_at.asc())
    )).scalars().all())
    # Merge by created_at so the conversation reads in order.
    merged = sorted(prior + completed_assistants, key=lambda m: m.created_at)
    for m in merged:
        history.append({"role": m.role.value, "content": m.content})

    await session.commit()

    # Dispatch to a worker. If it fails synchronously (no worker online, send
    # failed), record the error directly on the assistant message.
    err = await dispatch_turn(convo, assistant_id, history)
    if err:
        async with session.begin():
            am = await session.get(ConversationMessage, assistant_id)
            if am:
                am.error = err
                am.complete = True

    return RedirectResponse(f"/pilot/conversations/{conversation_id}", status_code=303)


@router.post("/pilot/conversations/{conversation_id}/delete", response_class=HTMLResponse, response_model=None)
async def delete_conversation(
    request: Request,
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    convo = await session.get(Conversation, conversation_id)
    if convo:
        await session.delete(convo)  # cascades to messages
        await session.commit()
    return RedirectResponse("/pilot", status_code=303)


# ── mode configuration ─────────────────────────────────────────────


@router.get("/pilot/settings", response_class=HTMLResponse, response_model=None)
async def pilot_settings_page(
    request: Request,
    user: User = Depends(get_current_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pilot_settings.html",
        {
            "user": user,
            "modes": await pilot.all_mode_statuses(),
            "pools": await pilot.pool_options(),
        },
    )


@router.post("/pilot/settings", response_model=None)
async def pilot_settings_submit(
    request: Request,
    user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Form arrives as pool.<mode> / model.<mode> / tools.<mode> triples."""
    form = await request.form()
    for mode in pilot.MODES:
        pool = (form.get(f"pool.{mode}") or "").strip()
        if pool not in KNOWN_POOLS:
            continue
        model = (form.get(f"model.{mode}") or "").strip() or None
        # Tools are a Thoughtful-only capability. Ignore the field on any
        # other mode so a hand-crafted POST can't hand tools to the small
        # model — the whole point of the split.
        tools = mode == pilot.DEEP and bool(form.get(f"tools.{mode}"))
        await pilot.set_mode_config(mode, pool, model, tools)
    return RedirectResponse("/pilot/settings", status_code=303)


# ── legacy redirects ───────────────────────────────────────────────
#
# Operator's URLs were bookmark-worthy (a long thread is a real artifact), so
# every old path keeps working. 307 preserves the method, which matters for
# any form still posting to an /operator/* action from a stale open tab.


@router.api_route(
    "/operator{rest:path}",
    methods=["GET", "POST"],
    include_in_schema=False,
    response_model=None,
)
async def operator_legacy_redirect(rest: str) -> RedirectResponse:
    return RedirectResponse(f"/pilot{rest}", status_code=307)

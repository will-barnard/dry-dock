"""Operator module — a chat surface over the worker fleet.

Routes:
  GET  /operator                                  conversation list + new form
  POST /operator/conversations                    create a conversation
  GET  /operator/conversations/{id}                thread view
  POST /operator/conversations/{id}/messages       post a turn (dispatches to a worker)
  POST /operator/conversations/{id}/delete         delete a conversation

The streaming half lives in routes/streams.py (SSE) + orchestrator/chat.py.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_session
from app.models import Conversation, ConversationMessage, MessageRole, User
from app.orchestrator.chat import dispatch_turn
from app.orchestrator.pools import KNOWN_POOLS
from app.orchestrator.registry import registry
from app.orchestrator import web_search

router = APIRouter(tags=["operator"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/operator/pools/{pool}/models", response_model=None)
async def pool_models(
    pool: str,
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """Return the union of installed_models across all live workers in a pool."""
    if pool not in KNOWN_POOLS:
        raise HTTPException(400, f"unknown pool: {pool}")
    workers = await registry.by_pool(pool)
    models: list[str] = sorted({m for w in workers for m in w.installed_models})
    return JSONResponse({"models": models})


@router.get("/operator", response_class=HTMLResponse, response_model=None)
async def operator_home(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    conversations = list((await session.execute(
        select(Conversation).order_by(desc(Conversation.updated_at))
    )).scalars().all())
    return templates.TemplateResponse(
        request,
        "operator.html",
        {"user": user, "conversations": conversations, "pools": list(KNOWN_POOLS)},
    )


@router.post("/operator/conversations", response_class=HTMLResponse, response_model=None)
async def create_conversation(
    request: Request,
    title: str = Form("New conversation"),
    pool: str = Form("researcher"),
    model: str = Form(""),
    system_prompt: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if pool not in KNOWN_POOLS:
        raise HTTPException(400, f"unknown pool: {pool}")
    convo = Conversation(
        title=(title.strip() or "New conversation")[:255],
        pool=pool,
        model=(model.strip() or None),
        system_prompt=(system_prompt.strip() or None),
    )
    session.add(convo)
    await session.commit()
    await session.refresh(convo)
    return RedirectResponse(f"/operator/conversations/{convo.id}", status_code=303)


@router.get("/operator/conversations/{conversation_id}", response_class=HTMLResponse, response_model=None)
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
    # Web access runtime status — the template uses this to decide whether
    # to show the mode selector and what to display next to it.
    settings = get_settings()
    web_search_available = web_search.get_provider() is not None
    web_search_usage_today = (
        await web_search.get_usage_today() if web_search_available else 0
    )
    web_mode = getattr(convo, "web_mode", None) or (
        "search" if convo.web_search_enabled else "off"
    )
    return templates.TemplateResponse(
        request,
        "operator_thread.html",
        {
            "user": user,
            "conversation": convo,
            "messages": messages,
            "web_mode": web_mode,
            "web_search_available": web_search_available,
            "web_search_usage_today": web_search_usage_today,
            "web_search_daily_budget": settings.web_search_daily_budget,
            "web_search_backend": settings.web_search_backend,
        },
    )


_VALID_WEB_MODES = ("off", "search", "tools")


@router.post(
    "/operator/conversations/{conversation_id}/settings",
    response_class=HTMLResponse, response_model=None,
)
async def update_conversation_settings(
    conversation_id: uuid.UUID,
    web_mode: str = Form("off"),
    search_site: str = Form(""),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Set the per-conversation web access mode (off / search / tools) and an
    optional single-site restriction. Posted from the composer's controls —
    see operator_thread.html."""
    convo = await session.get(Conversation, conversation_id)
    if not convo:
        raise HTTPException(404, "conversation not found")
    mode = (web_mode or "off").strip().lower()
    if mode not in _VALID_WEB_MODES:
        mode = "off"
    convo.web_mode = mode
    # Keep the legacy boolean roughly in sync for any old code paths.
    convo.web_search_enabled = mode in ("search", "tools")
    # Normalize the site restriction down to a bare host (or clear it).
    convo.search_site = web_search.normalize_site(search_site)
    await session.commit()
    return RedirectResponse(
        f"/operator/conversations/{conversation_id}#composer", status_code=303
    )


@router.post("/operator/conversations/{conversation_id}/messages", response_class=HTMLResponse, response_model=None)
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
        return RedirectResponse(f"/operator/conversations/{conversation_id}", status_code=303)

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
    # User + system rows only. TOOL rows are audit-trail UI metadata in
    # Phase 1 — the search results are folded into the prompt fresh each
    # turn inside dispatch_turn, never replayed from history.
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

    return RedirectResponse(f"/operator/conversations/{conversation_id}", status_code=303)


@router.post("/operator/conversations/{conversation_id}/delete", response_class=HTMLResponse, response_model=None)
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
    return RedirectResponse("/operator", status_code=303)

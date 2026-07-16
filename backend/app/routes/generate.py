"""Public generate API — one-shot text generation for external apps.

`POST /api/v1/generate` lets any app with the DRYDOCK_API_KEY dispatch a
single inference to the worker fleet and get the finished text back in the
HTTP response. It is intentionally NOT behind the operator session cookie —
it authenticates with a dedicated API key (header `X-API-Key` or
`Authorization: Bearer <key>`), so it can be called from servers that have no
browser session (e.g. seedbook's Express backend generating sales follow-ups).

See DRYDOCK-API.md in the repo root for the full contract.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.orchestrator.generate import GenerateError, run_generate

router = APIRouter(prefix="/api/v1", tags=["generate"])


class GenerateRequest(BaseModel):
    """Either supply `messages` (full OpenAI-style chat array) or the
    convenience pair `prompt` (+ optional `system`). If both are given,
    `messages` wins."""

    prompt: str | None = Field(
        default=None,
        description="Single user prompt. Ignored if `messages` is provided.",
    )
    system: str | None = Field(
        default=None,
        description="Optional system prompt prepended when using `prompt`.",
    )
    messages: list[dict[str, str]] | None = Field(
        default=None,
        description="Full chat history: [{role, content}, ...]. Overrides `prompt`.",
    )
    pool: str | None = Field(
        default=None,
        description="Pin a worker pool (e.g. 'researcher'). Omit to auto-select.",
    )
    model: str | None = Field(
        default=None,
        description="Pin an Ollama model tag. Omit to use the worker's default.",
    )
    timeout: float | None = Field(
        default=None,
        description="Per-request timeout in seconds. Capped by the server default.",
        gt=0,
    )

    def to_messages(self) -> list[dict[str, str]]:
        if self.messages:
            return self.messages
        msgs: list[dict[str, str]] = []
        if self.system:
            msgs.append({"role": "system", "content": self.system})
        msgs.append({"role": "user", "content": self.prompt or ""})
        return msgs


class GenerateResponse(BaseModel):
    content: str
    model: str | None
    worker: str
    pool: str
    elapsed_ms: int


def _require_api_key(x_api_key: str | None, authorization: str | None) -> None:
    """Constant-time check of the presented key against DRYDOCK_API_KEY.

    Accepts either the `X-API-Key` header or `Authorization: Bearer <key>`.
    A blank server key means the API is disabled — return 503 rather than
    letting a blank-vs-blank comparison succeed."""
    settings = get_settings()
    server_key = settings.drydock_api_key
    if not server_key:
        raise HTTPException(503, "Generate API is disabled (DRYDOCK_API_KEY unset).")

    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, server_key):
        raise HTTPException(401, "Invalid or missing API key.")


@router.get("/generate/health")
async def generate_health(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict:
    """Cheap auth + liveness probe. Does not touch a worker."""
    _require_api_key(x_api_key, authorization)
    return {"status": "ok"}


@router.post("/generate", response_model=GenerateResponse)
async def generate(
    body: GenerateRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> GenerateResponse:
    _require_api_key(x_api_key, authorization)

    messages = body.to_messages()
    if not any((m.get("content") or "").strip() for m in messages):
        raise HTTPException(422, "Provide a non-empty `prompt` or `messages`.")

    settings = get_settings()
    cap = settings.generate_timeout_seconds
    timeout = min(body.timeout, cap) if body.timeout else cap

    try:
        result = await run_generate(
            messages, pool=body.pool, model=body.model, timeout=timeout
        )
    except GenerateError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    return GenerateResponse(**result)

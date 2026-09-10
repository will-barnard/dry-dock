"""Shared API-key auth for the public, non-cookie endpoints.

`/api/v1/generate` and `/api/v1/image` both authenticate with DRYDOCK_API_KEY
rather than the operator session cookie, so they can be called from servers
with no browser session (seedbook, gearline). The check lives here so the two
can't drift — in particular the blank-key-means-disabled rule, which exists so
a deploy that never sets the key can't be probed with a blank-vs-blank compare.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException

from app.config import get_settings


def require_api_key(
    x_api_key: str | None,
    authorization: str | None,
    *,
    what: str = "API",
) -> None:
    """Constant-time check of the presented key against DRYDOCK_API_KEY.

    Accepts `X-API-Key` or `Authorization: Bearer <key>`. Raises 503 when the
    server key is blank (feature disabled), 401 when the key doesn't match.
    """
    server_key = get_settings().drydock_api_key
    if not server_key:
        raise HTTPException(503, f"{what} is disabled (DRYDOCK_API_KEY unset).")

    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, server_key):
        raise HTTPException(401, "Invalid or missing API key.")

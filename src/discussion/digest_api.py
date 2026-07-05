"""Digest self-service (SPECIFICATION §9 §11a / M6).

  GET  /me/digest            read the caller's subscription
  PUT  /me/digest            update {cadence, send_hour_local, send_dow, timezone, scope, ai_summary, quiet_if_empty}
  POST /me/digest/send-now   on-demand digest for the caller (rate-limited)
"""
from __future__ import annotations

import time
from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .deps import identity
from .ldap_auth import Identity

router = APIRouter()

_CADENCE = ("off", "hourly", "daily", "weekly")
# Per-user on-demand rate limiter (process-local; a small guard, not a hard SLA).
_last_send_now: dict[tuple[str, str], float] = {}


@router.get("/me/digest")
async def get_digest(request: Request, ident: Identity = Depends(identity)) -> dict:
    return await run_in_threadpool(request.app.state.digest_store.get, ident.tenant, ident.user)


@router.put("/me/digest")
async def put_digest(request: Request, body: dict = Body(...),
                     ident: Identity = Depends(identity)) -> dict:
    b = body or {}
    cadence = b.get("cadence", "off")
    if cadence not in _CADENCE:
        raise HTTPException(status_code=422, detail=f"cadence must be one of {_CADENCE}")
    try:
        hour = int(b.get("send_hour_local", 8))
        dow = int(b.get("send_dow", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="send_hour_local / send_dow must be integers")
    if not (0 <= hour <= 23) or not (0 <= dow <= 6):
        raise HTTPException(status_code=422, detail="send_hour_local 0-23, send_dow 0-6")
    scope = b.get("scope") or {}
    if not isinstance(scope, dict):
        raise HTTPException(status_code=422, detail="scope must be an object")
    return await run_in_threadpool(partial(
        request.app.state.digest_store.upsert, ident.tenant, ident.user, cadence=cadence,
        send_hour_local=hour, send_dow=dow, timezone=str(b.get("timezone", "UTC")),
        scope=scope, ai_summary=bool(b.get("ai_summary", False)),
        quiet_if_empty=bool(b.get("quiet_if_empty", True))))


@router.post("/me/digest/send-now")
async def send_now(request: Request, ident: Identity = Depends(identity)) -> dict:
    cfg = request.app.state.config
    key = (ident.tenant, ident.user)
    now = time.time()
    last = _last_send_now.get(key, 0)
    if now - last < cfg.digest_send_now_ratelimit_s:
        raise HTTPException(status_code=429, detail="please wait before requesting another digest")
    _last_send_now[key] = now
    sender = request.app.state.digest_sender
    return await run_in_threadpool(sender.send_now, ident)

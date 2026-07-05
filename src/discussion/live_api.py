"""The live-panel WebSocket (SPECIFICATION §9 §10h / M4b).

  WS /files/{file_uid}/live   ?token=<bridge token>&tenant=<t>&invisible=1

Subscribe while the comment panel is open: receive live comment events + a presence
roster; the connection carries both. Auth mirrors the CSAI chat WS (Authorization
header or ``?token=``). READ on the anchor is required; admins may view invisibly
(``invisible=1``, §10h) — verified server-side, audited.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket
from fastapi.concurrency import run_in_threadpool

from . import audit
from .http_auth import extract_tenant, resolve_identity
from .live import Connection

log = logging.getLogger("discussion.live_api")

router = APIRouter()

_TRUE = ("1", "true", "yes", "on")


@router.websocket("/files/{file_uid}/live")
async def live(websocket: WebSocket, file_uid: str) -> None:
    app = websocket.app
    config = app.state.config
    hub = getattr(app.state, "live", None)
    if hub is None or not config.live_enabled:
        await websocket.close(code=4404)
        return

    headers = {k.lower(): v for k, v in websocket.headers.items()}
    auth = headers.get("authorization", "")
    token = websocket.query_params.get("token")
    if not auth and token:
        auth = "Bearer " + token
    tenant = websocket.query_params.get("tenant") or extract_tenant(
        headers, headers.get("host", ""), config.tenant)

    ident = await run_in_threadpool(
        resolve_identity, auth, tenant, config, app.state.token_store,
        getattr(app.state, "bridge_verifier", None))

    await websocket.accept()
    if ident is None:
        await websocket.send_json({"type": "error", "error": "authentication required"})
        await websocket.close(code=4401)
        return
    if not await run_in_threadpool(app.state.permissions.can_read, ident, file_uid):
        await websocket.send_json({"type": "error", "error": "forbidden"})
        await websocket.close(code=4403)
        return
    if hub.total() >= config.live_max_conns:
        await websocket.send_json({"type": "error", "error": "too many connections"})
        await websocket.close(code=4429)
        return

    # Invisible viewing: admin-only, config-gated, server-verified — audited (§10h).
    invisible = (websocket.query_params.get("invisible") in _TRUE
                 and config.presence_admin_invisible and ident.is_admin)
    if invisible:
        audit.record("panel.viewed_invisibly", actor=ident.user, tenant=ident.tenant,
                     file_uid=file_uid)

    conn = Connection(websocket, ident, file_uid, invisible=invisible)
    await hub.join(conn)
    try:
        while True:
            # We don't act on client messages (no typing/echo); this just detects close.
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        await hub.leave(conn)

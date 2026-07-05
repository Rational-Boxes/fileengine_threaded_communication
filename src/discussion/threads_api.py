"""Threads & comments HTTP surface (SPECIFICATION §9 / M1 + M2).

  GET    /files/{file_uid}/threads       list threads on a document      (READ)
  POST   /files/{file_uid}/threads       open a thread {version?,title,body} (READ)
  GET    /threads/{id}                   thread + comments               (READ)
  POST   /threads/{id}/comments          reply {body, mentions:[email]}  (READ; §5.1)
  PATCH  /threads/{id}                   resolve/reopen {status,...}      (opener|WRITE)
  PATCH  /comments/{id}                  edit own comment (versioned)     (author)
  DELETE /comments/{id}                  soft-delete own comment          (author)
  POST   /comments/{id}/redact           mask + de-index, retain original (tenant admin, §5b)

Permissions derive from the anchor ``file_uid`` and are evaluated as the caller (§5).
Mentions are validated per target on submit and error-marked if they lack READ (§5.1).
Notifications + discussion events are written on each action. Blocking gRPC/DB work
runs off the event loop.
"""
from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from . import audit
from .deps import identity, require_tenant_admin
from .ldap_auth import Identity
from .markdown_text import to_plaintext
from .targets import validate_targets

router = APIRouter()


def _s(request: Request, name: str):
    return getattr(request.app.state, name)


async def _require(request: Request, ident: Identity, file_uid: str, perm: str) -> None:
    fn = _s(request, "permissions").can_read if perm == "r" else _s(request, "permissions").can_write
    if not await run_in_threadpool(fn, ident, file_uid):
        raise HTTPException(status_code=403, detail="permission denied")


def _clean_body(request: Request, raw) -> str:
    body = (raw or "").strip()
    if not body:
        raise HTTPException(status_code=422, detail="comment body is required")
    limit = request.app.state.config.max_comment_chars
    if len(body) > limit:
        raise HTTPException(status_code=422, detail=f"comment exceeds {limit} characters")
    return body


async def _notify(request: Request, tenant: str, users, *, kind: str, file_uid: str,
                  actor: str, thread_id=None) -> None:
    notif = _s(request, "notifications")
    for u in users:
        await run_in_threadpool(partial(
            notif.add, tenant, user_id=u, kind=kind, file_uid=file_uid, actor=actor,
            thread_id=thread_id))


async def _index(request: Request, tenant: str, *, comment_id: str, file_uid: str,
                 thread_id: str, text: str) -> None:
    """Best-effort comment indexing (§6) — never fails the request."""
    try:
        await run_in_threadpool(partial(
            _s(request, "indexer").index_comment, tenant, comment_id=comment_id,
            file_uid=file_uid, thread_id=thread_id, text=text))
    except Exception:
        pass


async def _deindex(request: Request, tenant: str, comment_id: str) -> None:
    try:
        await run_in_threadpool(_s(request, "indexer").remove_comment, tenant, comment_id)
    except Exception:
        pass


async def _live(request: Request, tenant: str, file_uid: str, message: dict) -> None:
    """Best-effort live fan-out to open panels on this file (§10h) — never fails a write."""
    hub = getattr(request.app.state, "live", None)
    if hub is None:
        return
    try:
        await hub.broadcast(tenant, file_uid, message)
    except Exception:
        pass


# ------------------------------ threads ------------------------------------
@router.get("/files/{file_uid}/threads")
async def list_threads(file_uid: str, request: Request,
                       status: str | None = Query(None, pattern="^(open|resolved)$"),
                       ident: Identity = Depends(identity)) -> dict:
    await _require(request, ident, file_uid, "r")
    threads = await run_in_threadpool(
        partial(_s(request, "store").list_threads, ident.tenant, file_uid, status=status))
    return {"threads": threads}


@router.post("/files/{file_uid}/threads", status_code=201)
async def open_thread(file_uid: str, request: Request, body: dict = Body(...),
                      ident: Identity = Depends(identity)) -> dict:
    await _require(request, ident, file_uid, "r")
    text = _clean_body(request, (body or {}).get("body"))
    version = ((body or {}).get("version") or "").strip()
    title = ((body or {}).get("title") or "").strip()
    thread = await run_in_threadpool(partial(
        _s(request, "store").create_thread, ident.tenant, file_uid=file_uid, version=version,
        title=title, body=text, body_text=to_plaintext(text), opened_by=ident.user))
    if thread.get("comments"):
        first = thread["comments"][0]
        await _index(request, ident.tenant, comment_id=first["id"],
                     file_uid=file_uid, thread_id=thread["id"], text=to_plaintext(text))
        await _live(request, ident.tenant, file_uid,
                    {"type": "comment", "action": "created", "thread_id": thread["id"],
                     "comment": first})
    events = _s(request, "events")
    events.publish("thread.opened", tenant=ident.tenant, file_uid=file_uid, actor=ident.user,
                   thread_id=thread["id"])
    events.publish("comment.created", tenant=ident.tenant, file_uid=file_uid, actor=ident.user,
                   thread_id=thread["id"])
    return thread


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request,
                     ident: Identity = Depends(identity)) -> dict:
    meta = await run_in_threadpool(_s(request, "store").thread_meta, ident.tenant, thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="thread not found")
    await _require(request, ident, meta["file_uid"], "r")
    thread = await run_in_threadpool(_s(request, "store").get_thread, ident.tenant, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


@router.patch("/threads/{thread_id}")
async def set_thread_status(thread_id: str, request: Request, body: dict = Body(...),
                            ident: Identity = Depends(identity)) -> dict:
    store = _s(request, "store")
    meta = await run_in_threadpool(store.thread_meta, ident.tenant, thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="thread not found")
    status = (body or {}).get("status")
    if status not in ("open", "resolved"):
        raise HTTPException(status_code=422, detail="status must be 'open' or 'resolved'")

    # Authorize: the thread opener, or anyone with WRITE on the file (§5).
    allowed = meta["opened_by"] == ident.user
    if not allowed:
        allowed = await run_in_threadpool(_s(request, "permissions").can_write, ident, meta["file_uid"])
    if not allowed:
        raise HTTPException(status_code=403, detail="permission denied")

    resolving = status == "resolved"
    thread = await run_in_threadpool(partial(
        store.set_thread_status, ident.tenant, thread_id, status=status,
        resolved_by=ident.user if resolving else None,
        resolved_version=((body or {}).get("resolved_version") or None) if resolving else None))
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")

    if resolving:
        participants = await run_in_threadpool(store.thread_participants, ident.tenant, thread_id)
        await _notify(request, ident.tenant, [p for p in participants if p != ident.user],
                      kind="thread_resolved", file_uid=meta["file_uid"], actor=ident.user,
                      thread_id=thread_id)
        _s(request, "events").publish("thread.resolved", tenant=ident.tenant,
                                      file_uid=meta["file_uid"], actor=ident.user, thread_id=thread_id)
        await _live(request, ident.tenant, meta["file_uid"],
                    {"type": "thread", "action": "resolved", "thread_id": thread_id})
    return thread


# ------------------------------ comments -----------------------------------
@router.post("/threads/{thread_id}/comments", status_code=201)
async def add_comment(thread_id: str, request: Request, body: dict = Body(...),
                      ident: Identity = Depends(identity)) -> dict:
    store = _s(request, "store")
    meta = await run_in_threadpool(store.thread_meta, ident.tenant, thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="thread not found")
    file_uid = meta["file_uid"]
    await _require(request, ident, file_uid, "r")
    text = _clean_body(request, (body or {}).get("body"))

    # Validate mentions *before* writing (§5.1): any target lacking READ error-marks
    # the whole submit so the author can fix and resubmit — no partial mention.
    mentions = (body or {}).get("mentions") or []
    valid = []
    if mentions:
        valid, invalid = await run_in_threadpool(
            validate_targets, _s(request, "directory"), _s(request, "permissions"), file_uid, mentions)
        if invalid:
            raise HTTPException(status_code=422,
                                detail={"error": "some mentioned users cannot access this file",
                                        "invalid_mentions": invalid})

    comment = await run_in_threadpool(partial(
        store.add_comment, ident.tenant, thread_id, author=ident.user,
        body=text, body_text=to_plaintext(text)))
    await _index(request, ident.tenant, comment_id=comment["id"], file_uid=file_uid,
                 thread_id=thread_id, text=to_plaintext(text))
    await _live(request, ident.tenant, file_uid,
                {"type": "comment", "action": "created", "thread_id": thread_id, "comment": comment})

    events = _s(request, "events")
    mentioned_uids = set()
    for _id, principal in valid:
        uid = principal.user
        mentioned_uids.add(uid)
        await run_in_threadpool(partial(
            store.add_mention, ident.tenant, comment_id=comment["id"], thread_id=thread_id,
            target_user=uid))
        await run_in_threadpool(partial(
            _s(request, "notifications").add, ident.tenant, user_id=uid, kind="mention",
            file_uid=file_uid, actor=ident.user, thread_id=thread_id))
        events.publish("mention.created", tenant=ident.tenant, file_uid=file_uid,
                       actor=ident.user, thread_id=thread_id, target_user=uid)

    # Reply notifications to other participants (not the author, not just-mentioned).
    participants = await run_in_threadpool(store.thread_participants, ident.tenant, thread_id)
    await _notify(request, ident.tenant,
                  [p for p in participants if p != ident.user and p not in mentioned_uids],
                  kind="reply", file_uid=file_uid, actor=ident.user, thread_id=thread_id)
    events.publish("comment.created", tenant=ident.tenant, file_uid=file_uid, actor=ident.user,
                   thread_id=thread_id)
    return comment


@router.patch("/comments/{comment_id}")
async def edit_comment(comment_id: str, request: Request, body: dict = Body(...),
                       ident: Identity = Depends(identity)) -> dict:
    store = _s(request, "store")
    comment = await run_in_threadpool(store.get_comment, ident.tenant, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment["author"] != ident.user:
        raise HTTPException(status_code=403, detail="only the author may edit a comment")
    text = _clean_body(request, (body or {}).get("body"))
    updated = await run_in_threadpool(partial(
        store.edit_comment, ident.tenant, comment_id, body=text, body_text=to_plaintext(text)))
    if updated is None:
        raise HTTPException(status_code=409, detail="comment cannot be edited (deleted or redacted)")
    await _index(request, ident.tenant, comment_id=comment_id, file_uid=updated["file_uid"],
                 thread_id=updated["thread_id"], text=to_plaintext(text))
    await _live(request, ident.tenant, updated["file_uid"],
                {"type": "comment", "action": "updated", "thread_id": updated["thread_id"],
                 "comment": updated})
    return updated


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, request: Request,
                         ident: Identity = Depends(identity)) -> dict:
    store = _s(request, "store")
    comment = await run_in_threadpool(store.get_comment, ident.tenant, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment["author"] != ident.user:
        raise HTTPException(status_code=403, detail="only the author may delete a comment")
    ok = await run_in_threadpool(store.soft_delete_comment, ident.tenant, comment_id)
    if ok:
        await _deindex(request, ident.tenant, comment_id)
        await _live(request, ident.tenant, comment["file_uid"],
                    {"type": "comment", "action": "deleted", "thread_id": comment["thread_id"],
                     "comment_id": comment_id})
    return {"deleted": bool(ok)}


@router.post("/comments/{comment_id}/redact")
async def redact_comment(comment_id: str, request: Request, body: dict = Body(default={}),
                         ident: Identity = Depends(require_tenant_admin)) -> dict:
    """Administrator moderation (§5b): mask the display + de-index; the original is
    retained forever in ``redactions``. Recorded in the audit log — hidden from
    peers, never from governance."""
    store = _s(request, "store")
    reason = ((body or {}).get("reason") or "").strip()
    masked = await run_in_threadpool(partial(
        store.redact_comment, ident.tenant, comment_id, redacted_by=ident.user, reason=reason))
    if masked is None:
        raise HTTPException(status_code=404, detail="comment not found or already redacted")
    audit.record("comment.redacted", actor=ident.user, tenant=ident.tenant,
                 comment_id=comment_id, file_uid=masked.get("file_uid", ""), reason=reason)
    await _deindex(request, ident.tenant, comment_id)
    await _live(request, ident.tenant, masked.get("file_uid", ""),
                {"type": "comment", "action": "redacted", "thread_id": masked.get("thread_id"),
                 "comment_id": comment_id})
    return masked

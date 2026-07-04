"""Threads & comments HTTP surface (SPECIFICATION §9 / M1).

  GET    /files/{file_uid}/threads       list threads on a document      (READ)
  POST   /files/{file_uid}/threads       open a thread {version?,title,body} (READ)
  GET    /threads/{id}                   thread + comments               (READ)
  POST   /threads/{id}/comments          reply {body}                    (READ)
  PATCH  /threads/{id}                   resolve/reopen {status,...}      (opener|WRITE)
  PATCH  /comments/{id}                  edit own comment (versioned)     (author)
  DELETE /comments/{id}                  soft-delete own comment          (author)

Permissions are derived from the anchor ``file_uid`` and evaluated as the caller
(§5). Blocking gRPC/DB work runs off the event loop via run_in_threadpool.
Mentions/reviews/redaction arrive in M2.
"""
from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from .deps import identity
from .ldap_auth import Identity
from .markdown_text import to_plaintext

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


def _perms(request: Request):
    return request.app.state.permissions


async def _require(request: Request, ident: Identity, file_uid: str, perm: str) -> None:
    """403 unless ``ident`` has ``perm`` on the anchor ``file_uid`` (fail-closed)."""
    perms = _perms(request)
    fn = perms.can_read if perm == "r" else perms.can_write
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


# ------------------------------ threads ------------------------------------
@router.get("/files/{file_uid}/threads")
async def list_threads(file_uid: str, request: Request,
                       status: str | None = Query(None, pattern="^(open|resolved)$"),
                       ident: Identity = Depends(identity)) -> dict:
    await _require(request, ident, file_uid, "r")
    threads = await run_in_threadpool(
        partial(_store(request).list_threads, ident.tenant, file_uid, status=status))
    return {"threads": threads}


@router.post("/files/{file_uid}/threads", status_code=201)
async def open_thread(file_uid: str, request: Request, body: dict = Body(...),
                      ident: Identity = Depends(identity)) -> dict:
    await _require(request, ident, file_uid, "r")
    text = _clean_body(request, (body or {}).get("body"))
    version = ((body or {}).get("version") or "").strip()
    title = ((body or {}).get("title") or "").strip()
    return await run_in_threadpool(partial(
        _store(request).create_thread, ident.tenant, file_uid=file_uid, version=version,
        title=title, body=text, body_text=to_plaintext(text), opened_by=ident.user))


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request,
                     ident: Identity = Depends(identity)) -> dict:
    meta = await run_in_threadpool(_store(request).thread_meta, ident.tenant, thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="thread not found")
    await _require(request, ident, meta["file_uid"], "r")
    thread = await run_in_threadpool(_store(request).get_thread, ident.tenant, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


@router.patch("/threads/{thread_id}")
async def set_thread_status(thread_id: str, request: Request, body: dict = Body(...),
                            ident: Identity = Depends(identity)) -> dict:
    meta = await run_in_threadpool(_store(request).thread_meta, ident.tenant, thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="thread not found")
    status = (body or {}).get("status")
    if status not in ("open", "resolved"):
        raise HTTPException(status_code=422, detail="status must be 'open' or 'resolved'")

    # Authorize: the thread opener, or anyone with WRITE on the file (§5). (Assigned
    # reviewers also gain this in M2 with the review model.)
    allowed = meta["opened_by"] == ident.user
    if not allowed:
        allowed = await run_in_threadpool(_perms(request).can_write, ident, meta["file_uid"])
    if not allowed:
        raise HTTPException(status_code=403, detail="permission denied")

    resolving = status == "resolved"
    thread = await run_in_threadpool(partial(
        _store(request).set_thread_status, ident.tenant, thread_id, status=status,
        resolved_by=ident.user if resolving else None,
        resolved_version=((body or {}).get("resolved_version") or None) if resolving else None))
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return thread


# ------------------------------ comments -----------------------------------
@router.post("/threads/{thread_id}/comments", status_code=201)
async def add_comment(thread_id: str, request: Request, body: dict = Body(...),
                      ident: Identity = Depends(identity)) -> dict:
    meta = await run_in_threadpool(_store(request).thread_meta, ident.tenant, thread_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="thread not found")
    await _require(request, ident, meta["file_uid"], "r")
    text = _clean_body(request, (body or {}).get("body"))
    # `mentions` (if present) are validated + persisted in M2; ignored here.
    return await run_in_threadpool(partial(
        _store(request).add_comment, ident.tenant, thread_id, author=ident.user,
        body=text, body_text=to_plaintext(text)))


@router.patch("/comments/{comment_id}")
async def edit_comment(comment_id: str, request: Request, body: dict = Body(...),
                       ident: Identity = Depends(identity)) -> dict:
    comment = await run_in_threadpool(_store(request).get_comment, ident.tenant, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment["author"] != ident.user:
        raise HTTPException(status_code=403, detail="only the author may edit a comment")
    text = _clean_body(request, (body or {}).get("body"))
    updated = await run_in_threadpool(partial(
        _store(request).edit_comment, ident.tenant, comment_id,
        body=text, body_text=to_plaintext(text)))
    if updated is None:
        raise HTTPException(status_code=409, detail="comment cannot be edited (deleted or redacted)")
    return updated


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, request: Request,
                         ident: Identity = Depends(identity)) -> dict:
    comment = await run_in_threadpool(_store(request).get_comment, ident.tenant, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment["author"] != ident.user:
        raise HTTPException(status_code=403, detail="only the author may delete a comment")
    ok = await run_in_threadpool(_store(request).soft_delete_comment, ident.tenant, comment_id)
    return {"deleted": bool(ok)}

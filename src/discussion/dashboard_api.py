"""Dashboard feeds, file-list attention flags & comment resolve (SPEC §9 §10 / M4a).

  GET  /dashboard/attention        the caller's attention feed, ACL-filtered (§10a)
  POST /dashboard/attention/{id}/seen   mark one seen (state only, no badges)
  GET  /dashboard/activity         new/updated docs the caller may see (§10a)
  POST /attention/flags            per-file flagged/needs-review counts, batch (§10e)
  GET  /comments/{id}              resolve a comment (for a `?comment=` permalink, §10f)

Every feed read re-checks, per row, that the anchor ``file_uid`` is both READable
as the caller AND still live (not soft-deleted) — so a lost-access or trashed
document disappears from every surface. The digest applies the same two-part guard,
keeping all attention/activity surfaces consistent (§10a).
"""
from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from .deps import identity
from .ldap_auth import Identity

router = APIRouter()


def _s(request: Request, name: str):
    return getattr(request.app.state, name)


async def _readable(request: Request, ident: Identity, file_uid: str) -> bool:
    return await run_in_threadpool(_s(request, "permissions").can_read, ident, file_uid)


async def _live(request: Request, ident: Identity, file_uid: str) -> bool:
    return await run_in_threadpool(_s(request, "permissions").is_live, ident, file_uid)


@router.get("/dashboard/attention")
async def attention(request: Request, limit: int = Query(50, ge=1, le=200),
                    unread: bool = Query(False), ident: Identity = Depends(identity)) -> dict:
    rows = await run_in_threadpool(partial(
        _s(request, "notifications").list_for, ident.tenant, ident.user,
        limit=limit, unread_only=unread))
    # Re-check READ per row (over-fetch → filter), so a lost-access item disappears,
    # and drop items whose anchor file is soft-deleted (same guard as the activity
    # feed) — a trashed document must not surface in any dashboard feed.
    out = []
    for r in rows:
        if await _readable(request, ident, r["file_uid"]) and await _live(request, ident, r["file_uid"]):
            out.append(r)
    return {"items": out}


@router.post("/dashboard/attention/{notification_id}/seen")
async def mark_seen(notification_id: int, request: Request,
                    ident: Identity = Depends(identity)) -> dict:
    ok = await run_in_threadpool(
        _s(request, "notifications").mark_seen, ident.tenant, ident.user, notification_id)
    return {"seen": bool(ok)}


@router.get("/dashboard/activity")
async def activity(request: Request, limit: int = Query(50, ge=1, le=200),
                   ident: Identity = Depends(identity)) -> dict:
    # Over-fetch then filter so the returned page is all readable AND live to the
    # caller: drop rows the caller can't READ, and drop soft-deleted files (an item
    # deleted before its file.deleted event was pruned, or recorded pre-fix). The
    # cheap cached READ check runs first; is_live only for rows that survive it.
    rows = await run_in_threadpool(partial(
        _s(request, "activity").recent, ident.tenant, limit=limit * 4))
    out = []
    for r in rows:
        if await _readable(request, ident, r["file_uid"]) and await _live(request, ident, r["file_uid"]):
            out.append(r)
            if len(out) >= limit:
                break
    return {"items": out}


@router.post("/attention/flags")
async def attention_flags(request: Request, body: dict = Body(...),
                          ident: Identity = Depends(identity)) -> dict:
    """Batch: {file_uids:[…]} → {uid: {mentions, reviews}} for the caller (§10e)."""
    file_uids = [u for u in ((body or {}).get("file_uids") or []) if u]
    if not file_uids:
        return {"flags": {}}
    mentions = await run_in_threadpool(
        _s(request, "store").mention_flags, ident.tenant, ident.user, file_uids)
    reviews = await run_in_threadpool(
        _s(request, "reviews").review_flags, ident.tenant, ident.user, file_uids)
    flags = {}
    for uid in set(file_uids):
        m, r = mentions.get(uid, 0), reviews.get(uid, 0)
        if m or r:
            flags[uid] = {"mentions": m, "reviews": r}
    return {"flags": flags}


@router.get("/comments/{comment_id}")
async def get_comment(comment_id: str, request: Request,
                      ident: Identity = Depends(identity)) -> dict:
    """Resolve a comment (its thread + anchor) for a `?comment=` deep link (§10f)."""
    comment = await run_in_threadpool(_s(request, "store").get_comment, ident.tenant, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if not await _readable(request, ident, comment["file_uid"]):
        raise HTTPException(status_code=403, detail="permission denied")
    return comment

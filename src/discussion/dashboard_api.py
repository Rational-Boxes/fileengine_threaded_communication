# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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

from datetime import datetime, timezone

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
    #
    # One exception, and it is the reason `detail_text` exists. A share item like
    # "your link stopped working" is raised PRECISELY BECAUSE the creator lost
    # access to the resource, so this filter would suppress the single item that
    # most needs to arrive. Rows carrying their own text are self-contained: they
    # are rendered without resolving `file_uid` at all, so nothing is disclosed
    # that the recipient did not already know when they minted the link.
    #
    # The exemption is narrow on purpose — self-contained rows only. It is not a
    # per-kind allowlist, because that would drift the moment a kind is added.
    out = []
    for r in rows:
        if r.get("detail_text"):
            out.append(r)
            continue
        if await _readable(request, ident, r["file_uid"]) and await _live(request, ident, r["file_uid"]):
            out.append(r)
    return {"items": out}


@router.post("/dashboard/attention/{notification_id}/seen")
async def mark_seen(notification_id: int, request: Request,
                    ident: Identity = Depends(identity)) -> dict:
    ok = await run_in_threadpool(
        _s(request, "notifications").mark_seen, ident.tenant, ident.user, notification_id)
    return {"seen": bool(ok)}


def _activity_from_core(config, ident: Identity, limit: int) -> list[dict]:
    """Ask the core what changed, instead of assembling it from events here.

    This used to read `document_activity` — a projection fed by an event for
    every file touched — over-fetch four times the page, and then make TWO
    permission calls per row: up to 800 round trips for one page, each building
    and closing a core client. It is the endpoint that returned 504 under load.

    The core holds the files, their versions and their ACLs, so it can answer
    the whole question in one query, already filtered to what this identity may
    read. `examined` minus the number returned is how much recent activity the
    caller cannot see; `scan_truncated` says a short page means the scan bound
    was reached rather than that there is nothing older.
    """
    from .core_client import client_for
    mf = client_for(ident, config)
    try:
        res = mf.list_recent_files(limit=limit, tenant=ident.tenant)
    finally:
        try:
            mf.close()
        except Exception:
            pass

    items: list[dict] = []
    for i, e in enumerate(res.get("entries", [])):
        ts = e.get("modified_at") or 0
        items.append({
            # The shape the SPA already maps (discussionService.toActivity).
            # `id` is positional: these rows are a query result now, not stored
            # projection rows with identities of their own.
            "id": i,
            "file_uid": e.get("uid", ""),
            # One version means the file is new; more means it was updated. The
            # projection recorded this as an event type; the core reports the
            # count, which says the same thing without needing the events.
            "event_type": "created" if (e.get("version_count") or 0) <= 1 else "updated",
            "version": e.get("version", ""),
            "name": e.get("name", ""),
            # No path: producing one costs an ancestor walk per row in the core,
            # which is the cost this whole change exists to remove. The SPA
            # already treats it as optional.
            "path": "",
            "actor": e.get("modified_by", ""),
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
        })
    return items


@router.get("/dashboard/activity")
async def activity(request: Request, limit: int = Query(50, ge=1, le=200),
                   ident: Identity = Depends(identity)) -> dict:
    items = await run_in_threadpool(
        partial(_activity_from_core, request.app.state.config, ident, limit))
    return {"items": items}


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

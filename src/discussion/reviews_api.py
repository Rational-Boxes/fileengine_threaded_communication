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

"""Review-request HTTP surface (SPECIFICATION §9 / M2).

  POST /files/{file_uid}/reviews   raise a review {reviewers:[email], version?, thread_id?} (READ)
  GET  /files/{file_uid}/reviews   the review record for a file — all requests (READ)
  POST /reviews/{id}/acknowledge   reviewer acks → requester notified
  POST /reviews/{id}/complete      reviewer completes {outcome} → requester notified
  GET  /reviews?role=&status=      the caller's reviews (as requester and/or reviewer)

Raising requires READ on the anchor; each reviewer is validated to hold READ (§5.1,
error-marked if not). Ack/complete are reviewer-only. Notifications + discussion
events are written on each transition.
"""
from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from .deps import identity
from .ldap_auth import Identity
from .targets import validate_targets

router = APIRouter()


def _s(request: Request, name: str):
    return getattr(request.app.state, name)


@router.post("/files/{file_uid}/reviews", status_code=201)
async def raise_review(file_uid: str, request: Request, body: dict = Body(...),
                       ident: Identity = Depends(identity)) -> dict:
    if not await run_in_threadpool(_s(request, "permissions").can_read, ident, file_uid):
        raise HTTPException(status_code=403, detail="permission denied")
    reviewers = (body or {}).get("reviewers") or []
    if not reviewers:
        raise HTTPException(status_code=422, detail="at least one reviewer is required")

    valid, invalid = await run_in_threadpool(
        validate_targets, _s(request, "directory"), _s(request, "permissions"), file_uid, reviewers)
    if invalid:
        # Error-mark: reject the submit, name the reviewers who lack access (§5.1).
        raise HTTPException(status_code=422,
                            detail={"error": "some reviewers cannot access this file",
                                    "invalid_reviewers": invalid})

    version = ((body or {}).get("version") or "").strip()
    thread_id = (body or {}).get("thread_id") or None
    reviewer_uids = [p.user for _id, p in valid]
    reviews = await run_in_threadpool(partial(
        _s(request, "reviews").create, ident.tenant, file_uid=file_uid, version=version,
        thread_id=thread_id, requester=ident.user, reviewers=reviewer_uids))

    notif, events = _s(request, "notifications"), _s(request, "events")
    for r in reviews:
        await run_in_threadpool(partial(
            notif.add, ident.tenant, user_id=r["reviewer"], kind="review_requested",
            file_uid=file_uid, actor=ident.user, thread_id=thread_id, review_id=r["id"]))
        events.publish("review.requested", tenant=ident.tenant, file_uid=file_uid,
                       actor=ident.user, review_id=r["id"], target_user=r["reviewer"])
    return {"reviews": reviews}


@router.get("/files/{file_uid}/reviews")
async def list_file_reviews(file_uid: str, request: Request,
                            status: str | None = Query(
                                None, pattern="^(requested|acknowledged|completed|declined)$"),
                            ident: Identity = Depends(identity)) -> dict:
    """The review record for the anchor — every request raised on the file, whoever
    asked or was assigned. Visible to anyone who can READ the file (the record is
    part of the document, like its comments)."""
    if not await run_in_threadpool(_s(request, "permissions").can_read, ident, file_uid):
        raise HTTPException(status_code=403, detail="permission denied")
    reviews = await run_in_threadpool(partial(
        _s(request, "reviews").list_for_file, ident.tenant, file_uid, status=status))
    return {"reviews": reviews}


async def _transition(request: Request, review_id: str, ident: Identity, *, status: str,
                      kind: str, event: str, outcome=None) -> dict:
    review = await run_in_threadpool(_s(request, "reviews").get, ident.tenant, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    if review["reviewer"] != ident.user:
        raise HTTPException(status_code=403, detail="only the assigned reviewer may act on this review")
    allowed_from = {"acknowledged": ("requested",),
                    "completed": ("requested", "acknowledged")}[status]
    if review["status"] not in allowed_from:
        raise HTTPException(status_code=409, detail=f"review is {review['status']}")

    updated = await run_in_threadpool(partial(
        _s(request, "reviews").set_status, ident.tenant, review_id, status=status, outcome=outcome))
    if updated is None:
        raise HTTPException(status_code=404, detail="review not found")

    await run_in_threadpool(partial(
        _s(request, "notifications").add, ident.tenant, user_id=updated["requester"], kind=kind,
        file_uid=updated["file_uid"], actor=ident.user, thread_id=updated["thread_id"],
        review_id=review_id))
    _s(request, "events").publish(event, tenant=ident.tenant, file_uid=updated["file_uid"],
                                  actor=ident.user, review_id=review_id,
                                  target_user=updated["requester"])
    return updated


@router.post("/reviews/{review_id}/acknowledge")
async def acknowledge(review_id: str, request: Request,
                      ident: Identity = Depends(identity)) -> dict:
    return await _transition(request, review_id, ident, status="acknowledged",
                             kind="review_acknowledged", event="review.acknowledged")


@router.post("/reviews/{review_id}/complete")
async def complete(review_id: str, request: Request, body: dict = Body(default={}),
                   ident: Identity = Depends(identity)) -> dict:
    outcome = ((body or {}).get("outcome") or "").strip() or None
    return await _transition(request, review_id, ident, status="completed",
                             kind="review_completed", event="review.completed", outcome=outcome)


@router.get("/reviews")
async def list_reviews(request: Request,
                       role: str = Query("both", pattern="^(requester|reviewer|both)$"),
                       status: str | None = Query(None, pattern="^(requested|acknowledged|completed|declined)$"),
                       ident: Identity = Depends(identity)) -> dict:
    reviews = await run_in_threadpool(partial(
        _s(request, "reviews").list_for, ident.tenant, ident.user, role=role, status=status))
    return {"reviews": reviews}

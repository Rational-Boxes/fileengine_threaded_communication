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

"""Comment search + internal RAG retrieve (SPECIFICATION §9 §6 / M3).

  GET  /search?q=&limit=      FTS/fuzzy over comments the caller may read (ACL-filtered)
  POST /internal/retrieve     RAG candidates for CSAI (Option A) — system-admin only,
                              NOT ACL-filtered here (CSAI re-applies its own gate)
"""
from __future__ import annotations

from functools import partial

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from .deps import identity, require_system_admin
from .ldap_auth import Identity

router = APIRouter()


@router.get("/search")
async def search(request: Request, q: str = Query(..., min_length=1),
                 limit: int = Query(20, ge=1, le=100),
                 ident: Identity = Depends(identity)) -> dict:
    cap = min(limit, request.app.state.config.max_results)
    results = await run_in_threadpool(partial(
        request.app.state.searcher.search, ident, q, limit=cap))
    return {"results": results}


@router.post("/internal/retrieve")
async def internal_retrieve(request: Request, body: dict = Body(...),
                            ident: Identity = Depends(require_system_admin)) -> dict:
    query = ((body or {}).get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="query is required")
    k = int((body or {}).get("k") or 8)
    k = max(1, min(k, request.app.state.config.max_results))
    candidates = await run_in_threadpool(partial(
        request.app.state.searcher.retrieve, ident.tenant, query, k=k))
    return {"candidates": candidates}

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

"""Comment search + RAG retrieval (SPECIFICATION §6).

Two consumers of the ``comment_chunks`` index:

- ``search(identity, query)`` — the dashboard/in-thread comment search: full-text +
  fuzzy, **ACL-filtered as the caller** (over-fetch → drop chunks whose anchor the
  user can't READ), deduplicated to one hit per comment.
- ``retrieve(tenant, query, k)`` — the internal RAG source for CSAI (Option A, §6):
  vector ANN candidates with **no** ACL filter here; CSAI re-applies its own
  ``can_read(file_uid)`` gate. Exposed only to system-admin callers (search_api).
"""
from __future__ import annotations

from .ldap_auth import Identity


class Searcher:
    def __init__(self, embedder, chunk_store, permissions):
        self.embedder = embedder
        self.chunks = chunk_store
        self.perms = permissions

    def search(self, identity: Identity, query: str, *, limit: int = 20) -> list[dict]:
        rows = self.chunks.fts_search(identity.tenant, query, fetch=max(limit * 5, limit))
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            if r["comment_id"] in seen:
                continue
            if self.perms.can_read(identity, r["file_uid"]):
                seen.add(r["comment_id"])
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def retrieve(self, tenant: str, query: str, *, k: int = 8) -> list[dict]:
        qv = self.embedder.embed_query(query)
        return self.chunks.ann_search(tenant, qv, k)

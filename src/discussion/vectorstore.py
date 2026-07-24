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

"""``comment_chunks`` CRUD + retrieval (SPECIFICATION §6).

Vector (pgvector HNSW cosine) + full-text (tsvector / pg_trgm) over comment text,
keyed by the anchor ``file_uid`` so the existing ``can_read(file_uid)`` gate is the
right filter at query time — no new permission logic. Per-tenant schema via
``connect_for_tenant``.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .config import Config
from .db import connect_for_tenant


def _vec_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class ChunkStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, *, readonly: bool = False, provision: bool = False):
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    def replace(self, tenant: str, comment_id: str, file_uid: str, thread_id: str,
                items: List[Tuple[str, Sequence[float]]]) -> None:
        """Replace all chunks for a comment (idempotent re-index)."""
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM comment_chunks WHERE comment_id = %s", (comment_id,))
            for text, emb in items:
                cur.execute(
                    "INSERT INTO comment_chunks (comment_id, file_uid, thread_id, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector)",
                    (comment_id, file_uid, thread_id, text, _vec_literal(emb)))
            conn.commit()

    def remove(self, tenant: str, comment_id: str) -> None:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM comment_chunks WHERE comment_id = %s", (comment_id,))
            conn.commit()

    def ann_search(self, tenant: str, query_embedding: Sequence[float], k: int) -> List[dict]:
        """Nearest chunks by cosine distance (no ACL filter — the caller applies it)."""
        ql = _vec_literal(query_embedding)
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT comment_id, file_uid, thread_id, text, "
                "       embedding <=> %s::vector AS distance "
                "FROM comment_chunks WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (ql, ql, k))
            return [{"comment_id": r[0], "file_uid": r[1], "thread_id": r[2],
                     "text": r[3], "score": 1.0 - float(r[4])} for r in cur.fetchall()]

    def fts_search(self, tenant: str, query: str, fetch: int) -> List[dict]:
        """Full-text + fuzzy match over comment chunks (no ACL filter here)."""
        sql = (
            "WITH q AS (SELECT websearch_to_tsquery('english', %(q)s) AS tsq) "
            "SELECT comment_id, file_uid, thread_id, text, "
            "       ts_rank(fts, q.tsq) + similarity(text, %(q)s) AS score "
            "FROM comment_chunks, q "
            "WHERE fts @@ q.tsq OR text %% %(q)s "
            "ORDER BY score DESC LIMIT %(fetch)s"
        )
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(sql, {"q": query, "fetch": fetch})
            return [{"comment_id": r[0], "file_uid": r[1], "thread_id": r[2],
                     "text": r[3], "score": float(r[4])} for r in cur.fetchall()]

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

"""Index comment text for search + RAG (SPECIFICATION §6).

On comment create/edit the plaintext ``body_text`` is chunked, embedded, and stored
in ``comment_chunks`` tagged with the anchor ``file_uid``; on delete/redact its chunks
are removed so masked content can never resurface. Indexing is **best-effort** — the
handlers call it after the authoritative write and never let an embedding/store failure
fail the request (see threads_api).
"""
from __future__ import annotations

import logging

from .chunking import chunk_text

log = logging.getLogger("discussion.indexing")


class Indexer:
    def __init__(self, embedder, chunk_store):
        self.embedder = embedder
        self.chunks = chunk_store

    def index_comment(self, tenant: str, *, comment_id: str, file_uid: str, thread_id: str,
                      text: str) -> int:
        """Chunk + embed + store. Returns the chunk count (0 clears the comment)."""
        pieces = chunk_text(text)
        if not pieces:
            self.chunks.remove(tenant, comment_id)
            return 0
        vectors = self.embedder.embed(pieces)
        items = list(zip(pieces, vectors))
        self.chunks.replace(tenant, comment_id, file_uid, thread_id, items)
        return len(items)

    def remove_comment(self, tenant: str, comment_id: str) -> None:
        self.chunks.remove(tenant, comment_id)

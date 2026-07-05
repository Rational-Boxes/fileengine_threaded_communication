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

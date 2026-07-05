"""Discussion operations for the MCP door (SPECIFICATION §9 / M5).

Thin, transport-agnostic operations an agent invokes **as its own resolved
identity** — the same permission model as the REST surface (§5). The mention/
reviewer safety primitive (§5.1) is the shared ``targets.validate_targets``, so
the security invariant is single-sourced; the surrounding orchestration mirrors
the REST handlers (a later consolidation onto one service layer is noted in the
roadmap DRY checkpoint — rule of three).

Each method raises ``ToolError`` (message surfaced to the MCP host) on a
permission/validation failure; otherwise returns plain JSON-serializable dicts.
Live fan-out (§10h) is a web-socket concern and intentionally omitted here.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from .ldap_auth import Identity
from .markdown_text import to_plaintext
from .targets import validate_targets

log = logging.getLogger("discussion.mcp_tools")


class ToolError(Exception):
    """A tool-level failure whose message is shown to the calling agent."""


@dataclass
class Components:
    permissions: object
    store: object
    directory: object
    notifications: object
    events: object
    reviews: object
    indexer: object


class Toolset:
    def __init__(self, components: Components):
        self.c = components

    # -- helpers ------------------------------------------------------------
    def _require_read(self, ident: Identity, file_uid: str) -> None:
        if not self.c.permissions.can_read(ident, file_uid):
            raise ToolError("permission denied")

    def _index(self, tenant, comment_id, file_uid, thread_id, text) -> None:
        try:
            self.c.indexer.index_comment(tenant, comment_id=comment_id, file_uid=file_uid,
                                         thread_id=thread_id, text=text)
        except Exception:
            log.warning("mcp index failed", exc_info=True)

    # -- reads --------------------------------------------------------------
    def list_threads(self, ident: Identity, file_uid: str, status: str | None = None) -> list[dict]:
        self._require_read(ident, file_uid)
        return self.c.store.list_threads(ident.tenant, file_uid, status=status)

    def get_thread(self, ident: Identity, thread_id: str) -> dict:
        meta = self.c.store.thread_meta(ident.tenant, thread_id)
        if meta is None:
            raise ToolError("thread not found")
        self._require_read(ident, meta["file_uid"])
        thread = self.c.store.get_thread(ident.tenant, thread_id)
        if thread is None:
            raise ToolError("thread not found")
        return thread

    # -- writes -------------------------------------------------------------
    def open_thread(self, ident: Identity, file_uid: str, *, body: str, title: str = "",
                    version: str = "") -> dict:
        self._require_read(ident, file_uid)
        text = (body or "").strip()
        if not text:
            raise ToolError("comment body is required")
        thread = self.c.store.create_thread(
            ident.tenant, file_uid=file_uid, version=version, title=title.strip(),
            body=text, body_text=to_plaintext(text), opened_by=ident.user)
        if thread.get("comments"):
            self._index(ident.tenant, thread["comments"][0]["id"], file_uid, thread["id"], to_plaintext(text))
        self.c.events.publish("thread.opened", tenant=ident.tenant, file_uid=file_uid,
                              actor=ident.user, thread_id=thread["id"])
        self.c.events.publish("comment.created", tenant=ident.tenant, file_uid=file_uid,
                              actor=ident.user, thread_id=thread["id"])
        return thread

    def post_comment(self, ident: Identity, thread_id: str, *, body: str,
                     mentions: list[str] | None = None) -> dict:
        meta = self.c.store.thread_meta(ident.tenant, thread_id)
        if meta is None:
            raise ToolError("thread not found")
        file_uid = meta["file_uid"]
        self._require_read(ident, file_uid)
        text = (body or "").strip()
        if not text:
            raise ToolError("comment body is required")

        valid = []
        if mentions:
            valid, invalid = validate_targets(self.c.directory, self.c.permissions, file_uid, mentions)
            if invalid:
                raise ToolError("some mentioned users cannot access this file: " + ", ".join(invalid))

        comment = self.c.store.add_comment(ident.tenant, thread_id, author=ident.user,
                                           body=text, body_text=to_plaintext(text))
        self._index(ident.tenant, comment["id"], file_uid, thread_id, to_plaintext(text))

        mentioned = set()
        for _id, principal in valid:
            uid = principal.user
            mentioned.add(uid)
            self.c.store.add_mention(ident.tenant, comment_id=comment["id"], thread_id=thread_id,
                                     target_user=uid)
            self.c.notifications.add(ident.tenant, user_id=uid, kind="mention", file_uid=file_uid,
                                     actor=ident.user, thread_id=thread_id)
            self.c.events.publish("mention.created", tenant=ident.tenant, file_uid=file_uid,
                                  actor=ident.user, thread_id=thread_id, target_user=uid)
        for part in self.c.store.thread_participants(ident.tenant, thread_id):
            if part != ident.user and part not in mentioned:
                self.c.notifications.add(ident.tenant, user_id=part, kind="reply", file_uid=file_uid,
                                         actor=ident.user, thread_id=thread_id)
        self.c.events.publish("comment.created", tenant=ident.tenant, file_uid=file_uid,
                              actor=ident.user, thread_id=thread_id)
        return comment

    def resolve_thread(self, ident: Identity, thread_id: str, *,
                       resolved_version: str | None = None) -> dict:
        meta = self.c.store.thread_meta(ident.tenant, thread_id)
        if meta is None:
            raise ToolError("thread not found")
        allowed = meta["opened_by"] == ident.user or self.c.permissions.can_write(ident, meta["file_uid"])
        if not allowed:
            raise ToolError("permission denied")
        thread = self.c.store.set_thread_status(ident.tenant, thread_id, status="resolved",
                                                resolved_by=ident.user, resolved_version=resolved_version)
        if thread is None:
            raise ToolError("thread not found")
        for part in self.c.store.thread_participants(ident.tenant, thread_id):
            if part != ident.user:
                self.c.notifications.add(ident.tenant, user_id=part, kind="thread_resolved",
                                         file_uid=meta["file_uid"], actor=ident.user, thread_id=thread_id)
        self.c.events.publish("thread.resolved", tenant=ident.tenant, file_uid=meta["file_uid"],
                              actor=ident.user, thread_id=thread_id)
        return thread

    def raise_review(self, ident: Identity, file_uid: str, reviewers: list[str], *,
                     version: str = "", thread_id: str | None = None) -> list[dict]:
        self._require_read(ident, file_uid)
        if not reviewers:
            raise ToolError("at least one reviewer is required")
        valid, invalid = validate_targets(self.c.directory, self.c.permissions, file_uid, reviewers)
        if invalid:
            raise ToolError("some reviewers cannot access this file: " + ", ".join(invalid))
        reviews = self.c.reviews.create(ident.tenant, file_uid=file_uid, version=version,
                                        thread_id=thread_id, requester=ident.user,
                                        reviewers=[p.user for _id, p in valid])
        for r in reviews:
            self.c.notifications.add(ident.tenant, user_id=r["reviewer"], kind="review_requested",
                                     file_uid=file_uid, actor=ident.user, thread_id=thread_id,
                                     review_id=r["id"])
            self.c.events.publish("review.requested", tenant=ident.tenant, file_uid=file_uid,
                                  actor=ident.user, review_id=r["id"], target_user=r["reviewer"])
        return reviews


def build_components(config) -> Components:
    """Real components wired from config (for the MCP server process)."""
    from .directory import Directory
    from .embeddings import build_embedder
    from .events import EventPublisher
    from .indexing import Indexer
    from .notifications import NotificationStore
    from .permissions import Permissions
    from .reviews_store import ReviewStore
    from .store import ThreadStore
    from .vectorstore import ChunkStore
    return Components(
        permissions=Permissions(config),
        store=ThreadStore(config),
        directory=Directory(config),
        notifications=NotificationStore(config),
        events=EventPublisher(config),
        reviews=ReviewStore(config),
        indexer=Indexer(build_embedder(config), ChunkStore(config)),
    )

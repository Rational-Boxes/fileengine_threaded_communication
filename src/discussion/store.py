"""Postgres persistence for threads & comments (SPECIFICATION §4 / M1).

A thin repository over the per-tenant schema (see schema.py / db.py). Permission
decisions live in permissions.py — this layer only reads/writes rows. All access
is per-tenant via ``connect_for_tenant`` (schema-scoped ``search_path``).

Timestamps are returned as ISO-8601 strings so handlers can serialize directly.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Optional

from .config import Config
from .db import connect_for_tenant


def _uid() -> str:
    return uuid.uuid4().hex


def _val(v):
    return v.isoformat() if isinstance(v, _dt.datetime) else v


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [{k: _val(v) for k, v in zip(cols, row)} for row in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    row = cur.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cur.description]
    return {k: _val(v) for k, v in zip(cols, row)}


_THREAD_COLS = ("id, file_uid, version, title, status, resolved_by, resolved_version, "
                "opened_by, created_at, updated_at, anchor_stale")
# A soft-deleted comment is tombstoned in-place (body blanked); a redacted one is
# masked. Callers see the flags and the (empty) body, never the original text.
def _comment_select(prefix: str = "") -> str:
    """The comment projection, optionally column-prefixed (e.g. ``c.`` for joins).
    A soft-deleted comment is tombstoned (body blanked); a redacted one is masked —
    callers see the flags and an empty body, never the original text."""
    p = prefix
    return (
        f"{p}id, {p}thread_id, {p}parent_comment_id, {p}author, "
        f"CASE WHEN {p}deleted THEN '' WHEN {p}redacted THEN '' ELSE {p}body END AS body, "
        f"{p}created_at, {p}edited_at, {p}deleted, {p}redacted, {p}redacted_by, {p}redacted_reason"
    )


_COMMENT_SELECT = _comment_select()


class ThreadStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, *, readonly: bool = False, provision: bool = False):
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    # -- threads -------------------------------------------------------------
    def create_thread(self, tenant: str, *, file_uid: str, version: str, title: str,
                      body: str, body_text: str, opened_by: str) -> dict:
        """Open a thread and its first comment (the opening body). Returns the thread."""
        tid, cid = _uid(), _uid()
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO threads (id, file_uid, version, title, opened_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tid, file_uid, version, title, opened_by))
            cur.execute(
                "INSERT INTO comments (id, thread_id, author, body, body_text) "
                "VALUES (%s, %s, %s, %s, %s)",
                (cid, tid, opened_by, body, body_text))
            conn.commit()
        return self.get_thread(tenant, tid)

    def list_threads(self, tenant: str, file_uid: str, *, status: Optional[str] = None) -> list[dict]:
        sql = f"SELECT {_THREAD_COLS} FROM threads WHERE file_uid = %s"
        params: list = [file_uid]
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            threads = _rows(cur)
            # Embed each thread's comment tree so the panel renders comments on
            # (re)load — not only during the live session. One batched query.
            if threads:
                ids = [t["id"] for t in threads]
                cur.execute(
                    f"SELECT {_COMMENT_SELECT} FROM comments WHERE thread_id = ANY(%s) "
                    f"ORDER BY created_at", (ids,))
                by_thread: dict[str, list] = {}
                for c in _rows(cur):
                    by_thread.setdefault(c["thread_id"], []).append(c)
                for t in threads:
                    t["comments"] = by_thread.get(t["id"], [])
            return threads

    def thread_meta(self, tenant: str, thread_id: str) -> Optional[dict]:
        """Just the fields needed for permission/authorization decisions."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT id, file_uid, opened_by, status FROM threads WHERE id = %s",
                        (thread_id,))
            return _one(cur)

    def get_thread(self, tenant: str, thread_id: str) -> Optional[dict]:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_THREAD_COLS} FROM threads WHERE id = %s", (thread_id,))
            thread = _one(cur)
            if thread is None:
                return None
            cur.execute(
                f"SELECT {_COMMENT_SELECT} FROM comments WHERE thread_id = %s ORDER BY created_at",
                (thread_id,))
            thread["comments"] = _rows(cur)
        return thread

    def set_thread_status(self, tenant: str, thread_id: str, *, status: str,
                          resolved_by: Optional[str], resolved_version: Optional[str]) -> Optional[dict]:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE threads SET status = %s, resolved_by = %s, resolved_version = %s, "
                "updated_at = now() WHERE id = %s",
                (status, resolved_by, resolved_version, thread_id))
            changed = cur.rowcount
            conn.commit()
        return self.get_thread(tenant, thread_id) if changed else None

    # -- comments ------------------------------------------------------------
    def add_comment(self, tenant: str, thread_id: str, *, author: str, body: str,
                    body_text: str, parent_comment_id: Optional[str] = None) -> dict:
        cid = _uid()
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO comments (id, thread_id, parent_comment_id, author, body, body_text) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (cid, thread_id, parent_comment_id, author, body, body_text))
            cur.execute("UPDATE threads SET updated_at = now() WHERE id = %s", (thread_id,))
            conn.commit()
        return self.get_comment(tenant, cid)

    def comment_parent_thread(self, tenant: str, comment_id: str) -> Optional[str]:
        """The thread_id a (parent) comment belongs to — for validating a reply target."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT thread_id FROM comments WHERE id = %s", (comment_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def list_revisions(self, tenant: str, comment_id: str) -> list[dict]:
        """Prior versions of an edited comment (newest first). Empty if never edited."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT body, edited_at FROM comment_revisions WHERE comment_id = %s "
                "ORDER BY edited_at DESC", (comment_id,))
            return [{"body": r[0], "edited_at": _val(r[1])} for r in cur.fetchall()]

    def get_comment(self, tenant: str, comment_id: str) -> Optional[dict]:
        """A comment plus its thread's ``file_uid`` (the ACL key)."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_comment_select('c.')}, t.file_uid AS file_uid "
                "FROM comments c JOIN threads t ON t.id = c.thread_id WHERE c.id = %s",
                (comment_id,))
            return _one(cur)

    def edit_comment(self, tenant: str, comment_id: str, *, body: str, body_text: str) -> Optional[dict]:
        """Edit a comment, snapshotting the prior body into comment_revisions."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT body, deleted, redacted FROM comments WHERE id = %s", (comment_id,))
            row = cur.fetchone()
            if row is None or row[1] or row[2]:      # missing / deleted / redacted → no edit
                return None
            cur.execute("INSERT INTO comment_revisions (comment_id, body) VALUES (%s, %s)",
                        (comment_id, row[0]))
            cur.execute(
                "UPDATE comments SET body = %s, body_text = %s, edited_at = now() WHERE id = %s",
                (body, body_text, comment_id))
            conn.commit()
        return self.get_comment(tenant, comment_id)

    def soft_delete_comment(self, tenant: str, comment_id: str) -> bool:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE comments SET deleted = true, body_text = '', edited_at = now() "
                "WHERE id = %s AND deleted = false",
                (comment_id,))
            changed = cur.rowcount
            conn.commit()
        return bool(changed)

    # -- mentions / participants / moderation (M2) ---------------------------
    def add_mention(self, tenant: str, *, comment_id: str, thread_id: str, target_user: str) -> None:
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO mentions (comment_id, thread_id, target_user) VALUES (%s, %s, %s)",
                (comment_id, thread_id, target_user))
            conn.commit()

    def thread_participants(self, tenant: str, thread_id: str) -> list[str]:
        """Distinct users involved in a thread (opener + comment authors) — the
        recipients of reply / resolution notifications."""
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT opened_by FROM threads WHERE id = %s "
                "UNION SELECT author FROM comments WHERE thread_id = %s",
                (thread_id, thread_id))
            return [r[0] for r in cur.fetchall() if r[0]]

    def mark_anchor_stale(self, tenant: str, file_uid: str, new_version: str) -> int:
        """Mark open threads pinned to a *prior* version stale when a newer version
        lands (§4). Threads tracking 'current' (version='') are unaffected."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE threads SET anchor_stale = true "
                "WHERE file_uid = %s AND version <> '' AND version <> %s AND status = 'open'",
                (file_uid, new_version or ""))
            n = cur.rowcount
            conn.commit()
        return n

    def mention_flags(self, tenant: str, user: str, file_uids: list[str]) -> dict[str, int]:
        """Open-thread @mention counts for ``user`` per file (attention flags, §10e)."""
        if not file_uids:
            return {}
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT t.file_uid, count(*) FROM mentions m JOIN threads t ON t.id = m.thread_id "
                "WHERE m.target_user = %s AND t.status = 'open' AND t.file_uid = ANY(%s) "
                "GROUP BY t.file_uid",
                (user, list(file_uids)))
            return {r[0]: int(r[1]) for r in cur.fetchall()}

    def redact_comment(self, tenant: str, comment_id: str, *, redacted_by: str,
                       reason: str) -> Optional[dict]:
        """Administrator redaction (§5b): move the original into ``redactions``
        (retained forever), mask the display, and de-index (drop comment_chunks).
        Returns the masked comment, or None if missing/already redacted."""
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT body, redacted FROM comments WHERE id = %s", (comment_id,))
            row = cur.fetchone()
            if row is None or row[1]:
                return None
            cur.execute(
                "INSERT INTO redactions (comment_id, original_body, redacted_by, reason) "
                "VALUES (%s, %s, %s, %s)",
                (comment_id, row[0], redacted_by, reason))
            cur.execute(
                "UPDATE comments SET redacted = true, redacted_by = %s, redacted_at = now(), "
                "redacted_reason = %s, body_text = '' WHERE id = %s",
                (redacted_by, reason, comment_id))
            # De-index so redacted content can't resurface via search/RAG (§6).
            cur.execute("DELETE FROM comment_chunks WHERE comment_id = %s", (comment_id,))
            conn.commit()
        return self.get_comment(tenant, comment_id)

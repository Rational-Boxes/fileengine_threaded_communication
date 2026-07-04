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
        f"{p}id, {p}thread_id, {p}author, "
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
            return _rows(cur)

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
                    body_text: str) -> dict:
        cid = _uid()
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO comments (id, thread_id, author, body, body_text) "
                "VALUES (%s, %s, %s, %s, %s)",
                (cid, thread_id, author, body, body_text))
            cur.execute("UPDATE threads SET updated_at = now() WHERE id = %s", (thread_id,))
            conn.commit()
        return self.get_comment(tenant, cid)

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

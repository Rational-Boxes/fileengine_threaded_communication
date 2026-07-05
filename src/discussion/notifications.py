"""The attention-feed backing store (SPECIFICATION §4 / §10a).

One row per thing wanting a user's attention (mention, reply, review lifecycle,
thread resolution). Shared by the threads and reviews surfaces. Reads (the dashboard
feed, M4) re-check READ per row; this layer only writes.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from .config import Config
from .db import connect_for_tenant

KINDS = ("mention", "reply", "review_requested", "review_acknowledged",
         "review_completed", "thread_resolved")


def _val(v):
    return v.isoformat() if isinstance(v, _dt.datetime) else v


class NotificationStore:
    def __init__(self, config: Config):
        self.config = config

    def add(self, tenant: str, *, user_id: str, kind: str, file_uid: str, actor: str,
            thread_id: Optional[str] = None, review_id: Optional[str] = None) -> None:
        """Record a notification. No self-notification (actor == recipient is skipped)."""
        if not user_id or user_id == actor or kind not in KINDS:
            return
        conn = connect_for_tenant(self.config, tenant, provision=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notifications (user_id, kind, file_uid, thread_id, review_id, actor) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, kind, file_uid, thread_id, review_id, actor))
            conn.commit()
        finally:
            conn.close()

    def list_for(self, tenant: str, user: str, *, limit: int = 50,
                 unread_only: bool = False) -> list[dict]:
        """The caller's attention feed (§10a). The handler re-checks READ per row."""
        sql = ("SELECT id, kind, file_uid, thread_id, review_id, actor, created_at, read_at "
               "FROM notifications WHERE user_id = %s")
        params: list = [user]
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        conn = connect_for_tenant(self.config, tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [c[0] for c in cur.description]
                return [{k: _val(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
        finally:
            conn.close()

    def mark_seen(self, tenant: str, user: str, notification_id: int) -> bool:
        """Mark one of the caller's notifications seen (state only; not a badge)."""
        conn = connect_for_tenant(self.config, tenant)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE notifications SET read_at = now() "
                    "WHERE id = %s AND user_id = %s AND read_at IS NULL",
                    (notification_id, user))
                changed = cur.rowcount
            conn.commit()
            return bool(changed)
        finally:
            conn.close()

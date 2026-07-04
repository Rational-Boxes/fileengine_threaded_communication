"""The attention-feed backing store (SPECIFICATION §4 / §10a).

One row per thing wanting a user's attention (mention, reply, review lifecycle,
thread resolution). Shared by the threads and reviews surfaces. Reads (the dashboard
feed, M4) re-check READ per row; this layer only writes.
"""
from __future__ import annotations

from typing import Optional

from .config import Config
from .db import connect_for_tenant

KINDS = ("mention", "reply", "review_requested", "review_acknowledged",
         "review_completed", "thread_resolved")


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

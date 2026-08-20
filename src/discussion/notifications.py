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

# NB `add()` DROPS an unknown kind silently (see below), and schema.py carries a
# matching CHECK constraint. A new kind must be added in both places or its
# notifications vanish with no log line.
KINDS = ("mention", "reply", "review_requested", "review_acknowledged",
         "review_completed", "review_approved", "review_rejected", "thread_resolved",
         # Share links (spec §10.6). The feed is one place a user looks; share
         # events join it rather than growing a parallel one.
         "share_drop_received", "share_link_dead", "share_otp_send_failed",
         "share_first_redemption", "share_link_locked")

# Which system a kind came from, mapped at WRITE time and returned by the API.
#
# Deriving this in the SPA by string-matching the kind would put the mapping in
# the one place that does not know when a new kind is added here. A source the
# SPA does not recognise renders under its own raw heading rather than
# disappearing, so an unmapped kind degrades visibly.
SOURCES = {
    "mention": "comments", "reply": "comments", "thread_resolved": "comments",
    "review_requested": "reviews", "review_acknowledged": "reviews",
    "review_completed": "reviews", "review_approved": "reviews",
    "review_rejected": "reviews",
    "share_drop_received": "sharing", "share_link_dead": "sharing",
    "share_otp_send_failed": "sharing", "share_first_redemption": "sharing",
    "share_link_locked": "sharing",
}

# The actor recorded for share events that have no external human behind them
# (a link went dead; an OTP send failed). It must NOT be the creator: `add()`
# suppresses self-notification, so a creator's own links could never notify
# them. Reserved, and shaped so it cannot collide with an LDAP username.
SYSTEM_ACTOR = "system:share"


def source_for(kind: str) -> str:
    """`SOURCES` with a safe default — an unmapped kind gets its own division
    rather than being silently filed under someone else's."""
    return SOURCES.get(kind, "other")


def _val(v):
    return v.isoformat() if isinstance(v, _dt.datetime) else v


class NotificationStore:
    def __init__(self, config: Config):
        self.config = config

    def add(self, tenant: str, *, user_id: str, kind: str, file_uid: str, actor: str,
            thread_id: Optional[str] = None, review_id: Optional[str] = None,
            share_link_uid: Optional[str] = None,
            detail_text: Optional[str] = None) -> None:
        """Record a notification. No self-notification (actor == recipient is skipped).

        The self-notification rule is a trap for share items, which are addressed
        TO the creator: the actor must be the external redeemer or `SYSTEM_ACTOR`,
        never `created_by`, or a creator's own links can never notify them.

        `detail_text` makes a row self-contained, so the feed can render it
        without resolving `file_uid` — see the READ re-check note in
        dashboard_api.
        """
        if not user_id or user_id == actor or kind not in KINDS:
            return
        conn = connect_for_tenant(self.config, tenant, provision=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notifications (user_id, kind, file_uid, thread_id, "
                    "review_id, actor, share_link_uid, detail_text) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (user_id, kind, file_uid, thread_id, review_id, actor,
                     share_link_uid, detail_text))
            conn.commit()
        finally:
            conn.close()

    def list_for(self, tenant: str, user: str, *, limit: int = 50,
                 unread_only: bool = False, since: Optional[str] = None) -> list[dict]:
        """The caller's attention feed (§10a) / digest window (§11b). The handler
        re-checks READ per row."""
        sql = ("SELECT id, kind, file_uid, thread_id, review_id, actor, created_at, "
               "read_at, share_link_uid, detail_text "
               "FROM notifications WHERE user_id = %s")
        params: list = [user]
        if unread_only:
            sql += " AND read_at IS NULL"
        if since:
            sql += " AND created_at > %s"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        conn = connect_for_tenant(self.config, tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [c[0] for c in cur.description]
                out = []
                for row in cur.fetchall():
                    item = {k: _val(v) for k, v in zip(cols, row)}
                    # Mapped here rather than stored: a stored value would be
                    # frozen at write time and could not be corrected without a
                    # migration, and the mapping is not user data.
                    item["source"] = source_for(item["kind"])
                    out.append(item)
                return out
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

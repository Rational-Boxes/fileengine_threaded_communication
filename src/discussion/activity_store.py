"""Durable projection of core file events (SPECIFICATION §4 §8 / M4a).

The event consumer records file.created/updated/restored here so the dashboard
activity feed (§10a) and the email digest (§11) can query "activity since T". Reads
are ACL-filtered per viewer by the handler (this layer only reads/writes rows).
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from .config import Config
from .db import connect_for_tenant


def _val(v):
    return v.isoformat() if isinstance(v, _dt.datetime) else v


class ActivityStore:
    def __init__(self, config: Config):
        self.config = config

    def record(self, tenant: str, *, event_type: str, file_uid: str, version: str = "",
               name: str = "", path: str = "", actor: str = "") -> None:
        conn = connect_for_tenant(self.config, tenant, provision=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO document_activity (file_uid, event_type, version, name, path, actor) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (file_uid, event_type, version, name, path, actor))
            conn.commit()
        finally:
            conn.close()

    def delete_for_file(self, tenant: str, file_uid: str) -> int:
        """Drop all activity rows for a file — called when the core reports the file
        deleted, so neither the dashboard feed (§10a) nor the digest (§11) keeps
        surfacing a trashed document. A later ``file.restored`` re-records the item,
        so restoring recovers it. Returns the number of rows removed.
        """
        if not file_uid:
            return 0
        conn = connect_for_tenant(self.config, tenant, provision=True)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM document_activity WHERE file_uid = %s", (file_uid,))
                removed = cur.rowcount
            conn.commit()
            return removed
        finally:
            conn.close()

    def recent(self, tenant: str, *, limit: int = 50, since: Optional[str] = None) -> list[dict]:
        sql = "SELECT id, file_uid, event_type, version, name, path, actor, ts FROM document_activity"
        params: list = []
        if since:
            sql += " WHERE ts > %s"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT %s"
        params.append(limit)
        conn = connect_for_tenant(self.config, tenant, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [c[0] for c in cur.description]
                return [{k: _val(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
        finally:
            conn.close()

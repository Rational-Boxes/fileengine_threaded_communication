"""Review-request persistence (SPECIFICATION §4 / §9 — the review state machine).

A requester asks one or more reviewers to review an anchor; one row per reviewer,
tracked ``requested → acknowledged → completed`` (or ``declined``). Timestamps are
returned as ISO-8601 strings.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Optional

from .config import Config
from .db import connect_for_tenant

_COLS = ("id, file_uid, version, thread_id, requester, reviewer, status, outcome, "
         "created_at, acknowledged_at, completed_at")


def _val(v):
    return v.isoformat() if isinstance(v, _dt.datetime) else v


def _one(cur) -> Optional[dict]:
    row = cur.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cur.description]
    return {k: _val(v) for k, v in zip(cols, row)}


def _rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [{k: _val(v) for k, v in zip(cols, row)} for row in cur.fetchall()]


class ReviewStore:
    def __init__(self, config: Config):
        self.config = config

    def _conn(self, tenant: str, *, readonly: bool = False, provision: bool = False):
        return connect_for_tenant(self.config, tenant, provision=provision, readonly=readonly)

    def create(self, tenant: str, *, file_uid: str, version: str, thread_id: Optional[str],
               requester: str, reviewers: list[str]) -> list[dict]:
        """One review row per reviewer (all in one transaction). Returns them."""
        ids = []
        with self._conn(tenant, provision=True) as conn, conn.cursor() as cur:
            for reviewer in reviewers:
                rid = uuid.uuid4().hex
                cur.execute(
                    "INSERT INTO review_requests (id, file_uid, version, thread_id, requester, reviewer) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (rid, file_uid, version, thread_id, requester, reviewer))
                ids.append(rid)
            conn.commit()
        return [self.get(tenant, rid) for rid in ids]

    def get(self, tenant: str, review_id: str) -> Optional[dict]:
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM review_requests WHERE id = %s", (review_id,))
            return _one(cur)

    def set_status(self, tenant: str, review_id: str, *, status: str,
                   outcome: Optional[str] = None) -> Optional[dict]:
        """Advance a review. Stamps acknowledged_at / completed_at from the status."""
        ts_col = {"acknowledged": "acknowledged_at", "completed": "completed_at"}.get(status)
        sets = ["status = %s", "outcome = COALESCE(%s, outcome)"]
        params: list = [status, outcome]
        if ts_col:
            sets.append(f"{ts_col} = now()")
        with self._conn(tenant) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE review_requests SET {', '.join(sets)} WHERE id = %s",
                        (*params, review_id))
            changed = cur.rowcount
            conn.commit()
        return self.get(tenant, review_id) if changed else None

    def review_flags(self, tenant: str, user: str, file_uids: list[str]) -> dict[str, int]:
        """Pending-review counts (``requested``/``acknowledged``) where ``user`` is the
        reviewer, per file (attention flags, §10e)."""
        if not file_uids:
            return {}
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT file_uid, count(*) FROM review_requests "
                "WHERE reviewer = %s AND status IN ('requested','acknowledged') "
                "AND file_uid = ANY(%s) GROUP BY file_uid",
                (user, list(file_uids)))
            return {r[0]: int(r[1]) for r in cur.fetchall()}

    def list_for(self, tenant: str, user: str, *, role: str = "both",
                 status: Optional[str] = None) -> list[dict]:
        """Reviews where ``user`` is the reviewer and/or the requester."""
        clauses = []
        if role in ("reviewer", "both"):
            clauses.append("reviewer = %(u)s")
        if role in ("requester", "both"):
            clauses.append("requester = %(u)s")
        where = "(" + " OR ".join(clauses) + ")"
        params = {"u": user}
        if status:
            where += " AND status = %(s)s"
            params["s"] = status
        with self._conn(tenant, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM review_requests WHERE {where} "
                        "ORDER BY created_at DESC", params)
            return _rows(cur)

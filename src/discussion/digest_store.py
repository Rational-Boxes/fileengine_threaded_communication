"""Digest subscriptions + delivery ledger (SPECIFICATION §4 §11 / M6).

Per-user subscription config and the idempotency ledger (one row per user per
period, ``UNIQUE(user_id, period_key)``). Also enumerates tenant schemas so the
hourly sender can sweep every tenant.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Optional

from .config import Config
from .db import connect, connect_for_tenant


def _val(v):
    return v.isoformat() if isinstance(v, _dt.datetime) else v


class DigestStore:
    def __init__(self, config: Config):
        self.config = config

    def _default(self, user: str) -> dict:
        return {"user_id": user, "cadence": self.config.digest_default_cadence,
                "send_hour_local": 8, "send_dow": 1, "timezone": "UTC",
                "scope": {}, "ai_summary": False, "quiet_if_empty": True}

    def get(self, tenant: str, user: str) -> dict:
        conn = connect_for_tenant(self.config, tenant, provision=True, readonly=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, cadence, send_hour_local, send_dow, timezone, scope, "
                    "ai_summary, quiet_if_empty FROM digest_subscriptions WHERE user_id = %s",
                    (user,))
                row = cur.fetchone()
                if row is None:
                    return self._default(user)
                cols = [c[0] for c in cur.description]
                return {k: _val(v) for k, v in zip(cols, row)}
        finally:
            conn.close()

    def upsert(self, tenant: str, user: str, *, cadence: str, send_hour_local: int,
               send_dow: int, timezone: str, scope: dict, ai_summary: bool,
               quiet_if_empty: bool) -> dict:
        conn = connect_for_tenant(self.config, tenant, provision=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO digest_subscriptions "
                    "(user_id, cadence, send_hour_local, send_dow, timezone, scope, ai_summary, "
                    " quiet_if_empty, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                    "ON CONFLICT (user_id) DO UPDATE SET cadence=EXCLUDED.cadence, "
                    "send_hour_local=EXCLUDED.send_hour_local, send_dow=EXCLUDED.send_dow, "
                    "timezone=EXCLUDED.timezone, scope=EXCLUDED.scope, "
                    "ai_summary=EXCLUDED.ai_summary, quiet_if_empty=EXCLUDED.quiet_if_empty, "
                    "updated_at=now()",
                    (user, cadence, send_hour_local, send_dow, timezone, json.dumps(scope),
                     ai_summary, quiet_if_empty))
            conn.commit()
        finally:
            conn.close()
        return self.get(tenant, user)

    def list_enabled(self, tenant: str, *, limit: int = 1000) -> list[dict]:
        conn = connect_for_tenant(self.config, tenant, provision=True, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, cadence, send_hour_local, send_dow, timezone, scope, "
                    "ai_summary, quiet_if_empty FROM digest_subscriptions "
                    "WHERE cadence <> 'off' ORDER BY user_id LIMIT %s", (limit,))
                cols = [c[0] for c in cur.description]
                return [{k: _val(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
        finally:
            conn.close()

    def already_delivered(self, tenant: str, user: str, period_key: str) -> bool:
        conn = connect_for_tenant(self.config, tenant, provision=True, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM digest_deliveries WHERE user_id = %s AND period_key = %s",
                            (user, period_key))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def record_delivery(self, tenant: str, user: str, period_key: str, *, status: str,
                        item_count: int = 0, error: Optional[str] = None) -> bool:
        """Insert the delivery row. Returns False if it already existed (the UNIQUE
        guard — another run already handled this period)."""
        conn = connect_for_tenant(self.config, tenant, provision=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO digest_deliveries (user_id, period_key, status, item_count, error) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, period_key) DO NOTHING",
                    (user, period_key, status, item_count, error))
                inserted = cur.rowcount
            conn.commit()
            return bool(inserted)
        finally:
            conn.close()

    def list_tenants(self) -> list[str]:
        """Tenant identifiers derived from the ``tenant_*`` schemas present."""
        conn = connect(self.config, readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT schema_name FROM information_schema.schemata "
                            "WHERE schema_name LIKE 'tenant\\_%'")
                return [r[0][len("tenant_"):] for r in cur.fetchall()]
        finally:
            conn.close()

    def try_lock(self) -> Optional[object]:
        """Acquire a process-wide advisory lock so overlapping cron ticks don't
        double-send. Returns an open connection holding the lock (close to release),
        or None if another run holds it."""
        conn = connect(self.config)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (0x0D15C55,))  # "DISCSS"
                got = cur.fetchone()[0]
            if not got:
                conn.close()
                return None
            return conn
        except Exception:
            conn.close()
            return None

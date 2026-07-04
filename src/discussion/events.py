"""Emit discussion events to Redis for the Phase 3 digest / cross-channel delivery.

The service publishes its own events to the ``discussion:events`` stream (§8), which
the digest sender (M6) and future chat bridges consume. Publishing is **best-effort**:
a Redis outage must never fail a comment/review write, so ``publish`` swallows errors
(the DB + notifications remain the source of truth). Envelope mirrors the core's
schema (event_id, type, tenant, file_uid, actor, ts, schema) plus optional
thread_id / review_id / target_user.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import uuid
from typing import Optional

log = logging.getLogger("discussion.events")

_SCHEMA = 1
_MAXLEN = 100_000


def _now_ts() -> str:
    # YYYYMMDD_HHMMSS.mmm — same shape as the core's event timestamps.
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S.%f")[:-3]


def make_event(etype: str, *, tenant: str, file_uid: str = "", actor: str = "",
               thread_id: Optional[str] = None, review_id: Optional[str] = None,
               target_user: Optional[str] = None) -> dict:
    evt = {
        "event_id": uuid.uuid4().hex,
        "type": etype,
        "tenant": tenant or "default",
        "file_uid": file_uid or "",
        "actor": actor or "",
        "ts": _now_ts(),
        "schema": _SCHEMA,
    }
    if thread_id is not None:
        evt["thread_id"] = thread_id
    if review_id is not None:
        evt["review_id"] = review_id
    if target_user is not None:
        evt["target_user"] = target_user
    return evt


class EventPublisher:
    def __init__(self, config):
        self.config = config
        self.stream = config.emits_stream
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis
            self._redis = redis.Redis(
                host=self.config.redis_host, port=self.config.redis_port,
                password=self.config.redis_password or None, db=self.config.redis_db)
        return self._redis

    def publish(self, etype: str, **fields) -> dict:
        """Build + XADD an event. Best-effort — never raises into a request."""
        evt = make_event(etype, **fields)
        try:
            self._client().xadd(self.stream, {"payload": json.dumps(evt)},
                                maxlen=_MAXLEN, approximate=True)
        except Exception:
            log.warning("event publish failed (%s) — continuing", etype, exc_info=True)
        return evt

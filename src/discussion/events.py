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

# Collaboration events the folder_actions service recognizes (SPEC §3.1 / §4.2).
# These are *dual-published*: in addition to the private ``discussion:events`` side
# stream, they are also XADD'd onto the shared recognized stream
# ``fileengine:events`` (config.events_stream, where the core also emits) so
# folder_actions' single-stream consumer sees them. Other internal event types
# stay on ``discussion:events`` only.
PROMOTED_TYPES = frozenset({
    "review.approved", "review.rejected",
    "thread.opened", "comment.created", "mention.created", "thread.resolved",
})


def _now_ts() -> str:
    # YYYYMMDD_HHMMSS.mmm — same shape as the core's event timestamps.
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S.%f")[:-3]


def make_event(etype: str, *, tenant: str, file_uid: str = "", actor: str = "",
               thread_id: Optional[str] = None, review_id: Optional[str] = None,
               target_user: Optional[str] = None, anchor: Optional[dict] = None) -> dict:
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
    # V2 (§5.4): a 3D/region anchor rides the envelope so digest / cross-channel
    # bridges know an opened thread is an anchored annotation, not a bare comment.
    if anchor is not None:
        evt["anchor"] = anchor
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

    def _xadd(self, stream: str, evt: dict, etype: str) -> None:
        """Best-effort XADD of one event to one stream — never raises."""
        try:
            self._client().xadd(stream, {"payload": json.dumps(evt)},
                                maxlen=_MAXLEN, approximate=True)
        except Exception:
            log.warning("event publish failed (%s -> %s) — continuing", etype, stream,
                        exc_info=True)

    def publish(self, etype: str, **fields) -> dict:
        """Build + XADD an event. Best-effort — never raises into a request.

        Always written to the private ``discussion:events`` side stream (the digest /
        cross-channel consumers). Recognized collaboration types (``PROMOTED_TYPES``)
        are *also* written to the shared ``fileengine:events`` recognized stream so
        the folder_actions consumer sees them (§4.2). A failure on either write is
        swallowed independently so one stream being down never blocks the other."""
        evt = make_event(etype, **fields)
        self._xadd(self.stream, evt, etype)
        if etype in PROMOTED_TYPES:
            shared = getattr(self.config, "events_stream", "")
            if shared and shared != self.stream:
                self._xadd(shared, evt, etype)
        return evt

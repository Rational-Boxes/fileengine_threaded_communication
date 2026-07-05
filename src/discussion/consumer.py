"""Consume the core's event stream (SPECIFICATION §8 / M4a).

A background worker (separate process, like CSAI's ingest) that reads
``fileengine:events`` and:
  - file.created/updated/restored → record document_activity (§4/§10a);
    file.updated also marks prior-version threads anchor_stale (§4).
  - file.deleted       → prune the file's document_activity (drop it from the feed).
  - acl.changed        → evict the READ permission cache for that resource (§5).
  - role.assigned / role.member_removed → evict for that member.
  - role.deleted       → evict the whole tenant.
Rendition events (our own kind of output-ish churn) are ignored.

At-least-once delivery via a consumer group (XREADGROUP + XACK). ``handle`` is pure
and unit-tested; ``run_forever`` is the loop. Launch: ``discuss-consumer``.
"""
from __future__ import annotations

import json
import logging
from typing import List, Tuple

log = logging.getLogger("discussion.consumer")

_ACTIVITY = {"file.created": "created", "file.updated": "updated", "file.restored": "restored"}
Entry = Tuple[str, dict]


class EventConsumer:
    def __init__(self, config, *, activity, store, permissions):
        self.config = config
        self.activity = activity
        self.store = store
        self.permissions = permissions

    def handle(self, event: dict) -> None:
        if event.get("is_rendition"):
            return
        etype = event.get("type", "")
        tenant = event.get("tenant") or "default"
        uid = event.get("file_uid", "")

        if etype in _ACTIVITY:
            if uid:
                self.activity.record(tenant, event_type=_ACTIVITY[etype], file_uid=uid,
                                     version=event.get("version", ""), name=event.get("name", ""),
                                     path=event.get("path", ""), actor=event.get("actor", ""))
            if etype == "file.updated" and uid:
                self.store.mark_anchor_stale(tenant, uid, event.get("version", ""))
        elif etype == "file.deleted" and uid:
            # A soft-deleted file must drop out of the activity feed/digest. Prune its
            # rows at the source so every reader is consistent; file.restored re-records.
            self.activity.delete_for_file(tenant, uid)
        elif etype == "acl.changed" and uid:
            self._invalidate("invalidate_resource", tenant, uid)
        elif etype in ("role.assigned", "role.member_removed"):
            member = event.get("member")
            if member:
                self._invalidate("invalidate_member", tenant, member)
        elif etype == "role.deleted":
            self._invalidate("invalidate_tenant", tenant)

    def _invalidate(self, method: str, *args) -> None:
        fn = getattr(self.permissions, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            log.warning("cache invalidation (%s) failed", method, exc_info=True)

    # ------------------------------ run loop -------------------------------
    def run_forever(self, source) -> None:
        source.ensure_group()
        while True:
            for msg_id, event in source.read():
                try:
                    self.handle(event)
                except Exception:
                    log.exception("failed handling event %s", msg_id)
                source.ack([msg_id])


class RedisEventSource:
    """XREADGROUP over the core's stream for our consumer group (mirrors CSAI)."""
    def __init__(self, config, consumer_name: str = "worker-1"):
        self.config = config
        self.stream = config.events_stream
        self.group = config.events_group
        self.consumer = consumer_name
        self._redis = None

    def _client(self):
        if self._redis is None:
            import redis
            self._redis = redis.Redis(
                host=self.config.redis_host, port=self.config.redis_port,
                password=self.config.redis_password or None, db=self.config.redis_db)
        return self._redis

    def ensure_group(self) -> None:
        import redis
        try:
            self._client().xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    @staticmethod
    def _parse(fields) -> dict:
        raw = fields.get(b"payload") or fields.get("payload")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def read(self, count: int = 64, block_ms: int = 5000) -> List[Entry]:
        resp = self._client().xreadgroup(self.group, self.consumer, {self.stream: ">"},
                                         count=count, block=block_ms)
        out: List[Entry] = []
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                mid = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else msg_id
                out.append((mid, self._parse(fields)))
        return out

    def ack(self, msg_ids: List[str]) -> None:
        if msg_ids:
            self._client().xack(self.stream, self.group, *msg_ids)


def main() -> None:
    import logging as _l

    from .activity_store import ActivityStore
    from .config import Config, load_dotenv
    from .permissions import Permissions
    from .store import ThreadStore

    _l.basicConfig(level=_l.INFO)
    load_dotenv()
    config = Config()
    consumer = EventConsumer(config, activity=ActivityStore(config), store=ThreadStore(config),
                             permissions=Permissions(config))
    log.info("discussion consumer — stream=%s group=%s", config.events_stream, config.events_group)
    consumer.run_forever(RedisEventSource(config))


if __name__ == "__main__":
    main()

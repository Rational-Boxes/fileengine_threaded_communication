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
import time
from typing import List, Tuple

from .notifications import KINDS as NOTIFY_KINDS, SYSTEM_ACTOR

log = logging.getLogger("discussion.consumer")

_ACTIVITY = {"file.created": "created", "file.updated": "updated", "file.restored": "restored"}
Entry = Tuple[str, dict]


# The name this service acknowledges erasures under; must match the core's
# FILEENGINE_ERASURE_PARTICIPANTS entry, or the core waits forever for an
# acknowledgement filed under a name it is not looking for.
ERASURE_PARTICIPANT = "discussion"


class EventConsumer:
    def __init__(self, config, *, activity, store, permissions, notifications=None,
                 core=None):
        self.config = config
        self.activity = activity
        self.store = store
        self.permissions = permissions
        # Optional so existing constructions (and tests) keep working; a share
        # event with no store logs and moves on rather than crashing the loop.
        self.notifications = notifications
        # Optional so existing constructions and tests need not grow a gRPC
        # dependency they do not use. Without it an erasure is still honoured
        # locally — the data is destroyed — but cannot be acknowledged, so the
        # core keeps offering it and the sweep will retry. That is the safe
        # direction: unacknowledged is visible, silently-uncomplied is not.
        self.core = core

    # ── Erasure (PROPOSAL_accountability_record.md §5.4) ────────────────────

    def _honour_erasure(self, tenant: str, uid: str, erasure_id: str = "") -> None:
        """Destroy this file's discussion, then say so — or say plainly that we could not."""
        try:
            counts = self.store.erase_file(tenant, uid, erasure_id)
            self.activity.delete_for_file(tenant, uid)
        except Exception as e:            # noqa: BLE001 — the reason must reach the record
            log.error("erasure %s: could not destroy discussion for %s: %s",
                      erasure_id or "(event)", uid, e)
            self._acknowledge(tenant, erasure_id, False, f"destroy failed: {e}")
            raise
        log.info("erased discussion for %s (threads=%s, comments=%s)",
                 uid, counts["threads"], counts["comments"])
        self._acknowledge(
            tenant, erasure_id, True,
            f"destroyed {counts['threads']} thread(s), {counts['comments']} comment(s), "
            "revisions and pre-redaction bodies")

    def _acknowledge(self, tenant: str, erasure_id: str, complied: bool, detail: str) -> None:
        if not erasure_id or self.core is None:
            return
        try:
            state = self.core.acknowledge_erasure(
                erasure_id, ERASURE_PARTICIPANT, complied=complied, detail=detail,
                tenant=tenant)
            log.info("acknowledged erasure %s (complied=%s) -> %s", erasure_id, complied, state)
        except Exception as e:            # noqa: BLE001
            # The destruction already committed. A lost ack delays completion,
            # which is the safe direction; the sweep re-offers it.
            log.warning("erasure %s destroyed locally but ack failed: %s", erasure_id, e)

    def sweep_erasures(self, tenants, limit: int = 100) -> int:
        """The guarantee path (§5.4.5).

        The event bus is fail-open and drop-oldest, so a dropped erasure event
        would leave comments quoting an erased document in place — silently. This
        poll is what the attestation actually counts, and it is how a service that
        was down, or restored from a pre-erasure backup, converges.
        """
        if self.core is None:
            return 0
        # ONE call, across every tenant. `tenants` is accepted for callers that
        # want to narrow it, but the default is all of them: guessing the tenant
        # set from local activity or config was wrong in the quiet direction —
        # an erasure in a tenant we had not been told about sat unacknowledged
        # for ever, with nothing saying so. The core is the authority.
        try:
            pending = self.core.list_pending_erasures(ERASURE_PARTICIPANT, limit=limit,
                                                      all_tenants=True)
        except Exception as e:            # noqa: BLE001
            log.warning("erasure sweep: could not list pending erasures: %s", e)
            return 0

        wanted = set(tenants or [])
        done = 0
        for item in pending:
            # The tenant the ROW carries. Acknowledging into the wrong schema
            # would leave the real erasure outstanding while looking answered.
            tenant = item.get("tenant") or "default"
            if wanted and tenant not in wanted:
                continue
            try:
                self._honour_erasure(tenant, item["uid"], item["erasure_id"])
                done += 1
            except Exception:             # noqa: BLE001 — logged; keep sweeping
                continue
        return done

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
        elif etype == "file.erased" and uid:
            # Deliberately its own branch. file.deleted is a SOFT delete the core
            # can reverse, so the threads survive and file.restored brings the
            # activity back. This one is irreversible and the comment bodies —
            # which quote the document — are exactly what has to go.
            self._honour_erasure(tenant, uid, event.get("erasure_id", ""))
        elif etype == "acl.changed" and uid:
            self._invalidate("invalidate_resource", tenant, uid)
        elif etype in ("role.assigned", "role.member_removed"):
            member = event.get("member")
            if member:
                self._invalidate("invalidate_member", tenant, member)
        elif etype == "role.deleted":
            self._invalidate("invalidate_tenant", tenant)
        elif etype.startswith("share."):
            # Share links (spec §10.6). share_service publishes these onto the
            # same stream the core uses, so no new transport is introduced.
            #
            # WHICH events exist is share_service's decision (its
            # `share.attention_events` setting) — this end raises whatever
            # arrives. Gating in both places would mean an operator turning an
            # event on and nothing happening.
            #
            # A drop ALSO arrives as an ordinary file.created, which is handled
            # above. That branch records document ACTIVITY and never writes a
            # notification, so a drop is not raised twice.
            self._share_notification(tenant, etype, event)

    def _share_notification(self, tenant: str, etype: str, event: dict) -> None:
        if self.notifications is None:
            log.warning("share event %s dropped: no notification store", etype)
            return
        creator = event.get("creator") or ""
        if not creator:
            log.warning("share event %s has no creator; nothing to notify", etype)
            return
        kind = etype.replace(".", "_", 1)
        if kind not in NOTIFY_KINDS:
            log.warning("share event %s maps to unknown kind %s", etype, kind)
            return
        # The actor must never be the creator: add() suppresses
        # self-notification, so a creator's own links could otherwise never
        # notify them. An external redeemer is named; a link that simply went
        # dead has no human behind it and gets the reserved system actor.
        actor = event.get("actor") or SYSTEM_ACTOR
        if actor == creator:
            actor = SYSTEM_ACTOR
        try:
            self.notifications.add(
                tenant, user_id=creator, kind=kind,
                file_uid=event.get("file_uid", ""), actor=actor,
                share_link_uid=event.get("link_uid") or None,
                # Denormalized so the row renders without resolving the
                # resource — the point of which is that "your link stopped
                # working" usually means the creator can no longer read it.
                detail_text=event.get("detail") or None)
        except Exception:
            log.exception("failed recording share notification %s", etype)

    def _invalidate(self, method: str, *args) -> None:
        fn = getattr(self.permissions, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            log.warning("cache invalidation (%s) failed", method, exc_info=True)

    def _start_erasure_sweeper(self) -> None:
        """Poll for erasures we owe, forever, in a daemon thread. Never fatal."""
        # getattr, not self.core: run_forever is exercised against a bare
        # EventConsumer.__new__ in the resilience tests, which have no __init__
        # and so no attributes. The sweeper must not be what breaks that — it is
        # an addition to the loop, not a precondition for it.
        if getattr(self, "core", None) is None:
            log.warning("erasure sweeper not started: no core client")
            return
        import threading

        interval = int(getattr(self.config, "erasure_sweep_interval_s", 60) or 60)

        def loop() -> None:
            while True:
                try:
                    done = self.sweep_erasures([])
                    if done:
                        log.info("erasure sweep honoured %d outstanding erasure(s)", done)
                except Exception:
                    log.exception("erasure sweep failed; retrying next tick")
                time.sleep(interval)

        threading.Thread(target=loop, name="erasure-sweep", daemon=True).start()
        log.info("erasure sweeper started (every %ss, participant=%s)",
                 interval, ERASURE_PARTICIPANT)

    def _tenants_for_sweep(self) -> list:
        """Tenants to poll. Configured list, else the default tenant alone.

        Deliberately explicit rather than discovered: this service has no
        authoritative view of the tenant set, and guessing wrong in the quiet
        direction (missing a tenant) would leave erasures unacknowledged with
        nothing saying so.
        """
        raw = getattr(self.config, "erasure_sweep_tenants", "") or ""
        tenants = [t.strip() for t in raw.split(",") if t.strip()]
        return tenants or [getattr(self.config, "default_tenant", "default") or "default"]

    # ------------------------------ run loop -------------------------------
    def run_forever(self, source) -> None:
        source.ensure_group()
        # The erasure guarantee path (§5.4.5), on a timer beside the event loop.
        # The event that triggers a purge is fail-open and drop-oldest by design,
        # so a dropped one would leave comments quoting an erased document in
        # place — silently. In a thread rather than folded into the loop below,
        # which blocks on a read for seconds at a time.
        self._start_erasure_sweeper()
        while True:
            # The whole CYCLE is guarded, not just handle(). Only the per-event
            # work used to be, so anything raised by source.read() or ack() — a
            # dropped connection, a restarted server — escaped the loop and ended
            # the process. Exiting 0 under `restart_policy: always` looks like a
            # clean stop, so the failure showed up as a container quietly
            # restarting every few minutes rather than as an error anyone chased.
            try:
                for msg_id, event in source.read():
                    try:
                        self.handle(event)
                    except Exception:
                        log.exception("failed handling event %s", msg_id)
                    source.ack([msg_id])
            except KeyboardInterrupt:  # pragma: no cover - operator stop
                log.info("discussion consumer stopping")
                return
            except Exception:
                # Back off before retrying: if Redis is genuinely down, an
                # unthrottled loop would spin on failed connections.
                log.exception("discussion consumer cycle failed; retrying in %ss",
                              CYCLE_BACKOFF_S)
                source.reset()
                time.sleep(CYCLE_BACKOFF_S)


#: Socket read timeout. Deliberately well above the 5s block used by read(), so a
#: blocking XREADGROUP on an idle stream completes normally rather than racing the
#: socket deadline. Still bounded, so a genuinely hung server is detected.
SOCKET_TIMEOUT_S = 30

#: Pause after a failed cycle, so a persistently unreachable Redis is retried
#: steadily rather than spun on.
CYCLE_BACKOFF_S = 5


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
                password=self.config.redis_password or None, db=self.config.redis_db,
                # MUST exceed the block time of the read below. redis-py 8 defaults
                # socket_timeout to 5s and this consumer blocks for 5000ms, so on an
                # idle stream the socket gave up at the same instant the server was
                # due to answer "no events" — a guaranteed race that fired on every
                # quiet poll. Headroom turns the idle case back into an ordinary
                # empty reply instead of an exception plus a reconnect.
                socket_timeout=SOCKET_TIMEOUT_S)
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
        import redis
        try:
            resp = self._client().xreadgroup(self.group, self.consumer, {self.stream: ">"},
                                             count=count, block=block_ms)
        except redis.exceptions.TimeoutError:
            # Still caught, even with the headroom above: a caller may pass a
            # block_ms longer than the socket timeout, and a timed-out blocking
            # read means "no events", not a failure. Returning an empty batch is
            # what the poll loop already does with an idle stream.
            return []
        out: List[Entry] = []
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                mid = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else msg_id
                out.append((mid, self._parse(fields)))
        return out

    def ack(self, msg_ids: List[str]) -> None:
        if msg_ids:
            self._client().xack(self.stream, self.group, *msg_ids)

    def reset(self) -> None:
        """Drop the cached client so the next cycle reconnects.

        A connection that failed mid-cycle may be unusable; rebuilding is cheap
        and avoids retrying forever through a dead socket."""
        try:
            if self._redis is not None:
                self._redis.close()
        except Exception:
            pass
        self._redis = None


def main() -> None:
    import logging as _l

    from .activity_store import ActivityStore
    from .config import Config, load_dotenv
    from .notifications import NotificationStore
    from .permissions import Permissions
    from .store import ThreadStore

    _l.basicConfig(level=_l.INFO)
    load_dotenv()
    config = Config()
    # A core client, so erasures can actually be ACKNOWLEDGED. Without one the
    # data is still destroyed but the core is never told, so every erasure this
    # service participates in stays outstanding for ever — an alarm that means
    # nothing because it is always ringing.
    try:
        from .core_client import agent_client
        core = agent_client(config)
    except Exception:               # noqa: BLE001
        core = None
        log.warning("no core client: erasures will be honoured but not acknowledged",
                    exc_info=True)

    consumer = EventConsumer(config, activity=ActivityStore(config), store=ThreadStore(config),
                             permissions=Permissions(config),
                             notifications=NotificationStore(config),
                             core=core)
    log.info("discussion consumer — stream=%s group=%s", config.events_stream, config.events_group)
    consumer.run_forever(RedisEventSource(config))


if __name__ == "__main__":
    main()

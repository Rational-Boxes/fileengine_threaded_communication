"""The cron-triggered digest sender (SPECIFICATION §11c).

A short-lived batch — **invoked hourly** (``discuss-digest``); each run self-selects
the subscriptions due this hour and sends one digest each. Acting **as each
recipient** (roles resolved via the directory), every item is ACL-filtered at send
time; delivery is idempotent per period (``UNIQUE(user_id, period_key)``); a run
lock prevents overlapping ticks. SMTP + AI summary are best-effort and never abort
the batch.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

from .ldap_auth import Identity
from .schedule import is_due, period_key, since_for

log = logging.getLogger("discussion.digest")

_KIND_LABEL = {
    "mention": "You were mentioned",
    "reply": "New reply",
    "review_requested": "Review requested",
    "review_acknowledged": "Review acknowledged",
    "review_completed": "Review completed",
    "thread_resolved": "Thread resolved",
}


class DigestSender:
    def __init__(self, config, *, digest_store, notifications, activity, permissions,
                 directory, mailer):
        self.config = config
        self.digest = digest_store
        self.notifications = notifications
        self.activity = activity
        self.perms = permissions
        self.directory = directory
        self.mailer = mailer

    # -- content ------------------------------------------------------------
    def build(self, identity: Identity, since_iso: str, *, want_activity: bool = True) -> dict:
        """Gather this recipient's items since ``since_iso``, filtered as them: must
        be READable AND still live (not soft-deleted), the same guard the dashboard
        feeds apply — a trashed document must not surface in the digest either."""
        def _visible(file_uid: str) -> bool:
            return self.perms.can_read(identity, file_uid) and self.perms.is_live(identity, file_uid)
        notes = self.notifications.list_for(identity.tenant, identity.user, limit=200, since=since_iso)
        notes = [n for n in notes if _visible(n["file_uid"])]
        acts: list[dict] = []
        if want_activity:
            acts = self.activity.recent(identity.tenant, limit=200, since=since_iso)
            acts = [a for a in acts if _visible(a["file_uid"])]
        return {"attention": notes, "activity": acts}

    def render(self, content: dict) -> tuple[str, str, str]:
        base = (self.config.spa_base_url or "").rstrip("/")

        def link(file_uid: str, thread_id: Optional[str]) -> str:
            q = f"?thread={thread_id}" if thread_id else ""
            return f"{base}/preview/{file_uid}{q}"

        n_att, n_act = len(content["attention"]), len(content["activity"])
        subject = f"FileEngine digest — {n_att} for you, {n_act} updates"

        lines = ["Needs your attention:"]
        html = ["<h3>Needs your attention</h3><ul>"]
        for n in content["attention"]:
            label = _KIND_LABEL.get(n["kind"], n["kind"])
            lines.append(f"  - {label} (by {n['actor']}) — {link(n['file_uid'], n.get('thread_id'))}")
            html.append(f'<li>{label} by {n["actor"]} — '
                        f'<a href="{link(n["file_uid"], n.get("thread_id"))}">open</a></li>')
        if not content["attention"]:
            lines.append("  (nothing)")
        html.append("</ul><h3>Recent document activity</h3><ul>")
        lines.append("\nRecent document activity:")
        for a in content["activity"]:
            name = a.get("name") or a.get("path") or a["file_uid"]
            lines.append(f"  - {a['event_type']}: {name} — {link(a['file_uid'], None)}")
            html.append(f'<li>{a["event_type"]}: {name} — '
                        f'<a href="{link(a["file_uid"], None)}">open</a></li>')
        if not content["activity"]:
            lines.append("  (nothing)")
        html.append("</ul>")
        return subject, "\n".join(lines), "".join(html)

    # -- per-subscription ---------------------------------------------------
    def _recipient(self, sub: dict, tenant: str) -> Optional[Identity]:
        ident = self.directory.resolve_principal(sub["user_id"])
        if ident is None:
            return None
        ident.tenant = tenant
        return ident

    def process(self, tenant: str, sub: dict, now_utc: _dt.datetime) -> str:
        """Handle one subscription for this run. Returns a status string."""
        cadence = sub["cadence"]
        tz = sub.get("timezone", "UTC")
        if not is_due(cadence, send_hour_local=sub["send_hour_local"], send_dow=sub["send_dow"],
                      tz_name=tz, now_utc=now_utc):
            return "not_due"
        pk = period_key(cadence, tz_name=tz, now_utc=now_utc)
        if self.digest.already_delivered(tenant, sub["user_id"], pk):
            return "duplicate"

        ident = self._recipient(sub, tenant)
        if ident is None:
            self.digest.record_delivery(tenant, sub["user_id"], pk, status="error",
                                        error="unresolved recipient")
            return "unresolved"

        scope = sub.get("scope") or {}
        content = self.build(ident, since_for(cadence, now_utc).isoformat(),
                             want_activity=bool(scope.get("activity", True)))
        count = len(content["attention"]) + len(content["activity"])
        if count == 0 and sub.get("quiet_if_empty", True):
            self.digest.record_delivery(tenant, sub["user_id"], pk, status="skipped_empty")
            return "empty"

        subject, text, html = self.render(content)
        sent = self.mailer.send(ident.email or ident.user, subject, text, html)
        if not sent:
            # Leave the period unmarked so the next hourly run retries (bounded by
            # the delivery ledger once it eventually succeeds).
            return "send_failed"
        self.digest.record_delivery(tenant, sub["user_id"], pk, status="sent", item_count=count)
        return "sent"

    def run_pass(self, now_utc: Optional[_dt.datetime] = None) -> dict:
        """One hourly pass over all due subscriptions across all tenants."""
        now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
        lock = self.digest.try_lock()
        if lock is None:
            log.info("another digest run holds the lock — skipping")
            return {"skipped": "locked"}
        stats: dict[str, int] = {}
        try:
            for tenant in self.digest.list_tenants():
                for sub in self.digest.list_enabled(tenant, limit=self.config.digest_batch_size):
                    try:
                        status = self.process(tenant, sub, now_utc)
                    except Exception:
                        log.exception("digest failed for %s/%s", tenant, sub.get("user_id"))
                        status = "error"
                    stats[status] = stats.get(status, 0) + 1
        finally:
            lock.close()
        log.info("digest pass complete: %s", stats)
        return stats

    def send_now(self, identity: Identity) -> dict:
        """On-demand digest for one user (§11c) — 24h lookback, ignores cadence."""
        since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).isoformat()
        content = self.build(identity, since)
        subject, text, html = self.render(content)
        sent = self.mailer.send(identity.email or identity.user, subject, text, html)
        return {"sent": bool(sent), "items": len(content["attention"]) + len(content["activity"])}


def build_sender(config) -> DigestSender:
    from .activity_store import ActivityStore
    from .digest_store import DigestStore
    from .directory import Directory
    from .mailer import SmtpMailer
    from .notifications import NotificationStore
    from .permissions import Permissions
    return DigestSender(
        config, digest_store=DigestStore(config), notifications=NotificationStore(config),
        activity=ActivityStore(config), permissions=Permissions(config),
        directory=Directory(config), mailer=SmtpMailer(config))


def main() -> None:
    import logging as _l

    from .config import Config, load_dotenv
    _l.basicConfig(level=_l.INFO)
    load_dotenv()
    config = Config()
    if not config.digest_enabled:
        log.info("digest disabled (DISC_DIGEST_ENABLED=false)")
        return
    build_sender(config).run_pass()


if __name__ == "__main__":
    main()

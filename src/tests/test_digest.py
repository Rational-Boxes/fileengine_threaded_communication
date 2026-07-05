"""The cron digest sender (§11c) — hermetic, with fakes."""
import datetime as dt

from discussion.config import Config
from discussion.digest import DigestSender
from discussion.ldap_auth import Identity

from .test_threads import FakePerms

UTC = dt.timezone.utc


class FakeDigest:
    def __init__(self, subs=None):
        self.subs = subs or {}          # tenant -> [sub,...]
        self.delivered = {}             # (tenant,user,pk) -> status
        self.locked = False

    def list_tenants(self):
        return list(self.subs.keys())

    def list_enabled(self, tenant, *, limit=1000):
        return [s for s in self.subs.get(tenant, []) if s["cadence"] != "off"][:limit]

    def already_delivered(self, tenant, user, pk):
        return (tenant, user, pk) in self.delivered

    def record_delivery(self, tenant, user, pk, *, status, item_count=0, error=None):
        if (tenant, user, pk) in self.delivered:
            return False
        self.delivered[(tenant, user, pk)] = status
        return True

    def try_lock(self):
        class L:
            def close(self_inner):
                pass
        return L()


class FakeNotes:
    def __init__(self, rows=None):
        self.rows = rows or []

    def list_for(self, tenant, user, *, limit=200, unread_only=False, since=None):
        return [dict(r) for r in self.rows if r["user_id"] == user]


class FakeActivity:
    def __init__(self, rows=None):
        self.rows = rows or []

    def recent(self, tenant, *, limit=200, since=None):
        return [dict(r) for r in self.rows]


class FakeDirectory:
    def resolve_principal(self, identifier):
        return Identity(user=identifier, roles=["users"], tenant="default",
                        email=f"{identifier}@x.test")


class FakeMailer:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, to_addr, subject, text, html):
        self.sent.append({"to": to_addr, "subject": subject})
        return self.ok


def _sender(*, notes=None, activity=None, digest=None, reads=True, live=True, mailer=None):
    return DigestSender(
        Config(),
        digest_store=digest or FakeDigest(),
        notifications=FakeNotes(notes),
        activity=FakeActivity(activity),
        permissions=FakePerms(reads=reads, live=live),
        directory=FakeDirectory(),
        mailer=mailer or FakeMailer(),
    ), mailer


def _note(user, file_uid="f1"):
    return {"id": 1, "user_id": user, "kind": "mention", "file_uid": file_uid,
            "thread_id": "t1", "actor": "carol"}


def _sub(user="bob", cadence="hourly", **kw):
    base = {"user_id": user, "cadence": cadence, "send_hour_local": 8, "send_dow": 0,
            "timezone": "UTC", "scope": {}, "ai_summary": False, "quiet_if_empty": True}
    base.update(kw)
    return base


def test_build_acl_filters():
    s, _ = _sender(notes=[_note("bob", "f1"), _note("bob", "f2")], reads={"f1"})
    content = s.build(Identity(user="bob", tenant="default"), "since")
    assert [n["file_uid"] for n in content["attention"]] == ["f1"]


def test_build_excludes_deleted():
    # Both readable, but f2 is soft-deleted → excluded from the digest, same as the
    # dashboard feeds.
    s, _ = _sender(notes=[_note("bob", "f1"), _note("bob", "f2")], reads=True, live={"f1"})
    content = s.build(Identity(user="bob", tenant="default"), "since")
    assert [n["file_uid"] for n in content["attention"]] == ["f1"]


def test_process_sends_and_records():
    mail = FakeMailer()
    s, _ = _sender(notes=[_note("bob")], mailer=mail)
    status = s.process("default", _sub("bob", "hourly"), dt.datetime(2026, 7, 4, 15, tzinfo=UTC))
    assert status == "sent"
    assert mail.sent and mail.sent[0]["to"] == "bob@x.test"
    assert s.digest.delivered[("default", "bob", "2026-07-04T15")] == "sent"


def test_process_quiet_if_empty_skips_send():
    mail = FakeMailer()
    s, _ = _sender(notes=[], mailer=mail)
    status = s.process("default", _sub("bob", "hourly", quiet_if_empty=True),
                       dt.datetime(2026, 7, 4, 15, tzinfo=UTC))
    assert status == "empty"
    assert mail.sent == []
    assert s.digest.delivered[("default", "bob", "2026-07-04T15")] == "skipped_empty"


def test_process_idempotent_per_period():
    s, _ = _sender(notes=[_note("bob")])
    now = dt.datetime(2026, 7, 4, 15, tzinfo=UTC)
    assert s.process("default", _sub("bob", "hourly"), now) == "sent"
    assert s.process("default", _sub("bob", "hourly"), now) == "duplicate"  # same period


def test_process_not_due():
    s, _ = _sender(notes=[_note("bob")])
    # daily due at hour 8; run at 15:00 UTC → not due
    status = s.process("default", _sub("bob", "daily", send_hour_local=8),
                       dt.datetime(2026, 7, 4, 15, tzinfo=UTC))
    assert status == "not_due"


def test_process_send_failure_not_recorded():
    mail = FakeMailer(ok=False)
    s, _ = _sender(notes=[_note("bob")], mailer=mail)
    now = dt.datetime(2026, 7, 4, 15, tzinfo=UTC)
    assert s.process("default", _sub("bob", "hourly"), now) == "send_failed"
    assert ("default", "bob", "2026-07-04T15") not in s.digest.delivered  # retried next hour


def test_run_pass_iterates():
    digest = FakeDigest({"default": [_sub("bob", "hourly"), _sub("carol", "off")]})
    s = DigestSender(Config(), digest_store=digest, notifications=FakeNotes([_note("bob")]),
                     activity=FakeActivity(), permissions=FakePerms(reads=True),
                     directory=FakeDirectory(), mailer=FakeMailer())
    stats = s.run_pass(dt.datetime(2026, 7, 4, 15, tzinfo=UTC))
    assert stats.get("sent") == 1     # carol is 'off' → not enumerated


def test_send_now():
    mail = FakeMailer()
    s, _ = _sender(notes=[_note("bob")], mailer=mail)
    res = s.send_now(Identity(user="bob", tenant="default", email="bob@x.test"))
    assert res["sent"] is True and res["items"] == 1

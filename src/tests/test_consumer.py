"""Core-event consumer routing (§8 / M4a) — pure handle() logic."""
from discussion.consumer import EventConsumer


class FakeActivity:
    def __init__(self):
        self.records = []

    def record(self, tenant, *, event_type, file_uid, version="", name="", path="", actor=""):
        self.records.append({"tenant": tenant, "event_type": event_type, "file_uid": file_uid,
                             "version": version})


class FakeStore:
    def __init__(self):
        self.stale = []

    def mark_anchor_stale(self, tenant, file_uid, new_version):
        self.stale.append((tenant, file_uid, new_version))
        return 1


class FakePerms:
    def __init__(self):
        self.calls = []

    def invalidate_resource(self, t, u):
        self.calls.append(("resource", t, u))

    def invalidate_member(self, t, u):
        self.calls.append(("member", t, u))

    def invalidate_tenant(self, t):
        self.calls.append(("tenant", t))


def _mk():
    a, s, p = FakeActivity(), FakeStore(), FakePerms()
    return EventConsumer(None, activity=a, store=s, permissions=p), a, s, p


def test_file_created_records_activity():
    c, a, s, p = _mk()
    c.handle({"type": "file.created", "tenant": "t1", "file_uid": "f1", "name": "a.txt"})
    assert a.records and a.records[0]["event_type"] == "created" and a.records[0]["file_uid"] == "f1"
    assert s.stale == []


def test_file_updated_records_and_marks_stale():
    c, a, s, p = _mk()
    c.handle({"type": "file.updated", "tenant": "t1", "file_uid": "f1", "version": "v2"})
    assert a.records[0]["event_type"] == "updated"
    assert s.stale == [("t1", "f1", "v2")]


def test_rendition_events_ignored():
    c, a, s, p = _mk()
    c.handle({"type": "file.created", "file_uid": "f1", "is_rendition": True})
    assert a.records == []


def test_acl_changed_invalidates_resource():
    c, a, s, p = _mk()
    c.handle({"type": "acl.changed", "tenant": "t1", "file_uid": "f1"})
    assert ("resource", "t1", "f1") in p.calls


def test_role_events_invalidate_member_and_tenant():
    c, a, s, p = _mk()
    c.handle({"type": "role.assigned", "tenant": "t1", "member": "carol"})
    c.handle({"type": "role.member_removed", "tenant": "t1", "member": "dave"})
    c.handle({"type": "role.deleted", "tenant": "t1"})
    assert ("member", "t1", "carol") in p.calls
    assert ("member", "t1", "dave") in p.calls
    assert ("tenant", "t1") in p.calls


def test_unknown_event_is_a_noop():
    c, a, s, p = _mk()
    c.handle({"type": "file.renamed", "tenant": "t1", "file_uid": "f1"})
    assert a.records == [] and s.stale == [] and p.calls == []

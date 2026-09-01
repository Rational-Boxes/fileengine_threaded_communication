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

"""Core-event consumer routing (§8 / M4a) — pure handle() logic."""
from discussion.consumer import EventConsumer


class FakeActivity:
    def __init__(self):
        self.records = []
        self.deleted = []

    def record(self, tenant, *, event_type, file_uid, version="", name="", path="", actor=""):
        self.records.append({"tenant": tenant, "event_type": event_type, "file_uid": file_uid,
                             "version": version})

    def delete_for_file(self, tenant, file_uid):
        self.deleted.append((tenant, file_uid))
        return 1


class FakeStore:
    def __init__(self):
        self.stale = []
        self.erased = []

    def mark_anchor_stale(self, tenant, file_uid, new_version):
        self.stale.append((tenant, file_uid, new_version))
        return 1

    def erase_file(self, tenant, file_uid, erasure_id=""):
        self.erased.append((tenant, file_uid, erasure_id))
        return {"threads": 2, "comments": 5}


class FakePerms:
    def __init__(self):
        self.calls = []

    def invalidate_resource(self, t, u):
        self.calls.append(("resource", t, u))

    def invalidate_member(self, t, u):
        self.calls.append(("member", t, u))

    def invalidate_tenant(self, t):
        self.calls.append(("tenant", t))


class FakeCore:
    def __init__(self, pending=None, fail=False):
        self._pending = pending or {}
        self.acks = []
        self.fail = fail

    def list_pending_erasures(self, participant, limit=0, tenant=None):
        return list(self._pending.get(tenant, []))

    def acknowledge_erasure(self, erasure_id, participant, complied=True, detail="",
                            tenant=None):
        if self.fail:
            raise RuntimeError("core unreachable")
        self.acks.append({"erasure_id": erasure_id, "participant": participant,
                          "complied": complied, "detail": detail})
        return "complete"


def _mk(core=None):
    a, s, p = FakeActivity(), FakeStore(), FakePerms()
    return (EventConsumer(None, activity=a, store=s, permissions=p, core=core),
            a, s, p)


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


def test_file_deleted_prunes_activity():
    c, a, s, p = _mk()
    c.handle({"type": "file.deleted", "tenant": "t1", "file_uid": "f1"})
    assert a.deleted == [("t1", "f1")]
    assert a.records == []


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


# --- share events (spec §10.6) -------------------------------------------

class FakeNotifications:
    def __init__(self):
        self.rows = []

    def add(self, tenant, **kw):
        self.rows.append({"tenant": tenant, **kw})


def _mk_share():
    a, s, p = FakeActivity(), FakeStore(), FakePerms()
    n = FakeNotifications()
    return EventConsumer(None, activity=a, store=s, permissions=p,
                         notifications=n), n, a


def test_a_drop_notifies_the_creator_naming_the_redeemer():
    c, n, _ = _mk_share()
    c.handle({"type": "share.drop_received", "tenant": "t", "creator": "alice",
              "actor": "share:l1|bob@contractor.example", "link_uid": "l1",
              "file_uid": "f1", "detail": "plans.pdf"})
    assert len(n.rows) == 1
    assert n.rows[0]["kind"] == "share_drop_received"
    assert n.rows[0]["user_id"] == "alice"
    assert n.rows[0]["share_link_uid"] == "l1"
    # The verified name is the confirmation the sender is waiting for.
    assert "bob@contractor.example" in n.rows[0]["actor"]


def test_a_creator_is_never_their_own_actor():
    """add() suppresses self-notification, so a share event whose actor IS the
    creator would silently never notify them — the exact failure this guards."""
    c, n, _ = _mk_share()
    c.handle({"type": "share.link_dead", "tenant": "t", "creator": "alice",
              "actor": "alice", "link_uid": "l2", "detail": "Q3 drawings"})
    assert n.rows[0]["actor"] != "alice"
    assert n.rows[0]["actor"].startswith("system:")


def test_a_system_event_with_no_actor_still_notifies():
    c, n, _ = _mk_share()
    c.handle({"type": "share.otp_send_failed", "tenant": "t", "creator": "alice",
              "link_uid": "l3", "detail": "code to bob@x.example"})
    assert n.rows[0]["kind"] == "share_otp_send_failed"
    assert n.rows[0]["actor"] == "system:share"


def test_share_rows_carry_their_own_text():
    """So the feed can render them without resolving the resource — "your link
    stopped working" usually means the creator can no longer read it."""
    c, n, _ = _mk_share()
    c.handle({"type": "share.link_dead", "tenant": "t", "creator": "alice",
              "link_uid": "l4", "detail": "Drawing-A.pdf"})
    assert n.rows[0]["detail_text"] == "Drawing-A.pdf"


def test_an_unknown_share_event_is_dropped_not_written():
    c, n, _ = _mk_share()
    c.handle({"type": "share.something_new", "tenant": "t", "creator": "alice"})
    assert n.rows == []


def test_a_share_event_without_a_creator_notifies_nobody():
    c, n, _ = _mk_share()
    c.handle({"type": "share.drop_received", "tenant": "t", "link_uid": "l5"})
    assert n.rows == []


def test_a_drop_is_not_notified_twice_by_its_file_created():
    """A drop also lands as an ordinary file.created. That path records document
    ACTIVITY and must not also raise an attention item."""
    c, n, a = _mk_share()
    c.handle({"type": "file.created", "tenant": "t", "file_uid": "f1",
              "actor": "alice", "name": "plans.pdf"})
    assert n.rows == []
    assert a.records  # ...but it did record activity


# ── Erasure (PROPOSAL_accountability_record.md §5.4) ────────────────────────

def test_erasure_destroys_the_discussion_rather_than_pruning_the_feed():
    """file.erased must not behave like file.deleted.

    A soft delete only drops the file out of the activity feed; the threads
    survive because file.restored brings them back. An erasure is irreversible
    and the comment bodies — which quote the document — are exactly what has to
    go, so sharing that branch would leave the erased content readable in
    quotation.
    """
    core = FakeCore()
    c, a, s, p = _mk(core)

    c.handle({"type": "file.deleted", "tenant": "t1", "file_uid": "f1"})
    assert s.erased == [], "a soft delete must not destroy threads"

    c.handle({"type": "file.erased", "tenant": "t1", "file_uid": "f2",
              "erasure_id": "e2"})
    assert s.erased == [("t1", "f2", "e2")]
    assert ("t1", "f2") in a.deleted


def test_the_acknowledgement_states_what_was_destroyed():
    core = FakeCore()
    c, a, s, p = _mk(core)
    c.handle({"type": "file.erased", "tenant": "t1", "file_uid": "f1",
              "erasure_id": "e1"})

    assert len(core.acks) == 1
    ack = core.acks[0]
    assert ack["participant"] == "discussion"
    assert ack["complied"] is True
    assert "2 thread(s)" in ack["detail"] and "5 comment(s)" in ack["detail"]
    # Naming the pre-redaction bodies matters: `redactions` is documented as
    # retained forever, and an auditor needs to see that erasure overrode it.
    assert "pre-redaction" in ack["detail"]


def test_a_failure_is_acknowledged_as_a_failure():
    core = FakeCore()
    c, a, s, p = _mk(core)

    def boom(tenant, file_uid, erasure_id=""):
        raise RuntimeError("nope")
    s.erase_file = boom

    try:
        c.handle({"type": "file.erased", "tenant": "t1", "file_uid": "f1",
                  "erasure_id": "e1"})
    except RuntimeError:
        pass
    assert core.acks and core.acks[0]["complied"] is False


def test_a_lost_acknowledgement_leaves_the_data_destroyed():
    core = FakeCore(fail=True)
    c, a, s, p = _mk(core)
    c.handle({"type": "file.erased", "tenant": "t1", "file_uid": "f1",
              "erasure_id": "e1"})
    assert s.erased == [("t1", "f1", "e1")]


def test_the_sweep_catches_what_the_event_bus_dropped():
    # fileengine:events is fail-open and drop-oldest by design, so without this
    # a dropped event leaves comments quoting an erased document in place.
    core = FakeCore(pending={"t1": [{"erasure_id": "e9", "uid": "u9",
                                     "tenant": "t1", "initiated_at": 1}]})
    c, a, s, p = _mk(core)
    assert c.sweep_erasures(["t1"]) == 1
    assert s.erased == [("t1", "u9", "e9")]
    assert core.acks[0]["erasure_id"] == "e9"


def test_without_a_core_client_the_data_is_still_destroyed():
    # Unacknowledged is visible and the sweep retries; silently-uncomplied is not.
    c, a, s, p = _mk(core=None)
    c.handle({"type": "file.erased", "tenant": "t1", "file_uid": "f1",
              "erasure_id": "e1"})
    assert s.erased == [("t1", "f1", "e1")]

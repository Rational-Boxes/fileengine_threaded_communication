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

"""Dashboard feeds, attention flags & comment resolve (M4a) — hermetic."""
import types

import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.config import Config

from .test_threads import FakePerms, _auth, _fake_auth


class FakeNotes:
    def __init__(self, rows):
        self.rows, self.seen = rows, []

    def list_for(self, tenant, user, *, limit=50, unread_only=False):
        return [dict(r) for r in self.rows
                if r["user_id"] == user and (not unread_only or r.get("read_at") is None)][:limit]

    def mark_seen(self, tenant, user, nid):
        for r in self.rows:
            if r["id"] == nid and r["user_id"] == user and r.get("read_at") is None:
                r["read_at"] = "t"
                self.seen.append(nid)
                return True
        return False


class FakeActivity:
    def __init__(self, rows):
        self.rows = rows

    def recent(self, tenant, *, limit=50, since=None):
        return [dict(r) for r in self.rows][:limit]


class FakeStoreD:
    def __init__(self, mentions=None, comments=None):
        self.mentions, self.comments = mentions or {}, comments or {}

    def mention_flags(self, tenant, user, file_uids):
        return {u: c for u, c in self.mentions.items() if u in file_uids}

    def get_comment(self, tenant, cid):
        return self.comments.get(cid)


class FakeReviewsD:
    def __init__(self, reviews=None):
        self.reviews = reviews or {}

    def review_flags(self, tenant, user, file_uids):
        return {u: c for u, c in self.reviews.items() if u in file_uids}


class Ctx:
    def __init__(self, client, notes, store):
        self.client, self.notes, self.store = client, notes, store


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)

    def _make(*, reads=True, live=True, notes=None, activity=None, store=None, reviews=None):
        notes = FakeNotes(notes or [])
        store = FakeStoreD(**(store or {}))
        app = build_app(Config(), permissions=FakePerms(reads=reads, live=live), notifications=notes,
                        activity=FakeActivity(activity or []), store=store,
                        reviews=FakeReviewsD(reviews or {}))
        return Ctx(TestClient(app), notes, store)
    return _make


def test_attention_feed_is_acl_filtered(make):
    rows = [{"id": 1, "user_id": "bob", "kind": "mention", "file_uid": "f1", "thread_id": "t1",
             "review_id": None, "actor": "carol", "created_at": "t", "read_at": None},
            {"id": 2, "user_id": "bob", "kind": "reply", "file_uid": "f2", "thread_id": "t2",
             "review_id": None, "actor": "carol", "created_at": "t", "read_at": None}]
    c = make(reads={"f1"}, notes=rows)   # only f1 readable
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert [i["id"] for i in items] == [1]


def test_attention_feed_excludes_deleted(make):
    # Both readable, but f2 is soft-deleted → its notification must not surface
    # (same live guard as the activity feed).
    rows = [{"id": 1, "user_id": "bob", "kind": "mention", "file_uid": "f1", "thread_id": "t1",
             "review_id": None, "actor": "carol", "created_at": "t", "read_at": None},
            {"id": 2, "user_id": "bob", "kind": "reply", "file_uid": "f2", "thread_id": "t2",
             "review_id": None, "actor": "carol", "created_at": "t", "read_at": None}]
    c = make(reads=True, live={"f1"}, notes=rows)   # f2 not live (deleted)
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert [i["id"] for i in items] == [1]


def test_mark_seen(make):
    rows = [{"id": 5, "user_id": "bob", "kind": "mention", "file_uid": "f1", "thread_id": None,
             "review_id": None, "actor": "carol", "created_at": "t", "read_at": None}]
    c = make(reads=True, notes=rows)
    assert c.client.post("/dashboard/attention/5/seen", headers=_auth("bob")).json()["seen"] is True
    assert c.notes.seen == [5]
    # already seen → False
    assert c.client.post("/dashboard/attention/5/seen", headers=_auth("bob")).json()["seen"] is False


# ── the activity feed now asks the core ─────────────────────────────────────
#
# It used to read `document_activity` — a projection fed by an event for every
# file touched — over-fetch four times the page, and make TWO permission calls
# per row. ACL filtering and the soft-deleted guard have moved INTO the core's
# ListRecentFiles, which applies them in the query; they are covered there by
# acl_subtree_live_tests against a real database.
#
# What is left to test here is the contract this service still owns: that it
# asks the core as the CALLER, and that it maps the answer into the shape the
# SPA reads — including turning a version count into an event type.

class _FakeCore:
    """Stands in for the gRPC client. Records how it was asked."""

    def __init__(self, entries):
        self._entries = entries
        self.calls = []
        self.closed = False

    def list_recent_files(self, **kw):
        self.calls.append(kw)
        return {"entries": self._entries, "examined": len(self._entries) * 2,
                "scan_truncated": False}

    def close(self):
        self.closed = True


@pytest.fixture
def core(monkeypatch):
    holder = {}

    def _install(entries):
        fake = _FakeCore(entries)
        holder["fake"] = fake
        monkeypatch.setattr("discussion.core_client.client_for", lambda ident, cfg: fake)
        return fake
    return _install


def test_activity_is_answered_by_the_core(make, core):
    fake = core([
        {"uid": "f1", "name": "a", "version": "20260101_000000.000", "version_count": 3,
         "size": 10, "modified_at": 1767225600, "modified_by": "carol", "owner": "carol"},
        {"uid": "f2", "name": "b", "version": "20260102_000000.000", "version_count": 1,
         "size": 20, "modified_at": 1767312000, "modified_by": "dave", "owner": "dave"},
    ])
    c = make(activity=[{"id": 99, "file_uid": "SHOULD-NOT-APPEAR", "event_type": "updated",
                        "version": "", "name": "stale", "path": "", "actor": "x", "ts": "t"}])
    items = c.client.get("/dashboard/activity", headers=_auth("bob")).json()["items"]

    assert [i["file_uid"] for i in items] == ["f1", "f2"]
    # The local projection is no longer consulted — a row only it holds must not
    # surface. This is the assertion that would fail if the old path came back.
    assert "SHOULD-NOT-APPEAR" not in [i["file_uid"] for i in items]
    assert fake.closed, "the core client is released"


def test_event_type_comes_from_the_version_count(make, core):
    core([
        {"uid": "f1", "name": "new", "version": "v", "version_count": 1,
         "size": 0, "modified_at": 1767225600, "modified_by": "carol", "owner": "carol"},
        {"uid": "f2", "name": "changed", "version": "v", "version_count": 7,
         "size": 0, "modified_at": 1767225600, "modified_by": "carol", "owner": "carol"},
    ])
    c = make()
    items = c.client.get("/dashboard/activity", headers=_auth("bob")).json()["items"]
    assert items[0]["event_type"] == "created", "one version means the file is new"
    assert items[1]["event_type"] == "updated", "more than one means it changed"


def test_the_core_is_asked_as_the_caller(make, core):
    fake = core([])
    c = make()
    c.client.get("/dashboard/activity?limit=25", headers=_auth("bob"))
    assert fake.calls, "the core was asked"
    assert fake.calls[0]["limit"] == 25, "the caller's limit is passed through, not a fixed page"


def test_timestamps_are_iso_utc(make, core):
    core([{"uid": "f1", "name": "a", "version": "v", "version_count": 2, "size": 0,
           "modified_at": 1767225600, "modified_by": "carol", "owner": "carol"}])
    c = make()
    ts = c.client.get("/dashboard/activity", headers=_auth("bob")).json()["items"][0]["ts"]
    assert ts.startswith("2025-12-31") or ts.startswith("2026-01-01"), ts
    assert ts.endswith("+00:00"), "UTC, explicitly — the SPA renders it in local time"


def test_attention_flags_batch(make):
    c = make(reads=True, store={"mentions": {"f1": 2}}, reviews={"f1": 1, "f2": 3})
    r = c.client.post("/attention/flags", json={"file_uids": ["f1", "f2", "f3"]},
                      headers=_auth("bob"))
    flags = r.json()["flags"]
    assert flags["f1"] == {"mentions": 2, "reviews": 1}
    assert flags["f2"] == {"mentions": 0, "reviews": 3}
    assert "f3" not in flags                       # nothing pending → omitted


def test_attention_flags_empty(make):
    c = make(reads=True)
    assert c.client.post("/attention/flags", json={"file_uids": []},
                         headers=_auth("bob")).json()["flags"] == {}


def test_get_comment_resolves_and_gates(make):
    comment = {"id": "c1", "thread_id": "t1", "author": "carol", "body": "hi", "file_uid": "f1",
               "created_at": "t", "edited_at": None, "deleted": False, "redacted": False}
    c = make(reads={"f1"}, store={"comments": {"c1": comment}})
    assert c.client.get("/comments/c1", headers=_auth("bob")).json()["thread_id"] == "t1"
    assert c.client.get("/comments/missing", headers=_auth("bob")).status_code == 404

    c2 = make(reads=None, store={"comments": {"c1": comment}})
    assert c2.client.get("/comments/c1", headers=_auth("bob")).status_code == 403


# --- share items in the feed (spec §10.6) --------------------------------

def _share_row(**over):
    return {"id": 9, "user_id": "bob", "kind": "share_link_dead", "file_uid": "gone",
            "thread_id": None, "review_id": None, "actor": "system:share",
            "created_at": "t", "read_at": None, "share_link_uid": "l1",
            "detail_text": "Q3 drawings", "source": "sharing", **over}


def test_a_self_contained_share_row_survives_the_read_filter(make):
    """The whole reason detail_text exists.

    "Your link stopped working" is raised precisely BECAUSE the creator lost
    access to the resource — so the per-row READ re-check would suppress the one
    item that most needs to arrive.
    """
    c = make(reads=set(), notes=[_share_row()])   # nothing readable at all
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert [i["id"] for i in items] == [9]
    assert items[0]["detail_text"] == "Q3 drawings"


def test_a_soft_deleted_resource_does_not_hide_a_share_row_either(make):
    c = make(reads=True, live=set(), notes=[_share_row()])
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert [i["id"] for i in items] == [9]


def test_the_exemption_is_for_self_contained_rows_not_for_share_kinds(make):
    """Narrow on purpose: a share row WITHOUT its own text is still filtered.

    A per-kind allowlist would drift the moment a kind is added, and would let a
    row that genuinely needs resolving through unresolved.
    """
    c = make(reads=set(), notes=[_share_row(detail_text=None)])
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert items == []


def test_ordinary_rows_are_still_filtered(make):
    # Guard on the guard: the exemption must not have loosened the normal path.
    rows = [{"id": 1, "user_id": "bob", "kind": "mention", "file_uid": "f1",
             "thread_id": "t1", "review_id": None, "actor": "carol",
             "created_at": "t", "read_at": None, "detail_text": None}]
    c = make(reads=set(), notes=rows)
    assert c.client.get("/dashboard/attention",
                        headers=_auth("bob")).json()["items"] == []


# --- attention rows name their document (owner's request 2026-10-05) ----------

class _NamingCore:
    """stat() as the user: names for the files it will answer for."""
    def __init__(self, names):
        self.names, self.asked, self.closed = names, [], False

    def stat(self, uid):
        self.asked.append(uid)
        if uid not in self.names:
            raise LookupError(uid)
        return types.SimpleNamespace(name=self.names[uid])

    def close(self):
        self.closed = True


def _note(i, kind, uid, **kw):
    return {"id": i, "user_id": "bob", "kind": kind, "file_uid": uid, "thread_id": "t",
            "review_id": None, "actor": "carol@example.com", "created_at": "t", "read_at": None, **kw}


def test_a_mention_carries_the_file_name(make, monkeypatch):
    fake = _NamingCore({"f1": "Site plan rev C.pdf"})
    monkeypatch.setattr("discussion.core_client.client_for", lambda ident, cfg: fake)
    c = make(reads=True, live=True, notes=[_note(1, "mention", "f1"), _note(2, "reply", "f1")])
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert [i["file_name"] for i in items] == ["Site plan rev C.pdf", "Site plan rev C.pdf"]
    assert items[0]["actor"] == "carol@example.com"           # who, as before
    assert fake.asked == ["f1"] and fake.closed               # one lookup per file, one connection


def test_a_name_the_core_will_not_give_is_absent_not_an_error(make, monkeypatch):
    fake = _NamingCore({})
    monkeypatch.setattr("discussion.core_client.client_for", lambda ident, cfg: fake)
    c = make(reads=True, live=True, notes=[_note(1, "mention", "f9")])
    r = c.client.get("/dashboard/attention", headers=_auth("bob"))
    assert r.status_code == 200
    assert r.json()["items"][0]["file_name"] is None


def test_a_share_row_is_never_resolved(make, monkeypatch):
    # Raised because the creator may have LOST access — resolving it would leak
    # (see the READ re-check note in the route).
    fake = _NamingCore({"f1": "secret.pdf"})
    monkeypatch.setattr("discussion.core_client.client_for", lambda ident, cfg: fake)
    c = make(reads=False, live=False,
             notes=[_note(1, "share_link_dead", "f1", detail_text="Q3 drawings")])
    items = c.client.get("/dashboard/attention", headers=_auth("bob")).json()["items"]
    assert "file_name" not in items[0] and fake.asked == []

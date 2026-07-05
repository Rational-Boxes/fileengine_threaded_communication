"""Dashboard feeds, attention flags & comment resolve (M4a) — hermetic."""
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

    def _make(*, reads=True, notes=None, activity=None, store=None, reviews=None):
        notes = FakeNotes(notes or [])
        store = FakeStoreD(**(store or {}))
        app = build_app(Config(), permissions=FakePerms(reads=reads), notifications=notes,
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


def test_mark_seen(make):
    rows = [{"id": 5, "user_id": "bob", "kind": "mention", "file_uid": "f1", "thread_id": None,
             "review_id": None, "actor": "carol", "created_at": "t", "read_at": None}]
    c = make(reads=True, notes=rows)
    assert c.client.post("/dashboard/attention/5/seen", headers=_auth("bob")).json()["seen"] is True
    assert c.notes.seen == [5]
    # already seen → False
    assert c.client.post("/dashboard/attention/5/seen", headers=_auth("bob")).json()["seen"] is False


def test_activity_feed_is_acl_filtered(make):
    rows = [{"id": 1, "file_uid": "f1", "event_type": "updated", "version": "v2", "name": "a",
             "path": "/a", "actor": "carol", "ts": "t"},
            {"id": 2, "file_uid": "fX", "event_type": "created", "version": "", "name": "b",
             "path": "/b", "actor": "carol", "ts": "t"}]
    c = make(reads={"f1"}, activity=rows)
    items = c.client.get("/dashboard/activity", headers=_auth("bob")).json()["items"]
    assert [i["file_uid"] for i in items] == ["f1"]


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

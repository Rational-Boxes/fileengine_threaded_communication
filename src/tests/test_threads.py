"""Threads & comments HTTP surface (M1) — hermetic.

The DB repository and the core permission checker are replaced with in-memory
fakes injected via build_app(), so these tests exercise the API + authorization
logic without Postgres or the gRPC core. (SQL correctness is covered by a `live`
test in a later milestone.)
"""
import base64

import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.config import Config
from discussion.ldap_auth import Identity


# ------------------------------- fakes -------------------------------------
class FakeStore:
    def __init__(self):
        self.threads: dict = {}
        self.comments: dict = {}
        self._n = 0

    def _id(self, p):
        self._n += 1
        return f"{p}{self._n}"

    def _view(self, c):
        body = "" if (c["deleted"] or c["redacted"]) else c["body"]
        return {"id": c["id"], "thread_id": c["thread_id"], "author": c["author"], "body": body,
                "created_at": c["created_at"], "edited_at": c["edited_at"], "deleted": c["deleted"],
                "redacted": c["redacted"], "redacted_by": None, "redacted_reason": None}

    def create_thread(self, tenant, *, file_uid, version, title, body, body_text, opened_by):
        tid, cid = self._id("t"), self._id("c")
        self.threads[tid] = {"id": tid, "file_uid": file_uid, "version": version, "title": title,
                             "status": "open", "resolved_by": None, "resolved_version": None,
                             "opened_by": opened_by, "created_at": "t0", "updated_at": "t0",
                             "anchor_stale": False}
        self.comments[cid] = {"id": cid, "thread_id": tid, "author": opened_by, "body": body,
                              "body_text": body_text, "created_at": "t0", "edited_at": None,
                              "deleted": False, "redacted": False}
        return self.get_thread(tenant, tid)

    def list_threads(self, tenant, file_uid, *, status=None):
        return [dict(t) for t in self.threads.values()
                if t["file_uid"] == file_uid and (status is None or t["status"] == status)]

    def thread_meta(self, tenant, thread_id):
        t = self.threads.get(thread_id)
        return None if t is None else {"id": t["id"], "file_uid": t["file_uid"],
                                       "opened_by": t["opened_by"], "status": t["status"]}

    def get_thread(self, tenant, thread_id):
        t = self.threads.get(thread_id)
        if t is None:
            return None
        d = dict(t)
        d["comments"] = [self._view(c) for c in self.comments.values() if c["thread_id"] == thread_id]
        return d

    def set_thread_status(self, tenant, thread_id, *, status, resolved_by, resolved_version):
        t = self.threads.get(thread_id)
        if t is None:
            return None
        t.update(status=status, resolved_by=resolved_by, resolved_version=resolved_version)
        return self.get_thread(tenant, thread_id)

    def add_comment(self, tenant, thread_id, *, author, body, body_text):
        cid = self._id("c")
        self.comments[cid] = {"id": cid, "thread_id": thread_id, "author": author, "body": body,
                              "body_text": body_text, "created_at": "t1", "edited_at": None,
                              "deleted": False, "redacted": False}
        return self.get_comment(tenant, cid)

    def get_comment(self, tenant, comment_id):
        c = self.comments.get(comment_id)
        if c is None:
            return None
        v = self._view(c)
        v["file_uid"] = self.threads[c["thread_id"]]["file_uid"]
        return v

    def edit_comment(self, tenant, comment_id, *, body, body_text):
        c = self.comments.get(comment_id)
        if c is None or c["deleted"] or c["redacted"]:
            return None
        c.update(body=body, body_text=body_text, edited_at="t2")
        return self.get_comment(tenant, comment_id)

    def soft_delete_comment(self, tenant, comment_id):
        c = self.comments.get(comment_id)
        if c is None or c["deleted"]:
            return False
        c.update(deleted=True, body_text="")
        return True


class FakePerms:
    """reads/writes: True (all), None (none), or a set of allowed file_uids."""
    def __init__(self, reads=True, writes=None):
        self.reads, self.writes = reads, writes

    @staticmethod
    def _ok(allow, file_uid):
        return True if allow is True else (False if not allow else file_uid in allow)

    def can_read(self, ident, file_uid):
        return self._ok(self.reads, file_uid)

    def can_write(self, ident, file_uid):
        return self._ok(self.writes, file_uid)


# ------------------------------ fixtures -----------------------------------
KNOWN = {"bob", "carol", "admin"}


def _fake_auth(cfg, username, password):
    if password != "pw" or username not in KNOWN:
        return Identity(user=username, tenant=cfg.tenant, authenticated=False)
    roles = ["administrators", "system_admin"] if username == "admin" else ["users"]
    return Identity(user=username, roles=roles, tenant=cfg.tenant, authenticated=True)


def _auth(user):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()}


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)

    def _make(reads=True, writes=None):
        store = FakeStore()
        app = build_app(Config(), store=store, permissions=FakePerms(reads=reads, writes=writes))
        return TestClient(app), store
    return _make


# ------------------------------- tests -------------------------------------
def test_open_thread_requires_read(make_client):
    client, _ = make_client(reads=None)
    r = client.post("/files/f1/threads", json={"body": "hi"}, headers=_auth("bob"))
    assert r.status_code == 403


def test_open_list_and_get_thread(make_client):
    client, _ = make_client(reads=True)
    r = client.post("/files/f1/threads", json={"title": "Q", "body": "**hi** there"},
                    headers=_auth("bob"))
    assert r.status_code == 201
    thread = r.json()
    assert thread["file_uid"] == "f1" and thread["status"] == "open"
    assert thread["opened_by"] == "bob"
    assert len(thread["comments"]) == 1 and thread["comments"][0]["body"] == "**hi** there"

    lst = client.get("/files/f1/threads", headers=_auth("carol"))
    assert lst.status_code == 200 and len(lst.json()["threads"]) == 1

    got = client.get(f"/threads/{thread['id']}", headers=_auth("carol"))
    assert got.status_code == 200 and len(got.json()["comments"]) == 1


def test_get_thread_read_gated_and_404(make_client):
    client, _ = make_client(reads=True)
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    assert client.get("/threads/does-not-exist", headers=_auth("bob")).status_code == 404
    # Now a client that can't read anything:
    client2, store2 = make_client(reads=None)
    store2.threads.update({"tX": {"id": "tX", "file_uid": "f9", "opened_by": "bob",
                                  "status": "open", "version": "", "title": "", "resolved_by": None,
                                  "resolved_version": None, "created_at": "t", "updated_at": "t",
                                  "anchor_stale": False}})
    assert client2.get("/threads/tX", headers=_auth("carol")).status_code == 403


def test_reply(make_client):
    client, _ = make_client(reads=True)
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = client.post(f"/threads/{tid}/comments", json={"body": "a reply"}, headers=_auth("carol"))
    assert r.status_code == 201 and r.json()["author"] == "carol"
    assert len(client.get(f"/threads/{tid}", headers=_auth("bob")).json()["comments"]) == 2


def test_resolve_by_opener_without_write(make_client):
    client, _ = make_client(reads=True, writes=None)
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = client.patch(f"/threads/{tid}", json={"status": "resolved", "resolved_version": "v2"},
                     headers=_auth("bob"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved" and body["resolved_by"] == "bob"
    assert body["resolved_version"] == "v2"


def test_resolve_forbidden_for_non_opener_without_write(make_client):
    client, _ = make_client(reads=True, writes=None)
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = client.patch(f"/threads/{tid}", json={"status": "resolved"}, headers=_auth("carol"))
    assert r.status_code == 403


def test_resolve_allowed_for_writer(make_client):
    client, _ = make_client(reads=True, writes={"f1"})
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = client.patch(f"/threads/{tid}", json={"status": "resolved"}, headers=_auth("carol"))
    assert r.status_code == 200 and r.json()["resolved_by"] == "carol"


def test_edit_own_comment_only(make_client):
    client, store = make_client(reads=True)
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    cid = client.post(f"/threads/{tid}/comments", json={"body": "orig"},
                      headers=_auth("carol")).json()["id"]
    # Author edits.
    ok = client.patch(f"/comments/{cid}", json={"body": "edited"}, headers=_auth("carol"))
    assert ok.status_code == 200 and ok.json()["body"] == "edited" and ok.json()["edited_at"]
    # Someone else cannot.
    assert client.patch(f"/comments/{cid}", json={"body": "hax"},
                        headers=_auth("bob")).status_code == 403


def test_delete_own_comment_masks_body(make_client):
    client, _ = make_client(reads=True)
    tid = client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    cid = client.post(f"/threads/{tid}/comments", json={"body": "secret"},
                      headers=_auth("carol")).json()["id"]
    assert client.delete(f"/comments/{cid}", headers=_auth("bob")).status_code == 403
    assert client.delete(f"/comments/{cid}", headers=_auth("carol")).json()["deleted"] is True
    comments = client.get(f"/threads/{tid}", headers=_auth("bob")).json()["comments"]
    deleted = [c for c in comments if c["id"] == cid][0]
    assert deleted["deleted"] is True and deleted["body"] == ""


def test_body_validation(make_client):
    client, _ = make_client(reads=True)
    assert client.post("/files/f1/threads", json={"body": "   "},
                       headers=_auth("bob")).status_code == 422
    big = "x" * (Config().max_comment_chars + 1)
    assert client.post("/files/f1/threads", json={"body": big},
                       headers=_auth("bob")).status_code == 422

"""Threads, comments, mentions & moderation HTTP surface (M1 + M2) — hermetic.

DB / core / LDAP / Redis are all replaced with in-memory fakes injected via
build_app(), so these exercise the API + authorization + mention-safety + event
emission without any live service.
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
        self.threads, self.comments, self.mentions, self.revisions = {}, {}, [], {}
        self._n = 0

    def _id(self, p):
        self._n += 1
        return f"{p}{self._n}"

    def _view(self, c):
        body = "" if (c["deleted"] or c["redacted"]) else c["body"]
        return {"id": c["id"], "thread_id": c["thread_id"],
                "parent_comment_id": c.get("parent_comment_id"),
                "author": c["author"], "body": body,
                "created_at": c["created_at"], "edited_at": c["edited_at"], "deleted": c["deleted"],
                "redacted": c["redacted"], "redacted_by": c.get("redacted_by"),
                "redacted_reason": c.get("redacted_reason")}

    def create_thread(self, tenant, *, file_uid, version, title, body, body_text, opened_by):
        tid, cid = self._id("t"), self._id("c")
        self.threads[tid] = {"id": tid, "file_uid": file_uid, "version": version, "title": title,
                             "status": "open", "resolved_by": None, "resolved_version": None,
                             "opened_by": opened_by, "created_at": "t0", "updated_at": "t0",
                             "anchor_stale": False}
        self.comments[cid] = {"id": cid, "thread_id": tid, "author": opened_by, "body": body,
                              "body_text": body_text, "created_at": "t0", "edited_at": None,
                              "deleted": False, "redacted": False, "parent_comment_id": None}
        return self.get_thread(tenant, tid)

    def list_threads(self, tenant, file_uid, *, status=None):
        out = []
        for t in self.threads.values():
            if t["file_uid"] == file_uid and (status is None or t["status"] == status):
                row = dict(t)
                row["comments"] = [self._view(c) for c in self.comments.values()
                                   if c["thread_id"] == t["id"]]
                out.append(row)
        return out

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

    def add_comment(self, tenant, thread_id, *, author, body, body_text, parent_comment_id=None):
        cid = self._id("c")
        self.comments[cid] = {"id": cid, "thread_id": thread_id, "author": author, "body": body,
                              "body_text": body_text, "created_at": "t1", "edited_at": None,
                              "deleted": False, "redacted": False,
                              "parent_comment_id": parent_comment_id}
        return self.get_comment(tenant, cid)

    def comment_parent_thread(self, tenant, comment_id):
        c = self.comments.get(comment_id)
        return c["thread_id"] if c else None

    def list_revisions(self, tenant, comment_id):
        return list(self.revisions.get(comment_id, []))

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
        self.revisions.setdefault(comment_id, []).insert(0, {"body": c["body"], "edited_at": "t1"})
        c.update(body=body, body_text=body_text, edited_at="t2")
        return self.get_comment(tenant, comment_id)

    def soft_delete_comment(self, tenant, comment_id):
        c = self.comments.get(comment_id)
        if c is None or c["deleted"]:
            return False
        c.update(deleted=True, body_text="")
        return True

    def thread_participants(self, tenant, thread_id):
        users = set()
        t = self.threads.get(thread_id)
        if t:
            users.add(t["opened_by"])
        for c in self.comments.values():
            if c["thread_id"] == thread_id:
                users.add(c["author"])
        return list(users)

    def add_mention(self, tenant, *, comment_id, thread_id, target_user):
        self.mentions.append({"comment_id": comment_id, "thread_id": thread_id,
                              "target_user": target_user})

    def redact_comment(self, tenant, comment_id, *, redacted_by, reason):
        c = self.comments.get(comment_id)
        if c is None or c["redacted"]:
            return None
        c.update(redacted=True, redacted_by=redacted_by, redacted_reason=reason, body_text="")
        return self.get_comment(tenant, comment_id)


class FakePerms:
    """reads/writes: True (all), None (none), or a set of allowed file_uids.
    deny_users: uids denied READ regardless (to exercise mention error-marking)."""
    def __init__(self, reads=True, writes=None, deny_users=frozenset()):
        self.reads, self.writes, self.deny_users = reads, writes, set(deny_users)

    @staticmethod
    def _ok(allow, file_uid):
        return True if allow is True else (False if not allow else file_uid in allow)

    def can_read(self, ident, file_uid):
        if ident.user in self.deny_users:
            return False
        return self._ok(self.reads, file_uid)

    def can_write(self, ident, file_uid):
        return self._ok(self.writes, file_uid)


class FakeDirectory:
    def __init__(self, mapping=None):
        # identifier -> uid; unknown identifiers resolve to None.
        self.mapping = mapping or {}

    def resolve_principal(self, identifier):
        uid = self.mapping.get(identifier)
        return None if uid is None else Identity(user=uid, roles=["users"], tenant="default")

    def search(self, q, limit=8):
        ql = (q or "").lower()
        out = [Identity(user=uid, roles=["users"], tenant="default", email=ident)
               for ident, uid in self.mapping.items()
               if ql in ident.lower() or ql in uid.lower()]
        return out[:limit]


class FakeNotes:
    def __init__(self):
        self.items = []

    def add(self, tenant, *, user_id, kind, file_uid, actor, thread_id=None, review_id=None):
        if not user_id or user_id == actor:
            return
        self.items.append({"user_id": user_id, "kind": kind, "file_uid": file_uid,
                           "actor": actor, "thread_id": thread_id, "review_id": review_id})

    def kinds_for(self, user_id):
        return [i["kind"] for i in self.items if i["user_id"] == user_id]


class FakeEvents:
    def __init__(self):
        self.published = []

    def publish(self, etype, **fields):
        evt = {"type": etype, **fields}
        self.published.append(evt)
        return evt

    def types(self):
        return [e["type"] for e in self.published]


class FakeIndexer:
    def __init__(self):
        self.indexed, self.removed = [], []

    def index_comment(self, tenant, *, comment_id, file_uid, thread_id, text):
        self.indexed.append({"comment_id": comment_id, "file_uid": file_uid,
                             "thread_id": thread_id, "text": text})
        return 1

    def remove_comment(self, tenant, comment_id):
        self.removed.append(comment_id)


class Ctx:
    def __init__(self, client, store, notes, events, directory, indexer):
        self.client, self.store, self.notes = client, store, notes
        self.events, self.directory, self.indexer = events, directory, indexer


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
def make(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)

    def _make(reads=True, writes=None, deny_users=frozenset(), directory=None):
        store, notes, events, indexer = FakeStore(), FakeNotes(), FakeEvents(), FakeIndexer()
        directory = directory or FakeDirectory()
        app = build_app(Config(), store=store,
                        permissions=FakePerms(reads=reads, writes=writes, deny_users=deny_users),
                        directory=directory, events=events, notifications=notes,
                        reviews=object(), indexer=indexer)
        return Ctx(TestClient(app), store, notes, events, directory, indexer)
    return _make


# ------------------------------- M1 tests ----------------------------------
def test_open_thread_requires_read(make):
    c = make(reads=None)
    assert c.client.post("/files/f1/threads", json={"body": "hi"},
                         headers=_auth("bob")).status_code == 403


def test_open_list_and_get_thread(make):
    c = make(reads=True)
    r = c.client.post("/files/f1/threads", json={"title": "Q", "body": "**hi** there"},
                      headers=_auth("bob"))
    assert r.status_code == 201
    thread = r.json()
    assert thread["file_uid"] == "f1" and thread["opened_by"] == "bob"
    assert len(thread["comments"]) == 1 and thread["comments"][0]["body"] == "**hi** there"
    assert "thread.opened" in c.events.types() and "comment.created" in c.events.types()

    assert len(c.client.get("/files/f1/threads", headers=_auth("carol")).json()["threads"]) == 1
    got = c.client.get(f"/threads/{thread['id']}", headers=_auth("carol"))
    assert got.status_code == 200 and len(got.json()["comments"]) == 1


def test_get_thread_read_gated_and_404(make):
    c = make(reads=None)
    c.store.threads["tX"] = {"id": "tX", "file_uid": "f9", "opened_by": "bob", "status": "open",
                             "version": "", "title": "", "resolved_by": None,
                             "resolved_version": None, "created_at": "t", "updated_at": "t",
                             "anchor_stale": False}
    assert c.client.get("/threads/tX", headers=_auth("carol")).status_code == 403
    assert c.client.get("/threads/nope", headers=_auth("carol")).status_code == 404


def test_reply_and_participant_notification(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = c.client.post(f"/threads/{tid}/comments", json={"body": "a reply"}, headers=_auth("carol"))
    assert r.status_code == 201 and r.json()["author"] == "carol"
    # bob (opener) gets a 'reply' notification; carol (actor) does not.
    assert "reply" in c.notes.kinds_for("bob")
    assert c.notes.kinds_for("carol") == []


def test_resolve_by_opener_notifies_and_emits(make):
    c = make(reads=True, writes=None)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    c.client.post(f"/threads/{tid}/comments", json={"body": "r"}, headers=_auth("carol"))
    r = c.client.patch(f"/threads/{tid}", json={"status": "resolved", "resolved_version": "v2"},
                       headers=_auth("bob"))
    assert r.status_code == 200 and r.json()["resolved_by"] == "bob"
    assert "thread_resolved" in c.notes.kinds_for("carol")
    assert "thread.resolved" in c.events.types()


def test_resolve_forbidden_for_non_opener_without_write(make):
    c = make(reads=True, writes=None)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    assert c.client.patch(f"/threads/{tid}", json={"status": "resolved"},
                          headers=_auth("carol")).status_code == 403


def test_resolve_allowed_for_writer(make):
    c = make(reads=True, writes={"f1"})
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = c.client.patch(f"/threads/{tid}", json={"status": "resolved"}, headers=_auth("carol"))
    assert r.status_code == 200 and r.json()["resolved_by"] == "carol"


def test_edit_and_delete_own_comment(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    cid = c.client.post(f"/threads/{tid}/comments", json={"body": "orig"},
                        headers=_auth("carol")).json()["id"]
    assert c.client.patch(f"/comments/{cid}", json={"body": "edited"},
                          headers=_auth("carol")).json()["body"] == "edited"
    assert c.client.patch(f"/comments/{cid}", json={"body": "hax"},
                          headers=_auth("bob")).status_code == 403
    assert c.client.delete(f"/comments/{cid}", headers=_auth("bob")).status_code == 403
    assert c.client.delete(f"/comments/{cid}", headers=_auth("carol")).json()["deleted"] is True
    comments = c.client.get(f"/threads/{tid}", headers=_auth("bob")).json()["comments"]
    assert [x for x in comments if x["id"] == cid][0]["body"] == ""


def test_body_validation(make):
    c = make(reads=True)
    assert c.client.post("/files/f1/threads", json={"body": "  "},
                         headers=_auth("bob")).status_code == 422
    big = "x" * (Config().max_comment_chars + 1)
    assert c.client.post("/files/f1/threads", json={"body": big},
                         headers=_auth("bob")).status_code == 422


# ------------------------------- M2 tests ----------------------------------
def test_mention_valid_notifies_and_emits(make):
    c = make(reads=True, directory=FakeDirectory({"carol@x": "carol"}))
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = c.client.post(f"/threads/{tid}/comments",
                      json={"body": "ping", "mentions": ["carol@x"]}, headers=_auth("bob"))
    assert r.status_code == 201
    assert "mention" in c.notes.kinds_for("carol")
    assert "mention.created" in c.events.types()
    assert c.store.mentions and c.store.mentions[0]["target_user"] == "carol"


def test_mention_invalid_is_error_marked(make):
    # carol resolves but is denied READ; dave doesn't resolve at all -> both invalid.
    c = make(reads=True, deny_users={"carol"},
             directory=FakeDirectory({"carol@x": "carol"}))
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    r = c.client.post(f"/threads/{tid}/comments",
                      json={"body": "ping", "mentions": ["carol@x", "dave@x"]}, headers=_auth("bob"))
    assert r.status_code == 422
    invalid = r.json()["detail"]["invalid_mentions"]
    assert set(invalid) == {"carol@x", "dave@x"}
    # No comment/mention was persisted (only the opening comment remains).
    assert len(c.client.get(f"/threads/{tid}", headers=_auth("bob")).json()["comments"]) == 1
    assert c.store.mentions == []


def test_comment_indexed_on_write_and_deindexed_on_delete(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "first"}, headers=_auth("bob")).json()["id"]
    assert any(x["text"] == "first" for x in c.indexer.indexed)   # opening comment indexed
    cid = c.client.post(f"/threads/{tid}/comments", json={"body": "reply text"},
                        headers=_auth("carol")).json()["id"]
    assert any(x["comment_id"] == cid for x in c.indexer.indexed)
    c.client.delete(f"/comments/{cid}", headers=_auth("carol"))
    assert cid in c.indexer.removed


def test_list_threads_embeds_comments(make):
    # The panel reloads from the list endpoint; it must carry each thread's comments.
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "root msg"}, headers=_auth("bob")).json()["id"]
    c.client.post(f"/threads/{tid}/comments", json={"body": "a reply"}, headers=_auth("carol"))
    threads = c.client.get("/files/f1/threads", headers=_auth("bob")).json()["threads"]
    t = next(x for x in threads if x["id"] == tid)
    assert [cm["body"] for cm in t["comments"]] == ["root msg", "a reply"]


def test_nested_reply(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "root"}, headers=_auth("bob")).json()["id"]
    root_cid = c.client.get(f"/threads/{tid}", headers=_auth("bob")).json()["comments"][0]["id"]
    r = c.client.post(f"/threads/{tid}/comments",
                      json={"body": "a reply", "parent_comment_id": root_cid}, headers=_auth("carol"))
    assert r.status_code == 201 and r.json()["parent_comment_id"] == root_cid
    # A parent from a different thread is rejected.
    tid2 = c.client.post("/files/f2/threads", json={"body": "other"}, headers=_auth("bob")).json()["id"]
    other_cid = c.client.get(f"/threads/{tid2}", headers=_auth("bob")).json()["comments"][0]["id"]
    assert c.client.post(f"/threads/{tid}/comments",
                         json={"body": "x", "parent_comment_id": other_cid},
                         headers=_auth("bob")).status_code == 422


def test_comment_revisions(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    cid = c.client.post(f"/threads/{tid}/comments", json={"body": "first"},
                        headers=_auth("carol")).json()["id"]
    c.client.patch(f"/comments/{cid}", json={"body": "second"}, headers=_auth("carol"))
    revs = c.client.get(f"/comments/{cid}/revisions", headers=_auth("bob")).json()["revisions"]
    assert [r["body"] for r in revs] == ["first"]   # prior version retained


def test_mentionable_autocomplete(make):
    c = make(reads=True, directory=FakeDirectory({"carol@x": "carol", "dave@x": "dave"}))
    users = c.client.get("/files/f1/mentionable?q=car", headers=_auth("bob")).json()["users"]
    assert [u["user"] for u in users] == ["carol"]

    # A candidate who can't read the file is filtered out.
    c2 = make(reads=True, deny_users={"carol"}, directory=FakeDirectory({"carol@x": "carol"}))
    assert c2.client.get("/files/f1/mentionable?q=car", headers=_auth("bob")).json()["users"] == []


def test_thread_provenance_endpoint(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    c.client.patch(f"/threads/{tid}", json={"status": "resolved", "resolved_version": "v9"},
                   headers=_auth("bob"))
    p = c.client.get(f"/threads/{tid}/provenance", headers=_auth("carol")).json()
    assert p["source_type"] == "discussion_thread"
    assert p["resolved_version"] == "v9"
    assert p["permalink"].endswith(f"/preview/f1?thread={tid}")


def test_redaction_admin_only(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "x"}, headers=_auth("bob")).json()["id"]
    cid = c.client.post(f"/threads/{tid}/comments", json={"body": "sensitive"},
                        headers=_auth("carol")).json()["id"]
    # Non-admin cannot redact.
    assert c.client.post(f"/comments/{cid}/redact", json={"reason": "pii"},
                         headers=_auth("bob")).status_code == 403
    # Admin redacts -> masked.
    r = c.client.post(f"/comments/{cid}/redact", json={"reason": "pii"}, headers=_auth("admin"))
    assert r.status_code == 200 and r.json()["redacted"] is True and r.json()["body"] == ""
    # Redacted body no longer visible in the thread.
    shown = c.client.get(f"/threads/{tid}", headers=_auth("bob")).json()["comments"]
    assert [x for x in shown if x["id"] == cid][0]["body"] == ""
    # Second redact -> already redacted -> 404.
    assert c.client.post(f"/comments/{cid}/redact", json={}, headers=_auth("admin")).status_code == 404

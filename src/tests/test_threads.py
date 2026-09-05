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
from discussion.targets import validate_targets


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
                "redacted_reason": c.get("redacted_reason"),
                "viewpoint_ref": c.get("viewpoint_ref"),
                "markup": c.get("markup")}

    def create_thread(self, tenant, *, file_uid, version, title, body, body_text, opened_by,
                      anchor=None, markup=None):
        tid, cid = self._id("t"), self._id("c")
        self.threads[tid] = {"id": tid, "file_uid": file_uid, "version": version, "title": title,
                             "status": "open", "resolved_by": None, "resolved_version": None,
                             "opened_by": opened_by, "created_at": "t0", "updated_at": "t0",
                             "anchor_stale": False, "anchor": anchor}
        self.comments[cid] = {"id": cid, "thread_id": tid, "author": opened_by, "body": body,
                              "body_text": body_text, "created_at": "t0", "edited_at": None,
                              "deleted": False, "redacted": False, "parent_comment_id": None,
                              "markup": markup}
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

    def add_comment(self, tenant, thread_id, *, author, body, body_text, parent_comment_id=None,
                    viewpoint_ref=None, markup=None):
        cid = self._id("c")
        self.comments[cid] = {"id": cid, "thread_id": thread_id, "author": author, "body": body,
                              "body_text": body_text, "created_at": "t1", "edited_at": None,
                              "deleted": False, "redacted": False,
                              "parent_comment_id": parent_comment_id,
                              "viewpoint_ref": viewpoint_ref, "markup": markup}
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
    """reads/writes/live: True (all), None (none), or a set of allowed file_uids.
    deny_users: uids denied READ regardless (to exercise mention error-marking).
    live: which file_uids are still present (not soft-deleted); True = all live."""
    def __init__(self, reads=True, writes=None, deny_users=frozenset(), live=True,
                 file_tenant=None):
        self.reads, self.writes, self.deny_users = reads, writes, set(deny_users)
        self.live = live
        # Which tenant the anchor lives in. The real check scopes its core client
        # by identity.tenant, so a principal stamped with the wrong tenant asks a
        # schema the file is not in and is refused. None = don't model it.
        self.file_tenant = file_tenant

    @staticmethod
    def _ok(allow, file_uid):
        return True if allow is True else (False if not allow else file_uid in allow)

    def can_read(self, ident, file_uid):
        if ident.user in self.deny_users:
            return False
        if self.file_tenant is not None and ident.tenant != self.file_tenant:
            return False        # wrong schema — exactly the production failure
        return self._ok(self.reads, file_uid)

    def can_write(self, ident, file_uid):
        return self._ok(self.writes, file_uid)

    def is_live(self, ident, file_uid):
        return self._ok(self.live, file_uid)


class FakeDirectory:
    """Members of the tenant it is ASKED for, as the real directory does.

    It used to hard-code "default", which is why the tenant defect was invisible
    here: the fake answered in the configured tenant no matter what the request
    said, and FakePerms did not look at the tenant at all. Between them they
    modelled the one arrangement in which the bug cannot appear."""
    def __init__(self, mapping=None, members=None):
        # identifier -> uid; unknown identifiers resolve to None.
        self.mapping = mapping or {}
        # uid -> the tenants they belong to. Default: every tenant, which keeps
        # the older tests reading as before. A uid listed here is offered ONLY in
        # the tenants named, because the real directory refuses a candidate with
        # no roles under ou=<tenant>.
        self.members = members or {}

    def _member(self, uid, tenant):
        allowed = self.members.get(uid)
        return True if allowed is None else (tenant or "default") in allowed

    def resolve_principal(self, identifier, tenant=None):
        uid = self.mapping.get(identifier)
        if uid is None or not self._member(uid, tenant):
            return None
        return Identity(user=uid, roles=["users"], tenant=tenant or "default")

    def search(self, q, limit=8, tenant=None):
        ql = (q or "").lower()
        out = [Identity(user=uid, roles=["users"], tenant=tenant or "default", email=ident)
               for ident, uid in self.mapping.items()
               if (ql in ident.lower() or ql in uid.lower()) and self._member(uid, tenant)]
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

    def _make(reads=True, writes=None, deny_users=frozenset(), directory=None,
              file_tenant=None):
        store, notes, events, indexer = FakeStore(), FakeNotes(), FakeEvents(), FakeIndexer()
        directory = directory or FakeDirectory()
        app = build_app(Config(), store=store,
                        permissions=FakePerms(reads=reads, writes=writes, deny_users=deny_users,
                                              file_tenant=file_tenant),
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


# --- the tenant the check runs in (production defect, 2026-09-05) -----------
#
# Both halves of @mention resolved the target into the SERVICE's configured
# tenant instead of the request's, so the READ check asked a schema the file was
# not in. On the deployment (configured tenant `default`, users in `arcdigital`)
# autocomplete answered 200 with an empty list — indistinguishable from "nobody
# matched" — and posting a mention failed 422. It worked only on the one tenant
# that happened to match the config, which is the tenant every test used.
TENANT_HDR = {"X-Tenant": "arcdigital"}


def test_mentionable_resolves_in_the_requested_tenant(make):
    c = make(reads=True, file_tenant="arcdigital",
             directory=FakeDirectory({"carol@x": "carol"}))
    r = c.client.get("/files/f1/mentionable?q=car",
                     headers={**_auth("bob"), **TENANT_HDR})
    assert r.status_code == 200
    assert [u["user"] for u in r.json()["users"]] == ["carol"]


def test_mention_can_be_posted_in_a_non_default_tenant(make):
    c = make(reads=True, file_tenant="arcdigital",
             directory=FakeDirectory({"carol@x": "carol"}))
    r = c.client.post("/files/f1/threads",
                      json={"body": "look at this @carol@x", "mentions": ["carol@x"]},
                      headers={**_auth("bob"), **TENANT_HDR})
    assert r.status_code == 201, r.json()          # not 422 "cannot access this file"
    assert c.notes.kinds_for("carol") == ["mention"]   # ...and they are flagged


def test_reviewers_resolve_in_the_requested_tenant(make):
    # Same primitive, same defect: the reviewer picker filters by who-can-read.
    c = make(reads=True, file_tenant="arcdigital",
             directory=FakeDirectory({"carol@x": "carol"}))
    valid, invalid = validate_targets(c.directory, FakePerms(reads=True, file_tenant="arcdigital"),
                                      "f1", ["carol@x"], "arcdigital")
    assert invalid == [] and [p.user for _i, p in valid] == ["carol"]


# --- members only (production disclosure, 2026-09-05) -----------------------
#
# The LDAP user base is shared by every tenant, and the substring match alone
# returns strangers. On the deployment a search for "a" on one tenant offered
# seven people, three of them members of a different tenant — names and email
# addresses shown to users with no relationship to them.
#
# The ACL filter does not cover this: the core is read-by-default, so a
# role-less outsider passes a READ check on any document without a deny rule.
# Membership is established in the directory, by having roles in that tenant.
def test_autocomplete_offers_only_members_of_the_active_tenant(make):
    directory = FakeDirectory({"carol@x": "carol", "outsider@x": "outsider"},
                              members={"carol": {"arcdigital"}, "outsider": {"default"}})
    c = make(reads=True, file_tenant="arcdigital", directory=directory)

    users = c.client.get("/files/f1/mentionable?q=outsider",
                         headers={**_auth("bob"), **TENANT_HDR}).json()["users"]
    assert users == []                               # matches the query, is not a member

    users = c.client.get("/files/f1/mentionable?q=car",
                         headers={**_auth("bob"), **TENANT_HDR}).json()["users"]
    assert [u["user"] for u in users] == ["carol"]   # ...a member still is

    # And the outsider is absent from a query that matches them BOTH, which is
    # the shape the disclosure actually took — a broad prefix, a mixed list.
    users = c.client.get("/files/f1/mentionable?q=o",
                         headers={**_auth("bob"), **TENANT_HDR}).json()["users"]
    assert "outsider" not in [u["user"] for u in users]


def test_a_non_member_cannot_be_mentioned_even_if_named_directly(make):
    # Defence in depth: the dropdown is a convenience, the address bar is not.
    directory = FakeDirectory({"outsider@x": "outsider"}, members={"outsider": {"default"}})
    c = make(reads=True, file_tenant="arcdigital", directory=directory)

    r = c.client.post("/files/f1/threads",
                      json={"body": "hi @outsider@x", "mentions": ["outsider@x"]},
                      headers={**_auth("bob"), **TENANT_HDR})

    assert r.status_code == 422
    assert r.json()["detail"]["invalid_mentions"] == ["outsider@x"]
    assert c.notes.kinds_for("outsider") == []       # and nothing was flagged to them


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


# ----------------------- V2 anchor / viewpoint (§5.4) ----------------------
class FakeLive:
    """Records live-hub broadcasts so tests can assert the 3D-marker fan-out."""
    def __init__(self):
        self.messages = []  # (tenant, file_uid, message)

    async def broadcast(self, tenant, file_uid, message):
        self.messages.append((tenant, file_uid, message))


_ANCHOR = {"kind": "model-viewpoint", "schema": "fileengine.anchor.v1",
           "viewpoint": {"perspective_camera": {}}, "marker": {"x": 1, "y": 2, "z": 3}}


def test_open_thread_with_anchor_roundtrips_and_syncs_live(make):
    c = make(reads=True)
    live = FakeLive()
    c.client.app.state.live = live
    r = c.client.post("/files/f1/threads",
                      json={"body": "see this clash", "anchor": _ANCHOR}, headers=_auth("bob"))
    assert r.status_code == 201
    assert r.json()["anchor"] == _ANCHOR                       # persisted + returned
    # the live fan-out carries the anchor so an open 3D viewer renders the marker
    assert any(m.get("anchor") == _ANCHOR for _, _, m in live.messages)
    # and the envelope event surfaces it for digest / cross-channel bridges
    opened = [e for e in c.events.published if e["type"] == "thread.opened"]
    assert opened and opened[0].get("anchor") == _ANCHOR


def test_open_thread_without_anchor_is_null(make):
    """Regression: a plain comment thread has a null anchor and unchanged live shape."""
    c = make(reads=True)
    live = FakeLive()
    c.client.app.state.live = live
    r = c.client.post("/files/f1/threads", json={"body": "just a note"}, headers=_auth("bob"))
    assert r.status_code == 201
    assert r.json()["anchor"] is None
    assert all(m.get("anchor") is None for _, _, m in live.messages)
    opened = [e for e in c.events.published if e["type"] == "thread.opened"]
    assert opened and opened[0].get("anchor") is None          # null (real envelope omits it; see make_event test)


def test_make_event_omits_null_anchor_but_includes_present():
    """The real envelope carries anchor only when set (§5.4)."""
    from discussion.events import make_event
    assert "anchor" not in make_event("thread.opened", tenant="t")   # null -> omitted
    assert make_event("thread.opened", tenant="t", anchor=_ANCHOR)["anchor"] == _ANCHOR


def test_comment_viewpoint_ref_roundtrips(make):
    c = make(reads=True)
    tid = c.client.post("/files/f1/threads", json={"body": "hi"},
                        headers=_auth("bob")).json()["id"]
    r = c.client.post(f"/threads/{tid}/comments",
                      json={"body": "pinned to a view", "viewpoint_ref": "vp-7"},
                      headers=_auth("bob"))
    assert r.status_code == 201
    assert r.json()["viewpoint_ref"] == "vp-7"
    # An ordinary comment stays unpinned.
    r2 = c.client.post(f"/threads/{tid}/comments", json={"body": "plain"}, headers=_auth("bob"))
    assert r2.json()["viewpoint_ref"] is None


def test_comment_markup_roundtrips(make):
    """Phase 7.1: a marked-up-PDF pointer round-trips on both the opening comment
    and replies; a plain comment carries none."""
    c = make(reads=True)
    _markup = {"rendition_uid": "rend-9", "name": "spec-bob-2026.pdf", "page": 3}
    # On the opening comment (thread root).
    opened = c.client.post("/files/f1/threads",
                           json={"body": "marked it up", "markup": _markup},
                           headers=_auth("bob")).json()
    assert opened["comments"][0]["markup"] == _markup
    tid = opened["id"]
    # On a reply.
    r = c.client.post(f"/threads/{tid}/comments",
                      json={"body": "and here too", "markup": _markup},
                      headers=_auth("bob"))
    assert r.status_code == 201
    assert r.json()["markup"] == _markup
    # A plain comment carries no markup.
    r2 = c.client.post(f"/threads/{tid}/comments", json={"body": "plain"}, headers=_auth("bob"))
    assert r2.json()["markup"] is None

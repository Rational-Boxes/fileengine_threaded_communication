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

"""Review-request state machine (M2) — hermetic."""
import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.config import Config

from .test_threads import (FakeDirectory, FakeEvents, FakeNotes, FakePerms, _auth,
                           _fake_auth)


class FakeReviewStore:
    def __init__(self):
        self.rows, self._n = {}, 0

    def create(self, tenant, *, file_uid, version, thread_id, requester, reviewers):
        out = []
        for rv in reviewers:
            self._n += 1
            rid = f"r{self._n}"
            self.rows[rid] = {"id": rid, "file_uid": file_uid, "version": version,
                              "thread_id": thread_id, "requester": requester, "reviewer": rv,
                              "status": "requested", "outcome": None, "created_at": "t0",
                              "acknowledged_at": None, "completed_at": None}
            out.append(dict(self.rows[rid]))
        return out

    def get(self, tenant, rid):
        r = self.rows.get(rid)
        return dict(r) if r else None

    def set_status(self, tenant, rid, *, status, outcome=None):
        r = self.rows.get(rid)
        if r is None:
            return None
        r["status"] = status
        if outcome is not None:
            r["outcome"] = outcome
        if status == "acknowledged":
            r["acknowledged_at"] = "t1"
        if status == "completed":
            r["completed_at"] = "t2"
        return dict(r)

    def list_for(self, tenant, user, *, role="both", status=None):
        out = []
        for r in self.rows.values():
            hit = ((role in ("reviewer", "both") and r["reviewer"] == user) or
                   (role in ("requester", "both") and r["requester"] == user))
            if hit and (status is None or r["status"] == status):
                out.append(dict(r))
        return out

    def list_for_file(self, tenant, file_uid, *, status=None):
        return [dict(r) for r in self.rows.values()
                if r["file_uid"] == file_uid and (status is None or r["status"] == status)]


class Ctx:
    def __init__(self, client, reviews, notes, events):
        self.client, self.reviews, self.notes, self.events = client, reviews, notes, events


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)

    def _make(reads=True, deny_users=frozenset(), directory=None):
        reviews, notes, events = FakeReviewStore(), FakeNotes(), FakeEvents()
        app = build_app(Config(), store=object(),
                        permissions=FakePerms(reads=reads, deny_users=deny_users),
                        directory=directory or FakeDirectory({"carol@x": "carol"}),
                        events=events, notifications=notes, reviews=reviews)
        return Ctx(TestClient(app), reviews, notes, events)
    return _make


def test_raise_review_valid(make):
    c = make(reads=True)
    r = c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"], "version": "v1"},
                      headers=_auth("bob"))
    assert r.status_code == 201
    reviews = r.json()["reviews"]
    assert len(reviews) == 1 and reviews[0]["reviewer"] == "carol" and reviews[0]["status"] == "requested"
    assert "review_requested" in c.notes.kinds_for("carol")
    assert "review.requested" in c.events.types()


def test_raise_review_requires_read_on_file(make):
    c = make(reads=None)
    assert c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"]},
                         headers=_auth("bob")).status_code == 403


def test_list_file_reviews_returns_the_record_for_the_file(make):
    c = make(reads=True)
    c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"]}, headers=_auth("bob"))
    # A reader who is neither requester nor reviewer still sees the file's record.
    r = c.client.get("/files/f1/reviews", headers=_auth("admin"))
    assert r.status_code == 200
    reviews = r.json()["reviews"]
    assert len(reviews) == 1 and reviews[0]["file_uid"] == "f1" and reviews[0]["requester"] == "bob"
    # A different file has no record.
    assert c.client.get("/files/fX/reviews", headers=_auth("admin")).json()["reviews"] == []


def test_list_file_reviews_requires_read(make):
    c = make(reads=None)
    assert c.client.get("/files/f1/reviews", headers=_auth("admin")).status_code == 403


def test_raise_review_error_marks_reviewer_without_access(make):
    c = make(reads=True, deny_users={"carol"})
    r = c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"]}, headers=_auth("bob"))
    assert r.status_code == 422
    assert c.client and r.json()["detail"]["invalid_reviewers"] == ["carol@x"]


def test_raise_review_needs_a_reviewer(make):
    c = make(reads=True)
    assert c.client.post("/files/f1/reviews", json={"reviewers": []},
                         headers=_auth("bob")).status_code == 422


def test_acknowledge_and_complete_flow(make):
    c = make(reads=True)
    rid = c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"]},
                        headers=_auth("bob")).json()["reviews"][0]["id"]

    # Only the assigned reviewer may act.
    assert c.client.post(f"/reviews/{rid}/acknowledge", headers=_auth("bob")).status_code == 403

    ack = c.client.post(f"/reviews/{rid}/acknowledge", headers=_auth("carol"))
    assert ack.status_code == 200 and ack.json()["status"] == "acknowledged"
    assert "review_acknowledged" in c.notes.kinds_for("bob")

    done = c.client.post(f"/reviews/{rid}/complete", json={"outcome": "approved"},
                         headers=_auth("carol"))
    assert done.status_code == 200 and done.json()["status"] == "completed"
    assert done.json()["outcome"] == "approved"
    assert "review_completed" in c.notes.kinds_for("bob")
    assert "review.completed" in c.events.types()


def test_acknowledge_twice_conflicts(make):
    c = make(reads=True)
    rid = c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"]},
                        headers=_auth("bob")).json()["reviews"][0]["id"]
    assert c.client.post(f"/reviews/{rid}/acknowledge", headers=_auth("carol")).status_code == 200
    assert c.client.post(f"/reviews/{rid}/acknowledge", headers=_auth("carol")).status_code == 409


def test_list_reviews_by_role(make):
    c = make(reads=True)
    c.client.post("/files/f1/reviews", json={"reviewers": ["carol@x"]}, headers=_auth("bob"))
    as_requester = c.client.get("/reviews?role=requester", headers=_auth("bob")).json()["reviews"]
    assert len(as_requester) == 1
    as_reviewer = c.client.get("/reviews?role=reviewer", headers=_auth("carol")).json()["reviews"]
    assert len(as_reviewer) == 1
    # bob is not a reviewer on anything.
    assert c.client.get("/reviews?role=reviewer", headers=_auth("bob")).json()["reviews"] == []

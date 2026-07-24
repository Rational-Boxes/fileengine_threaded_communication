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

"""Digest self-service endpoints (§11a / M6) — hermetic."""
import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.config import Config

from .test_threads import _auth, _fake_auth


class FakeDigestStore:
    def __init__(self):
        self.saved = {}

    def get(self, tenant, user):
        return self.saved.get((tenant, user),
                              {"user_id": user, "cadence": "off", "send_hour_local": 8,
                               "send_dow": 1, "timezone": "UTC", "scope": {},
                               "ai_summary": False, "quiet_if_empty": True})

    def upsert(self, tenant, user, **fields):
        sub = {"user_id": user, **fields}
        self.saved[(tenant, user)] = sub
        return sub


class FakeSender:
    def __init__(self):
        self.calls = 0

    def send_now(self, ident):
        self.calls += 1
        return {"sent": True, "items": 2}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)
    app = build_app(Config(), digest_store=FakeDigestStore(), digest_sender=FakeSender())
    return TestClient(app)


def test_get_digest_defaults(client):
    r = client.get("/me/digest", headers=_auth("bob"))
    assert r.status_code == 200 and r.json()["cadence"] == "off"


def test_put_digest_updates(client):
    r = client.put("/me/digest", json={"cadence": "daily", "send_hour_local": 9, "timezone": "UTC"},
                   headers=_auth("bob"))
    assert r.status_code == 200 and r.json()["cadence"] == "daily"
    # persisted
    assert client.get("/me/digest", headers=_auth("bob")).json()["send_hour_local"] == 9


def test_put_digest_validates_cadence(client):
    assert client.put("/me/digest", json={"cadence": "yearly"},
                      headers=_auth("bob")).status_code == 422


def test_put_digest_validates_hour(client):
    assert client.put("/me/digest", json={"cadence": "daily", "send_hour_local": 99},
                      headers=_auth("bob")).status_code == 422


def test_send_now_and_ratelimit(client):
    r = client.post("/me/digest/send-now", headers=_auth("bob"))
    assert r.status_code == 200 and r.json()["sent"] is True
    # immediate second call is rate-limited
    assert client.post("/me/digest/send-now", headers=_auth("bob")).status_code == 429

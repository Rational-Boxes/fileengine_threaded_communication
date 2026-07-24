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

"""HTTP surface (M0) — health, auth/token, whoami. Hermetic: LDAP/DB/core are
stubbed, so no live services are needed."""
import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.config import Config
from discussion.ldap_auth import Identity


@pytest.fixture
def app(monkeypatch):
    # A fake LDAP authenticate: 'admin'/'pw' -> admin identity, 'bob'/'pw' -> user.
    def fake_auth(cfg, username, password):
        if password != "pw" or username not in ("admin", "bob"):
            return Identity(user=username, tenant=cfg.tenant, authenticated=False)
        roles = ["administrators", "system_admin"] if username == "admin" else ["users"]
        return Identity(user=username, roles=roles, tenant=cfg.tenant, authenticated=True)

    # Patch every module that imported `authenticate` by name.
    monkeypatch.setattr("discussion.api.authenticate", fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", fake_auth)
    return build_app(Config())


@pytest.fixture
def client(app):
    return TestClient(app)


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["service"] == "discussion"


def test_whoami_requires_auth(client):
    assert client.get("/whoami").status_code == 401


def test_auth_token_then_whoami(client):
    r = client.post("/auth/token", json={"username": "bob", "password": "pw"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    # Tenant is per-request (X-Tenant), not baked into the token — one account can
    # act across tenants. So whoami reflects the request's tenant.
    who = client.get("/whoami", headers={"Authorization": f"Bearer {token}",
                                         "X-Tenant": "acme"})
    assert who.status_code == 200
    body = who.json()
    assert body["user"] == "bob"
    assert body["tenant"] == "acme"
    assert body["is_admin"] is False


def test_auth_token_bad_credentials(client):
    r = client.post("/auth/token", json={"username": "bob", "password": "wrong"})
    assert r.status_code == 401


def test_admin_flag_via_basic_auth(client):
    import base64
    cred = base64.b64encode(b"admin:pw").decode()
    who = client.get("/whoami", headers={"Authorization": f"Basic {cred}"})
    assert who.status_code == 200
    assert who.json()["is_admin"] is True


def test_basic_auth_rejected_when_invalid(client):
    import base64
    cred = base64.b64encode(b"bob:nope").decode()
    assert client.get("/whoami", headers={"Authorization": f"Basic {cred}"}).status_code == 401

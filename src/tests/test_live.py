"""Live comment sync + co-viewing presence (§10h / M4b).

LiveHub routing/roster is tested directly with fake sockets (asyncio.run); the WS
endpoint is smoke-tested via TestClient with stubbed auth + FakePerms.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.config import Config
from discussion.ldap_auth import Identity
from discussion.live import Connection, LiveHub

from .test_threads import FakePerms, _auth, _fake_auth


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


def _conn(user, file_uid="f1", invisible=False, roles=None):
    ident = Identity(user=user, roles=roles or ["users"], tenant="default")
    ws = FakeWS()
    return Connection(ws, ident, file_uid, invisible=invisible), ws


# ------------------------------ hub unit -----------------------------------
def test_presence_join_and_leave():
    async def go():
        hub = LiveHub(Config(), FakePerms(reads=True))
        b, bws = _conn("bob")
        c, cws = _conn("carol")
        await hub.join(b)
        assert bws.sent[-1] == {"type": "presence", "viewers": ["bob"], "count": 1}
        await hub.join(c)
        assert bws.sent[-1]["viewers"] == ["bob", "carol"]
        assert cws.sent[-1]["viewers"] == ["bob", "carol"]
        await hub.leave(b)
        assert cws.sent[-1]["viewers"] == ["carol"]
    asyncio.run(go())


def test_invisible_admin_excluded_from_roster_but_receives():
    async def go():
        hub = LiveHub(Config(), FakePerms(reads=True))
        b, bws = _conn("bob")
        a, aws = _conn("admin", invisible=True, roles=["administrators", "system_admin"])
        await hub.join(b)
        await hub.join(a)
        assert hub.roster("default", "f1") == ["bob"]      # admin hidden
        assert bws.sent[-1]["viewers"] == ["bob"]
        assert aws.sent[-1]["type"] == "presence"          # admin still sees presence
        await hub.broadcast("default", "f1", {"type": "comment", "action": "created"})
        assert {"type": "comment", "action": "created"} in aws.sent  # …and receives comments
        assert {"type": "comment", "action": "created"} in bws.sent
    asyncio.run(go())


def test_broadcast_skips_reader_who_lost_access():
    async def go():
        hub = LiveHub(Config(), FakePerms(reads=True, deny_users={"carol"}))
        b, bws = _conn("bob")
        c, cws = _conn("carol")
        await hub.join(b)
        await hub.join(c)
        cut = len(cws.sent)
        await hub.broadcast("default", "f1", {"type": "comment"})
        assert {"type": "comment"} in bws.sent
        assert {"type": "comment"} not in cws.sent[cut:]   # revoked → no delivery
    asyncio.run(go())


def test_bridge_publish_called():
    async def go():
        published = []

        class Bridge:
            def publish(self, tenant, file_uid, message):
                published.append((tenant, file_uid, message))
        hub = LiveHub(Config(), FakePerms(reads=True), bridge=Bridge())
        await hub.broadcast("default", "f1", {"type": "comment"})
        assert published == [("default", "f1", {"type": "comment"})]
    asyncio.run(go())


# ------------------------------ WS endpoint --------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)
    app = build_app(Config(), permissions=FakePerms(reads={"f1"}))
    return TestClient(app)


def test_ws_connect_receives_presence(client):
    with client.websocket_connect("/files/f1/live", headers=_auth("bob")) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "presence" and msg["viewers"] == ["bob"]


def test_ws_forbidden_without_read(client):
    with client.websocket_connect("/files/f2/live", headers=_auth("bob")) as ws:
        assert ws.receive_json()["type"] == "error"


def test_ws_admin_can_view_invisibly(client):
    with client.websocket_connect("/files/f1/live?invisible=1", headers=_auth("admin")) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "presence" and msg["viewers"] == []   # admin hidden


def test_ws_non_admin_invisible_is_ignored(client):
    with client.websocket_connect("/files/f1/live?invisible=1", headers=_auth("bob")) as ws:
        assert ws.receive_json()["viewers"] == ["bob"]              # bob still visible

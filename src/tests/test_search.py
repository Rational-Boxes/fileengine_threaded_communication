"""Comment indexing, search & internal retrieve (M3) — hermetic."""
import pytest
from fastapi.testclient import TestClient

from discussion.app import build_app
from discussion.chunking import chunk_text
from discussion.config import Config
from discussion.embeddings import HashEmbedder, build_embedder
from discussion.indexing import Indexer
from discussion.ldap_auth import Identity
from discussion.search import Searcher

from .test_threads import FakePerms, _auth, _fake_auth


# ------------------------- embeddings / chunking ---------------------------
def test_hash_embedder_shape_and_determinism():
    e = HashEmbedder(16)
    v1 = e.embed_query("hello world")
    v2 = e.embed_query("hello world")
    assert len(v1) == 16 and v1 == v2                 # deterministic
    assert e.embed_query("different") != v1
    # roughly unit-normalized
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6


def test_build_embedder_defaults_to_hash():
    cfg = Config()
    cfg.embedding_provider = "hash"
    assert isinstance(build_embedder(cfg), HashEmbedder)


def test_chunking_short_and_long():
    assert chunk_text("") == []
    assert chunk_text("one small comment") == ["one small comment"]
    big = "\n\n".join(["para " + "x" * 400 for _ in range(6)])
    chunks = chunk_text(big, max_chars=500)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)


# --------------------------- Searcher unit ---------------------------------
class FakeChunks:
    def __init__(self, fts_rows=None, ann_rows=None):
        self.fts_rows = fts_rows or []
        self.ann_rows = ann_rows or []

    def fts_search(self, tenant, query, fetch):
        return list(self.fts_rows)

    def ann_search(self, tenant, qv, k):
        return list(self.ann_rows[:k])


def _row(cid, fuid):
    return {"comment_id": cid, "file_uid": fuid, "thread_id": "t", "text": "x", "score": 1.0}


def test_search_acl_filters_by_anchor():
    chunks = FakeChunks(fts_rows=[_row("c1", "f1"), _row("c2", "f2"), _row("c1", "f1")])
    s = Searcher(HashEmbedder(8), chunks, FakePerms(reads={"f1"}))
    res = s.search(Identity(user="bob", tenant="default"), "q", limit=10)
    # only f1 is readable, and the duplicate comment is de-duplicated
    assert [r["comment_id"] for r in res] == ["c1"]


def test_retrieve_is_not_acl_filtered():
    chunks = FakeChunks(ann_rows=[_row("c1", "fX"), _row("c2", "fY")])
    # perms deny everything — retrieve must still return candidates (CSAI filters)
    s = Searcher(HashEmbedder(8), chunks, FakePerms(reads=None))
    assert len(s.retrieve("default", "q", k=5)) == 2


def test_indexer_removes_when_text_empty():
    class Chunks:
        def __init__(self):
            self.replaced, self.removed = [], []
        def replace(self, tenant, comment_id, file_uid, thread_id, items):
            self.replaced.append(comment_id)
        def remove(self, tenant, comment_id):
            self.removed.append(comment_id)
    ch = Chunks()
    idx = Indexer(HashEmbedder(8), ch)
    assert idx.index_comment("t", comment_id="c1", file_uid="f", thread_id="th", text="hi") == 1
    assert ch.replaced == ["c1"]
    assert idx.index_comment("t", comment_id="c2", file_uid="f", thread_id="th", text="  ") == 0
    assert ch.removed == ["c2"]


# ------------------------------ API layer ----------------------------------
class FakeSearcher:
    def search(self, identity, query, *, limit):
        return [{"comment_id": "c1", "file_uid": "f1", "thread_id": "t1", "text": "hit", "score": 1.0}]

    def retrieve(self, tenant, query, *, k):
        return [{"comment_id": "c1", "file_uid": "f1", "thread_id": "t1", "text": "cand", "score": 0.9}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("discussion.api.authenticate", _fake_auth)
    monkeypatch.setattr("discussion.http_auth.authenticate", _fake_auth)
    app = build_app(Config(), searcher=FakeSearcher())
    return TestClient(app)


def test_search_endpoint(client):
    r = client.get("/search?q=hello", headers=_auth("bob"))
    assert r.status_code == 200 and r.json()["results"][0]["comment_id"] == "c1"


def test_search_requires_query(client):
    assert client.get("/search", headers=_auth("bob")).status_code == 422


def test_internal_retrieve_is_system_admin_only(client):
    # a normal user is rejected …
    assert client.post("/internal/retrieve", json={"query": "q"},
                       headers=_auth("bob")).status_code == 403
    # … the admin (system_admin) is allowed.
    r = client.post("/internal/retrieve", json={"query": "q", "k": 3}, headers=_auth("admin"))
    assert r.status_code == 200 and r.json()["candidates"][0]["text"] == "cand"


def test_internal_retrieve_requires_query(client):
    assert client.post("/internal/retrieve", json={}, headers=_auth("admin")).status_code == 422

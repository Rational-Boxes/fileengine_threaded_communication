"""Config parsing — hermetic (no services)."""
import importlib

import pytest

from discussion.config import Config, _strip_value


def test_defaults(monkeypatch):
    # Clear anything that would leak from the ambient environment.
    for k in list(__import__("os").environ):
        if k.startswith("DISC_") or k.startswith("FILEENGINE_"):
            monkeypatch.delenv(k, raising=False)
    cfg = Config()
    assert cfg.http_port == 8094
    assert cfg.grpc_address == "localhost:50051"
    assert cfg.tenant == "default"
    assert cfg.pg_database == "discussion"
    assert cfg.embedding_dimension == 1024
    assert cfg.presence_admin_invisible is True
    assert cfg.cors_origins == []
    assert "dbname=discussion" in cfg.pg_dsn


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("DISC_HTTP_PORT", "9000")
    monkeypatch.setenv("DISC_PG_PORT", "5434")
    monkeypatch.setenv("DISC_CORS_ORIGINS", "http://a.test, http://b.test")
    monkeypatch.setenv("FILEENGINE_GRPC_HOST", "core")
    cfg = Config()
    assert cfg.http_port == 9000
    assert cfg.pg_port == 5434
    assert cfg.grpc_address == "core:50051"
    assert cfg.cors_origins == ["http://a.test", "http://b.test"]


def test_replica_toggles(monkeypatch):
    monkeypatch.delenv("DISC_PG_REPLICA_HOST", raising=False)
    monkeypatch.setenv("DISC_PG_REPLICA_ENABLED", "true")
    cfg = Config()
    assert cfg.pg_replica_enabled is True
    assert cfg.pg_replica_host == "localhost"


@pytest.mark.parametrize("raw,expected", [
    ("plain", "plain"),
    ('"quoted value"', "quoted value"),
    ("value # inline comment", "value"),
    ("# whole comment", ""),
    ("'single'", "single"),
])
def test_strip_value(raw, expected):
    assert _strip_value(raw) == expected

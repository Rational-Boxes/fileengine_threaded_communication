"""Permission READ cache + event-driven invalidation (§5 / M4a)."""
from discussion.config import Config
from discussion.ldap_auth import Identity
from discussion.permissions import Permissions


def _counting(p):
    calls = {"n": 0}

    def fake_check(identity, file_uid, perm):
        calls["n"] += 1
        return True
    p.check = fake_check
    return calls


def test_can_read_is_cached():
    p = Permissions(Config())
    calls = _counting(p)
    bob = Identity(user="bob", tenant="default")
    assert p.can_read(bob, "f1") and p.can_read(bob, "f1")
    assert calls["n"] == 1                       # second hit served from cache
    p.can_read(bob, "f2")
    assert calls["n"] == 2                        # different file → real check


def test_invalidation_evicts():
    p = Permissions(Config())
    calls = _counting(p)
    bob = Identity(user="bob", tenant="default")
    p.can_read(bob, "f1")
    p.invalidate_resource("default", "f1")
    p.can_read(bob, "f1")
    assert calls["n"] == 2                        # re-checked after resource evict
    p.invalidate_member("default", "bob")
    p.can_read(bob, "f1")
    assert calls["n"] == 3                        # re-checked after member evict
    p.invalidate_tenant("default")
    p.can_read(bob, "f1")
    assert calls["n"] == 4                        # re-checked after tenant evict


def test_can_write_is_not_cached():
    p = Permissions(Config())
    calls = _counting(p)
    bob = Identity(user="bob", tenant="default")
    p.can_write(bob, "f1")
    p.can_write(bob, "f1")
    assert calls["n"] == 2                        # writes always live

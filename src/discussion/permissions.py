"""Permission checks — derived from the anchor document's ACL, evaluated *as the
end user* (SPECIFICATION §5).

The service holds zero enforcement logic: every visibility/mutation decision maps
to a core ``CheckPermission`` on the anchor ``file_uid``, run through a gRPC client
bound to the requesting identity. Fail-closed — any error denies.

M1 checks on each call (no cache); the ≤5-min PermissionGate cache + event-driven
invalidation (§5) arrive with the event consumer in M2.
"""
from __future__ import annotations

import logging
import threading
import time

from .ldap_auth import Identity

log = logging.getLogger("discussion.permissions")


class Permissions:
    """READ decisions are cached per ``(tenant, user, file_uid)`` for
    ``permission_cache_ttl`` seconds (≤5 min, §5), with real-time eviction driven by
    the core event consumer (M4a: ``acl.changed`` → invalidate_resource, ``role.*`` →
    invalidate_member/tenant). WRITE is checked live (rarer, mutation path)."""

    def __init__(self, config):
        self.config = config
        self._ttl = getattr(config, "permission_cache_ttl", 300)
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, str], tuple[bool, float]] = {}

    def check(self, identity: Identity, file_uid: str, perm: str) -> bool:
        """True if ``identity`` has ``perm`` (``"r"``/``"w"``/…) on ``file_uid``.
        Fail-closed: unreachable core or any error → False. Uncached."""
        if not file_uid:
            return False
        from .core_client import client_for
        try:
            mf = client_for(identity, self.config)
        except Exception:
            log.warning("permission check: could not build core client", exc_info=True)
            return False
        try:
            return bool(mf.check_permission(file_uid, perm, tenant=identity.tenant))
        except Exception:
            log.warning("permission check failed for %s on %s", perm, file_uid, exc_info=True)
            return False
        finally:
            try:
                mf.close()
            except Exception:
                pass

    def can_read(self, identity: Identity, file_uid: str) -> bool:
        key = (identity.tenant, identity.user, file_uid)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        val = self.check(identity, file_uid, "r")
        with self._lock:
            self._cache[key] = (val, now + self._ttl)
        return val

    def can_write(self, identity: Identity, file_uid: str) -> bool:
        return self.check(identity, file_uid, "w")

    # -- event-driven invalidation (called by the consumer, M4a) -------------
    def invalidate_resource(self, tenant: str, file_uid: str) -> None:
        with self._lock:
            for k in [k for k in self._cache if k[0] == tenant and k[2] == file_uid]:
                del self._cache[k]

    def invalidate_member(self, tenant: str, user: str) -> None:
        with self._lock:
            for k in [k for k in self._cache if k[0] == tenant and k[1] == user]:
                del self._cache[k]

    def invalidate_tenant(self, tenant: str) -> None:
        with self._lock:
            for k in [k for k in self._cache if k[0] == tenant]:
                del self._cache[k]

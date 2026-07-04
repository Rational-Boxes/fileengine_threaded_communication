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

from .ldap_auth import Identity

log = logging.getLogger("discussion.permissions")


class Permissions:
    def __init__(self, config):
        self.config = config

    def check(self, identity: Identity, file_uid: str, perm: str) -> bool:
        """True if ``identity`` has ``perm`` (``"r"``/``"w"``/…) on ``file_uid``.
        Fail-closed: unreachable core or any error → False."""
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
        return self.check(identity, file_uid, "r")

    def can_write(self, identity: Identity, file_uid: str) -> bool:
        return self.check(identity, file_uid, "w")
